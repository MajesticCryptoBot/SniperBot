"""Isolated /automate logic: P15/P18 only, trailing dip, trailing-stop phases."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sniper.automate_logic import (
    PHASES,
    best_valid,
    dip_decision,
    init_dip,
    ratchet_dip,
    score_patterns,
    trail_tick,
    init_trail,
)


def _ind(**overrides):
    base = {
        "price": 1.0,
        "ema_5": 1.02,
        "ema_9": 1.01,
        "ema_13": 1.0,
        "ema_21": 0.99,
        "ema_50": 0.95,
        "rsi": 62.0,
        "rsi_prev": 58.0,
        "atr": 0.02,
        "prev_atr": 0.018,
        "atr_percent": 2.0,
        "macd": 0.01,
        "prev_macd": 0.008,
        "histogram": 0.004,
        "prev_histogram": 0.002,
        "bb_width": 0.03,
        "bb_lower": 0.9,
        "volume": 150.0,
        "avg_volume": 100.0,
        "prev_volume": 90.0,
        "prev_close": 0.99,
        "price_low": 0.97,
        "recent_high_5": 1.05,
        "recent_high_10": 1.08,
        "support": 0.9,
        "cvd": 20.0,
        "prev_cvd": 10.0,
        "cvd_change_pct": 8.0,
        "obi": 0.2,
        "prev_obi": -0.05,
        "volatility_index": 16.0,
    }
    base.update(overrides)
    return base


def test_only_p15_and_p18_are_scored():
    numbers = {row["number"] for row in score_patterns(_ind())}
    assert numbers == {15, 18}


def test_p18_valid_on_a_washed_out_rebound():
    ind = _ind(
        price=0.90,
        ema_50=1.0,
        rsi=20.0,
        rsi_prev=18.0,
        histogram=-0.0005,
        prev_histogram=-0.002,
        cvd=5.0,
        prev_cvd=-2.0,
        cvd_change_pct=6.0,
        obi=0.1,
        prev_obi=-0.1,
        atr=0.03,
        prev_atr=0.02,
        volume=130.0,
        avg_volume=100.0,
        price_low=0.90,
    )
    patterns = score_patterns(ind)
    p18 = next(row for row in patterns if row["number"] == 18)
    assert p18["valid"] is True
    assert p18["match_pct"] >= 85
    assert best_valid(patterns)["number"] == 18


def test_trailing_dip_only_moves_down_and_buys_the_rebound():
    distance, level, name = init_dip(15, price=1.0, atr=0.01, volatility_index=10)
    assert name == "P15-TIGHT"
    assert 0.015 <= distance / 1.0 <= 0.030
    assert level == 1.0 + distance
    wide, _, wide_name = init_dip(18, price=1.0, atr=0.04, volatility_index=30)
    assert wide_name == "P18-WIDE"
    assert 0.030 <= wide / 1.0 <= 0.060
    lowered, dipped = ratchet_dip(0.97, trigger_price=1.0, dip_level=level, dip_distance=distance)
    assert dipped is True
    assert lowered < level
    held, _ = ratchet_dip(0.99, trigger_price=1.0, dip_level=lowered, dip_distance=distance)
    assert held == lowered
    assert dip_decision(lowered, lowered, 15, atr_percent=1.2) == "buy"
    assert dip_decision(lowered * 1.02, lowered, 15, atr_percent=1.2) == "buy"
    assert dip_decision(lowered * 0.99, lowered, 15, atr_percent=1.2) == "wait"
    assert dip_decision(lowered * 1.04, lowered, 15, atr_percent=1.2) == "escape"


def test_trailing_stop_phases_match_the_cex_table():
    entry = 1.0
    trail = init_trail(entry)
    assert trail["phase"] == 0
    assert abs(trail["distance"] - entry * 0.13) < 1e-9
    assert trail["stop"] == entry - trail["distance"]

    decision, state = trail_tick(1.01, entry, 0, trail["distance"], trail["stop"], trail["peak"], trail["last_ratchet"])
    assert decision in {"wait", "ratchet"}
    assert state["phase"] == 0
    assert state["stop"] >= trail["stop"]

    decision, state = trail_tick(1.08, entry, 0, trail["distance"], trail["stop"], 1.0, 1.0)
    assert decision == "advance"
    assert state["phase"] == 1
    assert PHASES[1][1] == "BREATHE"
    assert abs(state["distance"] / 1.08 - 0.040) < 1e-9

    breathe_stop = state["stop"]
    decision, state = trail_tick(1.20, entry, 1, state["distance"], breathe_stop, 1.08, 1.08)
    assert state["phase"] == 2
    assert PHASES[2][1] == "LOCK"
    assert abs(state["distance"] / 1.20 - 0.030) < 1e-9
    assert state["stop"] >= breathe_stop

    decision, fired = trail_tick(
        state["stop"] * 0.999, entry, state["phase"], state["distance"], state["stop"], state["peak"], state["last_ratchet"]
    )
    assert decision == "fire"
    assert fired["stop"] >= state["stop"]


def test_schema_adds_automate_jobs_beside_existing_tables():
    sql = Path(__file__).resolve().parents[1].joinpath("schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)
    conn.executescript(sql)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"automate_jobs", "positions", "snipe_watch", "trade_triggers", "trades"} <= names
    conn.execute(
        "INSERT INTO users (telegram_id, chat_id, created_at, updated_at) VALUES ('7', '7', 't', 't')"
    )
    conn.execute(
        "INSERT INTO automate_jobs (telegram_id, token_mint, amount_sol, status, created_at, updated_at) "
        "VALUES ('7', 'Mint111', 0.05, 'scanning', 't', 't')"
    )
    conn.close()
