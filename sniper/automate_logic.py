"""Automate — P15 / P18, trailing dip, trailing stop.

Ported from the Binance desk and isolated from autosnipe, TP/SL, limit
orders, and Smart Trailing. Nothing in this module touches those paths.

Flow the monitor runs, one job at a time:

1. Patterns. Only Pattern 15 (bull momentum) and Pattern 18 (bear rebound).
   A score of 85 or more is the same "valid" line the CEX bot uses.
2. Trailing dip. Armed when a pattern is valid. The buy level sits one
   dip-distance above the price and only moves down as price makes new lows.
   A rebound into that level is the buy. A spike through the escape band
   without a fill cancels the dip and scanning resumes.
3. Trailing stop. After the buy, the position is managed with the CEX
   phase table (RUN → BREATHE → LOCK → STEEL → HARVEST → MOONSHOT).
   The stop only ratchets up. Touching it sells the whole position.

DEX adaptations:

- Candles replace Binance klines. Callers pass oldest-first OHLCV.
- There is no CEX order book, so OBI is the signed-volume imbalance of
  the recent candles (close >= open counts as buy volume).
- There is no BTC news index, so the CSI gate is not applied.
- Profit is measured on the SOL-stable ratio the rest of this bot uses
  (token USD / SOL USD against the position's SOL cost basis), so a move
  in SOL itself does not look like profit or loss.
- Distances are widened for Solana pool noise. A 0.3% CEX dip is smaller
  than a normal 5-minute wick here, and the MACD step is a fraction of
  price so a micro-cap and a high-priced token are scored the same way.
"""

from __future__ import annotations

ACCEPT_SCORE = 85
# Below this, an armed dip is dropped and scanning starts again.
# Kept under ACCEPT_SCORE so a one-tick dip in the score does not flicker.
KEEP_SCORE = 70

# Trailing-dip distances for a Solana pool, not a Binance book.
# atr_mult sizes the distance off ATR; min/cap are fractions of price.
# P15 stays inside 1.5–3%. P18 stays inside 3–6%.
TDIP_INIT = {
    15: {"atr_mult": 0.50, "min_pct": 0.015, "cap_pct": 0.030, "vol_hi": 1.08, "vol_lo": 0.85, "name": "P15-TIGHT"},
    18: {"atr_mult": 1.00, "min_pct": 0.030, "cap_pct": 0.060, "vol_hi": 1.18, "vol_lo": 1.00, "name": "P18-WIDE"},
}

# How far above the dip level price may run before the bounce is considered missed.
ESCAPE_HI = {15: 1.030, 18: 1.050}
ESCAPE_LO = {15: 1.030, 18: 1.050}

# Trailing-stop phases. Thresholds are unrealized percent; distance is the
# fraction of the current print the stop sits below.
# RUN is active under the first threshold and uses the wide RUN buffer.
PHASES = (
    # (index, name, pnl_at_or_above, pnl_below, distance_fraction)
    (0, "RUN", None, 8.0, 0.13),
    (1, "BREATHE", 8.0, 18.0, 0.040),
    (2, "LOCK", 18.0, 26.0, 0.030),
    (3, "STEEL", 26.0, 35.0, 0.040),
    (4, "HARVEST", 35.0, 47.0, 0.040),
    (5, "MOONSHOT", 47.0, None, 0.075),
)
PHASE_COUNT = len(PHASES)
RUN_MIN_DISTANCE = 0.12  # a RUN trail that has shrunk under 12% is reset
RUN_MAX_DISTANCE = 0.15  # RUN buffer never wider than 15%
RUN_RATCHET = 0.010      # price must advance 1% before a RUN ratchet


