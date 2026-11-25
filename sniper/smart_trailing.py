"""Smart Trailing — the phased profit-locking trailing stop.

A position exit engine that follows the price up through six phases. Every
phase prescribes how far below the running peak the exit floor sits; each
time the position reaches the next profit threshold the floor tightens and
profit becomes locked. The floor never moves down, and the exit fires once
— selling the whole position — when the floor is touched.

The engine works on a SOL-stable price ratio: entry is the position's
average cost basis in SOL per token, and the current value is the token's
USD price over the live SOL price. A moving SOL price therefore does not
distort the profit measurement — the same axis the TP/SL monitor uses.

Phase defaults are bot-curated for DEX volatility (memecoins move an order
of magnitude faster than CEX pairs). Users do not tune numbers: they read
the short guide and arm the order; /triggers shows the live floor.

PHASES (name, profit threshold %, floor distance % below peak):
    RUN      —  armed          25%   room to breathe
    BREATHE  —  +10%           8%    first lock
    LOCK     —  +20%           5%    maximum compression
    STEEL    —  +35%           7%    absorb volatility
    HARVEST  —  +75%          10%    harvest zone
    MOONSHOT —  +150%         18%    re-widened to ride the runner

Pure functions only — the monitor and the panels decide I/O.
"""

from __future__ import annotations

# (name, profit_threshold_pct, trail_distance_pct)
# threshold None = the arming phase, active immediately.
PHASES: tuple[tuple[str, float | None, float], ...] = (
    ("RUN", None, 25.0),
    ("BREATHE", 10.0, 8.0),
    ("LOCK", 20.0, 5.0),
    ("STEEL", 35.0, 7.0),
    ("HARVEST", 75.0, 10.0),
    ("MOONSHOT", 150.0, 18.0),
)

PHASE_COUNT = len(PHASES)
RUN_DISTANCE_PCT = PHASES[0][2]


def phase_name(index: int) -> str:
    """Display name for a phase index (clamped into range)."""
    index = max(0, min(int(index), PHASE_COUNT - 1))
    return PHASES[index][0]


def phase_for(pnl_pct: float) -> int:
    """Highest phase index whose profit threshold is met (0 below all)."""
    index = 0
    for i, (_, threshold, _distance) in enumerate(PHASES):
        if threshold is not None and pnl_pct >= threshold:
            index = i
    return index


def pnl_pct(current_ratio: float, entry_ratio: float) -> float:
    """Profit percent of the current SOL-stable ratio over the entry basis."""
    if entry_ratio <= 0 or current_ratio <= 0:
        return 0.0
    return (current_ratio / entry_ratio - 1.0) * 100.0


def evaluate(
    current_ratio: float,
    entry_ratio: float,
    phase: int,
    peak_ratio: float | None,
    floor_ratio: float | None,
) -> tuple[str, dict, dict]:
    """Decide one monitoring tick for one Smart Trailing order.

    Returns (decision, next_state, info):
    - ("wait",    state, {})                 — nothing worth reporting
    - ("ratchet", state, {"floor_ratio"})    — floor moved up silently
    - ("advance", state, {"floor_ratio", "phase", "pnl_pct", "locked_pct"})
    - ("fire",    state, {"pnl_pct", "locked_pct"})

    Rules (in order): the tracked peak only rises; a phase only advances;
    the floor only rises; the exit fires the moment the floor is touched.
    """
    info: dict = {}
    if entry_ratio <= 0 or current_ratio <= 0:
        return "wait", _state(phase, peak_ratio, floor_ratio), info

    profit = pnl_pct(current_ratio, entry_ratio)

    # First evaluation after arming: lay the floor one RUN distance under
    # the value the position had right now (trailing starts from here).
    if floor_ratio is None:
        floor_ratio = current_ratio * (1.0 - RUN_DISTANCE_PCT / 100.0)
    if peak_ratio is None:
        peak_ratio = current_ratio
    if current_ratio > peak_ratio:
        peak_ratio = current_ratio

    new_phase = phase_for(profit)
    advanced = new_phase > int(phase)
    effective = new_phase if advanced else int(phase)
    distance = PHASES[effective][2]

    candidate = peak_ratio * (1.0 - distance / 100.0)
    floor_moved = floor_ratio is None or candidate > floor_ratio
    if floor_moved:
        floor_ratio = candidate
    state = _state(effective if advanced else phase, peak_ratio, floor_ratio)

    if current_ratio <= floor_ratio:
        info = {"pnl_pct": profit, "locked_pct": _locked(floor_ratio, entry_ratio)}
        return "fire", state, info
    if advanced:
        info = {
            "phase": new_phase,
            "pnl_pct": profit,
            "locked_pct": _locked(floor_ratio, entry_ratio),
        }
        return "advance", state, info
    if floor_moved:
        info = {"floor_ratio": floor_ratio}
        return "ratchet", state, info
    return "wait", state, info


def locked_pct(floor_ratio: float, entry_ratio: float) -> float:
    """The profit percent the current floor guarantees if it fires."""
    return _locked(floor_ratio, entry_ratio)


def _locked(floor_ratio: float, entry_ratio: float) -> float:
    if entry_ratio <= 0 or floor_ratio <= 0:
        return -100.0
    return (floor_ratio / entry_ratio - 1.0) * 100.0


def _state(phase, peak_ratio, floor_ratio) -> dict:
    return {"phase": int(phase), "peak_ratio": peak_ratio, "floor_ratio": floor_ratio}