def phase_name(index: int) -> str:
    index = max(0, min(int(index), PHASE_COUNT - 1))
    return PHASES[index][1]


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = price * k + ema * (1.0 - k)
    return ema


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(candles: list[tuple], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        _ts, _o, high, low, close, _v = candles[i]
        prev_close = candles[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def _macd(closes: list[float]) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (macd, prev_macd, histogram, prev_histogram)."""
    if len(closes) < 35:
        return None, None, None, None
    series = []
    for i in range(26, len(closes) + 1):
        window = closes[:i]
        fast = _ema(window, 12)
        slow = _ema(window, 26)
        if fast is None or slow is None:
            continue
        series.append(fast - slow)
    if len(series) < 10:
        return None, None, None, None
    signal = _ema(series, 9)
    prev_signal = _ema(series[:-1], 9) if len(series) > 10 else signal
    if signal is None:
        return None, None, None, None
    macd = series[-1]
    prev_macd = series[-2]
    histogram = macd - signal
    prev_histogram = prev_macd - (prev_signal if prev_signal is not None else signal)
    return macd, prev_macd, histogram, prev_histogram


def _stdev(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return var ** 0.5


def indicators_from_candles(candles: list[tuple]) -> dict | None:
    """Build the indicator snapshot both patterns read.

    `candles` is oldest-first (ts, open, high, low, close, volume).
    Returns None when the series is too short to score.
    """
    if not candles or len(candles) < 50:
        return None
    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    opens = [float(c[1]) for c in candles]
    price = closes[-1]
    if price <= 0:
        return None

    def _need(period: int) -> float:
        value = _ema(closes, period)
        return float(value if value is not None else price)

    ema_5, ema_9, ema_13, ema_21, ema_50 = (_need(p) for p in (5, 9, 13, 21, 50))
    rsi = _rsi(closes) or 50.0
    rsi_prev = _rsi(closes[:-1]) or (rsi - 1.0)
    atr = _atr(candles) or (price * 0.01)
    prev_atr = _atr(candles[:-14]) or (atr * 0.9)
    atr_percent = (atr / price * 100.0) if price else 0.0
    macd, prev_macd, histogram, prev_histogram = _macd(closes)
    macd = macd or 0.0
    prev_macd = prev_macd if prev_macd is not None else macd * 0.9
    histogram = histogram or 0.0
    prev_histogram = prev_histogram if prev_histogram is not None else histogram - 0.1

    window = closes[-20:]
    middle = sum(window) / len(window)
    sd = _stdev(window)
    upper = middle + 2.0 * sd
    lower = middle - 2.0 * sd
    bb_width = ((upper - lower) / middle) if middle else 0.02

    current_volume = volumes[-1]
    avg_volume = sum(volumes[-10:]) / min(10, len(volumes))
    prev_volume = volumes[-2] if len(volumes) >= 2 else current_volume
    prev_close = closes[-2]
    price_low = min(closes[-5:])
    recent_high_5 = max(highs[-5:])
    recent_high_10 = max(highs[-10:])
    support = min(lows[-20:])

    # Signed-volume CVD. Buy candles (close >= open) add volume, sell candles subtract.
    cvd = 0.0
    cvd_values = []
    for o, c, v in zip(opens, closes, volumes):
        cvd += v if c >= o else -v
        cvd_values.append(cvd)
    prev_cvd = cvd_values[-2] if len(cvd_values) >= 2 else cvd * 0.9
    cvd_change_pct = ((cvd - prev_cvd) / abs(prev_cvd + 1e-6)) * 100.0

    def _obi(start: int, end: int) -> float:
        buy = sell = 0.0
        for i in range(start, end):
            if closes[i] >= opens[i]:
                buy += volumes[i]
            else:
                sell += volumes[i]
        total = buy + sell
        if total <= 0:
            return 0.0
        return (buy - sell) / total

    obi = _obi(len(closes) - 5, len(closes))
    prev_obi = _obi(len(closes) - 10, len(closes) - 5)

    return {
        "price": price,
        "ema_5": ema_5,
        "ema_9": ema_9,
        "ema_13": ema_13,
        "ema_21": ema_21,
        "ema_50": ema_50,
        "rsi": rsi,
        "rsi_prev": rsi_prev,
        "atr": atr,
        "prev_atr": prev_atr,
        "atr_percent": atr_percent,
        "macd": macd,
        "prev_macd": prev_macd,
        "histogram": histogram,
        "prev_histogram": prev_histogram,
        "bb_width": bb_width,
        "bb_lower": lower,
        "volume": current_volume,
        "avg_volume": avg_volume or current_volume,
        "prev_volume": prev_volume,
        "prev_close": prev_close,
        "price_low": price_low,
        "recent_high_5": recent_high_5,
        "recent_high_10": recent_high_10,
        "support": support,
        "cvd": cvd,
        "prev_cvd": prev_cvd,
        "cvd_change_pct": cvd_change_pct,
        "obi": obi,
        "prev_obi": prev_obi,
        "volatility_index": min(100.0, max(0.0, atr_percent * 8.0)),
    }


def _regime(ind: dict) -> str:
    ema_5, ema_9, ema_13 = ind["ema_5"], ind["ema_9"], ind["ema_13"]
    ema_21, ema_50 = ind["ema_21"], ind["ema_50"]
    rsi = ind["rsi"]
    full = ema_5 > ema_9 > ema_13 > ema_21 and ema_21 > ema_50 * 0.995
    if full and rsi > 52 and ind["macd"] >= 0 and ind["histogram"] >= ind["prev_histogram"]:
        return "trend_bull"
    if ind["price"] < ema_21 and rsi < 42 and ind["histogram"] < 0:
        return "trend_bear"
    if ind["bb_width"] < 0.018 and ind["atr_percent"] < 1.0:
        return "compression"
    if rsi < 30 and ind["histogram"] > ind["prev_histogram"]:
        return "reversal_up"
    if rsi > 72 and ind["histogram"] < ind["prev_histogram"]:
        return "reversal_down"
    return "neutral"


def _top_risk(ind: dict) -> tuple[float, bool]:
    """CEX top detector, reduced to the inputs a DEX candle series actually has."""
    price = ind["price"]
    score = 0.0
    near = price >= ind["recent_high_5"] * 0.996
    extension = ((price - ind["ema_21"]) / ind["ema_21"] * 100.0) if ind["ema_21"] else 0.0
    if near:
        score += 18
    if price >= ind["recent_high_10"] * 0.998:
        score += 12
    if extension > 1.8:
        score += 12
    if extension > 3.0:
        score += 10
    if ind["rsi"] > 72:
        score += 15
    if ind["rsi"] > 76:
        score += 10
    if ind["rsi"] < ind["rsi_prev"] and ind["rsi"] > 70:
        score += 10
    if near and ind["volume"] < ind["avg_volume"] * 0.85:
        score += 15
    if ind["obi"] < 0:
        score += 10
    if ind["cvd"] < ind["prev_cvd"] and price >= ind["prev_close"]:
        score += 10
    if ind["histogram"] < ind["prev_histogram"] and ind["histogram"] > 0:
        score += 12
    score = max(0.0, min(100.0, score))
    hard = False
    if ind["rsi"] > 73 and near and ind["volume"] < ind["avg_volume"] * 0.90:
        hard = True
    if near and ind["obi"] < 0 and ind["cvd"] < ind["prev_cvd"]:
        hard = True
    if extension > 3.0 and ind["rsi"] > 75:
        hard = True
    return score, hard


def _confluence(ind: dict, regime: str) -> float:
    """Enough of the CEX confluence stack for the P15 gate (needs >= 42)."""
    ema_aligned = ind["ema_9"] > ind["ema_21"] > ind["ema_50"] * 0.99
    macd_ok = ind["macd"] > 0 or ind["histogram"] > ind["prev_histogram"] * 0.90
    hist_up = ind["histogram"] > ind["prev_histogram"] * 0.90
    rsi_ok = 45 < ind["rsi"] < 75
    above_support = ind["price"] > ind["support"] * 1.005 if ind["support"] else True
    volume_ok = ind["volume"] > ind["avg_volume"] * 0.60
    obi_recovery = ind["prev_obi"] < 0 < ind["obi"]
    fast = obi_recovery and ind["cvd_change_pct"] > 5 and ind["rsi"] > 50
    met = sum([ema_aligned, macd_ok, hist_up, rsi_ok, above_support, volume_ok, fast])
    score = 0.0
    if met >= 5 or (fast and ema_aligned and hist_up and volume_ok):
        score += 30
    if (ind["cvd"] > ind["prev_cvd"] and ind["price"] < ind["prev_close"]) or obi_recovery:
        score += 15
    trend = abs((ind["ema_9"] - ind["ema_21"]) / max(abs(ind["ema_21"]), 1e-9)) * 100.0
    if score >= 30 and trend > 0.40:
        score += 40
    if regime == "trend_bull":
        score += 8
    return score


def _context(ind: dict) -> dict:
    price = ind["price"]
    regime = _regime(ind)
    top, hard = _top_risk(ind)
    near = price >= ind["recent_high_5"] * 0.996
    extension = ((price - ind["ema_21"]) / ind["ema_21"] * 100.0) if ind["ema_21"] else 0.0
    overextended = price > ind["ema_21"] * 1.018
    dist_low = ((price - ind["price_low"]) / ind["price_low"] * 100.0) if ind["price_low"] else 0.0
    support = ind["support"]
    atr = ind["atr"]
    if support and support > 0 and atr > 0:
        recent_bounce = price > support * 1.002 and price < support + atr * 3.0
    else:
        recent_bounce = False
    obi_recovery = ind["prev_obi"] < 0 < ind["obi"]
    cvd_div = ind["cvd"] > ind["prev_cvd"] and price < ind["prev_close"]
    return {
        "regime": regime,
        "top": top,
        "hard": hard,
        "near": near,
        "extension": extension,
        "overextended": overextended,
        "dist_low": dist_low,
        "recent_bounce": recent_bounce,
        "obi_recovery": obi_recovery,
        "cvd_div": cvd_div,
        "confluence": _confluence(ind, regime),
    }


def _score_p15(ind: dict, ctx: dict) -> dict:
    score = 0.0
    if ind["rsi"] > ind["rsi_prev"] + 1.5:
        score += 15
    macd_step = abs(ind["price"]) * 0.0005
    hist_floor = abs(ind["price"]) * 0.001
    if ind["macd"] > ind["prev_macd"] + macd_step:
        score += 15
    if ind["histogram"] > max(ind["prev_histogram"], -hist_floor):
        score += 15
    atr_threshold = 2.5 if ind["atr_percent"] > 6 else 1.2 if ind["atr_percent"] < 1 else 0.30
    if ind["atr_percent"] > atr_threshold * 0.7 and ind["rsi"] < 72 and ind["bb_width"] > 0.015:
        score += 20
    if ind["price"] > ind["ema_21"] * 0.998:
        score += 10
    if ind["price"] > ind["prev_close"] * 1.002:
        score += 10
    if ctx["recent_bounce"]:
        score += 10
    if ind["volume"] > ind["avg_volume"] * 1.2:
        score += 5
    if ind["cvd"] > ind["prev_cvd"] * 1.05:
        score += 5
    if ind["obi"] > 0.15 and (ind["obi"] > ind["prev_obi"] or ctx["obi_recovery"]):
        score += 5
    if ctx["obi_recovery"]:
        score += 5
    if ctx["regime"] in {"trend_bull", "reversal_up"}:
        score += 8
    if ind["cvd_change_pct"] < -12 and ind["obi"] < -0.12:
        score -= 20
    if ind["bb_width"] < 0.012:
        score -= 8
    if ctx["near"]:
        score -= 12
    if ctx["overextended"]:
        score -= 10
    if ctx["dist_low"] > 5.0:
        score -= 8
    if ctx["near"] and ind["rsi"] >= 70:
        score -= 10
    # Anti-deceleration, same shape as the CEX fade block.
    if ind["rsi"] < ind["rsi_prev"]:
        fade = ind["rsi_prev"] - ind["rsi"]
        score -= 22 if fade > 8 else 16 if fade > 2 else 9
    if ind["histogram"] < ind["prev_histogram"]:
        fade_h = ind["prev_histogram"] - ind["histogram"]
        strong = abs(ind["price"]) * 0.0008
        moderate = abs(ind["price"]) * 0.0003
        score -= 22 if fade_h > strong else 14 if fade_h > moderate else 8
    score = max(0.0, min(100.0, score))
    accel = sum(
        [
            ind["rsi"] > ind["rsi_prev"] + 1.5,
            ind["macd"] > ind["prev_macd"] + macd_step,
            ind["histogram"] > ind["prev_histogram"],
        ]
    )
    valid = (
        score >= ACCEPT_SCORE
        and accel >= 2
        and ctx["confluence"] >= 42
        and ctx["regime"] != "compression"
        and ind["atr_percent"] >= 1.0
        and ind["volume"] <= ind["avg_volume"] * 2.5
        and not ctx["hard"]
        and ctx["top"] < 55
        and not (ctx["near"] and ctx["extension"] > 2.2)
        and not (ctx["near"] and ind["rsi"] >= 70)
    )
    return {"number": 15, "name": "Momentum Acceleration", "match_pct": round(score), "valid": valid}


def _score_p18(ind: dict, ctx: dict) -> dict:
    score = 0.0
    rsi = ind["rsi"]
    if 15 < rsi < 25:
        score += 35
    if rsi > ind["rsi_prev"] + 0.5:
        score += 18
    hist, prev_h = ind["histogram"], ind["prev_histogram"]
    hist_floor = abs(ind["price"]) * 0.001
    if hist > prev_h * 0.95 or (hist > -hist_floor and prev_h < -hist_floor):
        score += 15
    if ind["price"] < ind["ema_50"] * 0.985:
        score += 10
    if ctx["cvd_div"]:
        score += 10
    if ind["cvd"] > ind["prev_cvd"] and ind["cvd"] > 0:
        score += 12
    if ind["obi"] > 0.05:
        score += 8
    if ind["atr"] > ind["prev_atr"] * 1.1:
        score += 6
    if ctx["obi_recovery"]:
        score += 10
    if ind["volume"] > ind["avg_volume"] * 1.1:
        score += 7
    if ctx["regime"] == "reversal_up":
        score += 10
    if ind["cvd_change_pct"] < -8 or ind["obi"] < -0.15 or ind["price"] > ind["price_low"] * 1.03:
        score -= 30
    score = max(0.0, min(100.0, score))
    return {
        "number": 18,
        "name": "Bearish Rebound Hunter",
        "match_pct": round(score),
        "valid": score >= ACCEPT_SCORE,
    }


def score_patterns(ind: dict | None, price: float | None = None) -> list[dict]:
    """Score P15 and P18. Highest score first. `valid` means the CEX buy line."""
    if not ind:
        return []
    if price and price > 0:
        ind = dict(ind)
        ind["price"] = float(price)
    ctx = _context(ind)
    patterns = [_score_p15(ind, ctx), _score_p18(ind, ctx)]
    patterns.sort(key=lambda row: row["match_pct"], reverse=True)
    return patterns


def best_valid(patterns: list[dict]) -> dict | None:
    valid = [row for row in patterns if row.get("valid")]
    if not valid:
        return None
    return max(valid, key=lambda row: row["match_pct"])


def any_kept(patterns: list[dict]) -> bool:
    """True when either pattern is still close enough to keep a live dip."""
    return any(row.get("match_pct", 0) >= KEEP_SCORE for row in patterns)


def init_dip(pattern: int, price: float, atr: float, volatility_index: float = 0.0) -> tuple[float, float, str]:
    """Return (dip_distance, dip_level, profile_name) for a fresh trailing dip."""
    cfg = TDIP_INIT.get(int(pattern), TDIP_INIT[18])
    if price <= 0:
        raise ValueError("price must be positive")
    atr_safe = float(atr or 0.0)
    distance = max(atr_safe * cfg["atr_mult"], price * cfg["min_pct"])
    vol = float(volatility_index or 0.0)
    if vol > 50:
        distance *= cfg["vol_hi"]
    elif vol < 20:
        distance *= cfg["vol_lo"]
    distance = max(distance, price * cfg["min_pct"])
    distance = min(distance, price * cfg["cap_pct"])
    return distance, price + distance, cfg["name"]


def ratchet_dip(price: float, trigger_price: float, dip_level: float, dip_distance: float) -> tuple[float, bool]:
    """Lower the buy level when price is under the original trigger.

    The level never rises. Returns (dip_level, dipped).
    """
    if price <= 0 or dip_distance <= 0:
        return dip_level, False
    dipped = price < trigger_price
    if not dipped:
        return dip_level, False
    candidate = price + dip_distance
    if candidate < dip_level:
        return candidate, True
    return dip_level, True


def dip_decision(price: float, dip_level: float, pattern: int, atr_percent: float) -> str:
    """What a live dip should do at this price: wait, buy, or escape."""
    if dip_level <= 0 or price <= 0:
        return "wait"
    band = ESCAPE_HI if atr_percent >= 1.0 else ESCAPE_LO
    escape = dip_level * band.get(int(pattern), 1.012)
    if price >= escape:
        return "escape"
    if price >= dip_level:
        return "buy"
    return "wait"


def init_trail(entry_ratio: float) -> dict:
    """Lay the RUN trailing stop under a fresh entry. Ratio is SOL per token."""
    if entry_ratio <= 0:
        raise ValueError("entry ratio must be positive")
    distance = entry_ratio * PHASES[0][4]
    return {
        "phase": 0,
        "distance": distance,
        "stop": entry_ratio - distance,
        "peak": entry_ratio,
        "last_ratchet": entry_ratio,
    }


def trail_tick(
    current_ratio: float,
    entry_ratio: float,
    phase: int,
    distance: float,
    stop: float,
    peak: float | None,
    last_ratchet: float | None,
) -> tuple[str, dict]:
    """One trailing-stop tick.

    Returns (decision, state). decision is wait, ratchet, advance, or fire.
    The stop never moves down. Distance becomes sacred once a profit phase
    is active: a later phase may tighten it, but the stop is only replaced
    when the new stop is higher.
    """
    state = {
        "phase": int(phase),
        "distance": float(distance),
        "stop": float(stop),
        "peak": peak if peak is not None else current_ratio,
        "last_ratchet": last_ratchet,
    }
    if entry_ratio <= 0 or current_ratio <= 0:
        return "wait", state

    if current_ratio > state["peak"]:
        state["peak"] = current_ratio
    pnl = (current_ratio / entry_ratio - 1.0) * 100.0

    new_phase = int(phase)
    new_frac = None
    for index, _name, at_or_above, below, frac in PHASES:
        if index == 0:
            continue
        upper_ok = below is None or pnl < below
        if pnl >= at_or_above and upper_ok and int(phase) < index:
            new_phase = index
            new_frac = frac
            break

    decision = "wait"
    if new_frac is not None:
        candidate_distance = current_ratio * new_frac
        candidate_stop = current_ratio - candidate_distance
        if candidate_stop > state["stop"]:
            state["distance"] = candidate_distance
            state["stop"] = candidate_stop
        else:
            state["distance"] = candidate_distance
        state["phase"] = new_phase
        decision = "advance"
    else:
        if int(phase) == 0 and state["distance"] < current_ratio * RUN_MIN_DISTANCE:
            state["distance"] = current_ratio * PHASES[0][4]
        new_stop = current_ratio - state["distance"]
        if int(phase) >= 1:
            if new_stop > state["stop"]:
                state["stop"] = new_stop
                state["last_ratchet"] = current_ratio
                decision = "ratchet"
        else:
            step = current_ratio * RUN_RATCHET
            moved = last_ratchet is None or current_ratio >= float(last_ratchet) + step
            if new_stop > state["stop"] and moved:
                state["stop"] = new_stop
                state["last_ratchet"] = current_ratio
                decision = "ratchet"

    if int(state["phase"]) == 0:
        cap = current_ratio * RUN_MAX_DISTANCE
        if state["distance"] > cap:
            state["distance"] = cap

    locked = (state["stop"] / entry_ratio - 1.0) * 100.0 if entry_ratio else 0.0
    state["pnl_pct"] = pnl
    state["locked_pct"] = locked
    if current_ratio <= state["stop"]:
        return "fire", state
    return decision, state


def parse_gecko_ohlcv(payload) -> list[tuple]:
    """GeckoTerminal ohlcv_list → oldest-first (ts, open, high, low, close, volume)."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    attrs = data.get("attributes") if isinstance(data, dict) else None
    rows = (attrs or {}).get("ohlcv_list") if isinstance(attrs, dict) else None
    if not isinstance(rows, list):
        return []
    parsed = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            parsed.append(
                (
                    int(row[0]),
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                )
            )
        except (TypeError, ValueError):
            continue
    parsed.sort(key=lambda item: item[0])
    return parsed
