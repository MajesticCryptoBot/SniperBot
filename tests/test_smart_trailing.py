"""Tests for Smart Trailing: the phase engine, the smart_triggers DB layer,
and the SmartMonitor tick paths (advance / ratchet / fire / no-position)."""

from __future__ import annotations

import asyncio

import pytest

from sniper.db import Database
from sniper.smart_monitor import SmartMonitor
from sniper.smart_trailing import (
    PHASE_COUNT,
    evaluate,
    locked_pct,
    phase_for,
    phase_name,
    pnl_pct,
)

MINT = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"


def make_db():
    db = Database.connect_sqlite(":memory:")
    db.apply_schema()
    return db


def make_user(db, telegram_id="7", paused=0):
    db.create_user_sync(
        telegram_id=telegram_id,
        username="tester",
        chat_id=telegram_id,
        wallet_address="W",
        enc_payload="enc",
        defaults={
            "slippage_bps": 500,
            "priority_fee_lamports": 100_000,
            "max_sol_per_trade": 0.1,
            "daily_spend_limit_sol": 0.5,
            "autosnipe_amount_sol": 0.05,
        },
    )
    if paused:
        db.execute_sync("UPDATE users SET trading_paused=1 WHERE telegram_id=?", (telegram_id,))


def smart_row(trigger_id=1, phase=0, peak=None, floor=None, telegram_id="7"):
    return {
        "id": trigger_id,
        "telegram_id": telegram_id,
        "token_mint": MINT,
        "phase": phase,
        "peak_ratio": peak,
        "floor_ratio": floor,
        "chat_id": telegram_id,
        "wallet_address": "W",
        "slippage_bps": 500,
        "priority_fee_lamports": 100_000,
    }


# ---------------------------------------------------------------------------
# Phase engine — pure decisions
# ---------------------------------------------------------------------------


def test_phase_names_and_count():
    assert PHASE_COUNT == 6
    assert phase_name(0) == "RUN"
    assert phase_name(2) == "LOCK"
    assert phase_name(5) == "MOONSHOT"
    assert phase_name(99) == "MOONSHOT"  # clamped


def test_phase_for_thresholds():
    assert phase_for(0.0) == 0
    assert phase_for(9.9) == 0
    assert phase_for(10.0) == 1
    assert phase_for(19.9) == 1
    assert phase_for(20.0) == 2
    assert phase_for(34.9) == 2
    assert phase_for(35.0) == 3
    assert phase_for(74.9) == 3
    assert phase_for(75.0) == 4
    assert phase_for(149.9) == 4
    assert phase_for(150.0) == 5


def test_first_tick_lays_floor_one_run_below_and_waits():
    decision, state, info = evaluate(1.0, 1.0, 0, None, None)
    assert decision == "wait"
    assert state["phase"] == 0
    assert state["floor_ratio"] == pytest.approx(0.75)
    assert state["peak_ratio"] == pytest.approx(1.0)


def test_breathe_advances_and_locks_profit():
    decision, state, info = evaluate(1.12, 1.0, 0, 1.12, 0.84)
    assert decision == "advance"
    assert state["phase"] == 1
    assert info["phase"] == 1
    # peak 1.12 with the 8% BREATHE distance → floor 1.0304 → locks +3.04%
    assert state["floor_ratio"] == pytest.approx(1.12 * 0.92)
    assert info["locked_pct"] == pytest.approx(3.04, abs=0.01)


def test_lock_phase_tightens_again():
    decision, state, info = evaluate(1.25, 1.0, 1, 1.25, 1.15)
    assert decision == "advance"
    assert state["phase"] == 2
    assert state["floor_ratio"] == pytest.approx(1.25 * 0.95)
    assert info["locked_pct"] == pytest.approx(18.75, abs=0.01)


def test_floor_never_moves_down_on_pullback():
    # After LOCK the floor is 5% under the peak; a deep pullback above the
    # floor must not move it, and a deeper one must fire.
    _, state, _ = evaluate(1.25, 1.0, 2, 1.25, 1.1875)
    decision, state2, _ = evaluate(1.20, 1.0, state["phase"], state["peak_ratio"], state["floor_ratio"])
    assert decision == "wait"
    assert state2["floor_ratio"] == pytest.approx(1.1875)
    decision3, _, info3 = evaluate(1.10, 1.0, state["phase"], state["peak_ratio"], state["floor_ratio"])
    assert decision3 == "fire"
    assert info3["locked_pct"] == pytest.approx(18.75, abs=0.01)


def test_moonshot_widening_never_lets_the_floor_drop():
    # Deep in HARVEST the floor hugs the peak at 10%; crossing into
    # MOONSHOT must not lower the floor even though the distance widens.
    _, state, _ = evaluate(2.40, 1.0, 4, 2.40, 2.16)
    assert state["floor_ratio"] == pytest.approx(2.16)
    # +55% over entry crosses into MOONSHOT; the wider 18% distance would
    # put the floor at 2.55*0.82 ≈ 2.09 — below the stored floor.
    decision, state2, _ = evaluate(2.55, 1.0, state["phase"], 2.55, state["floor_ratio"])
    assert decision == "advance"
    assert state2["phase"] == 5
    assert state2["floor_ratio"] == pytest.approx(2.16)
    # The widening only pays off on new highs.
    _, state3, _ = evaluate(3.00, 1.0, 5, 3.00, state2["floor_ratio"])
    assert state3["floor_ratio"] == pytest.approx(3.00 * 0.82)


def test_fire_on_exact_floor_touch():
    decision, _, info = evaluate(0.75, 1.0, 0, 1.0, 0.75)
    assert decision == "fire"
    assert info["locked_pct"] == pytest.approx(-25.0, abs=0.01)


def test_ratchet_reports_silent_floor_move():
    decision, state, info = evaluate(1.05, 1.0, 1, 1.05, 0.90)
    assert decision == "ratchet"
    assert info["floor_ratio"] == pytest.approx(1.05 * 0.92)
    assert state["phase"] == 1


def test_bad_ratios_wait():
    assert evaluate(0.0, 1.0, 0, None, None)[0] == "wait"
    assert evaluate(1.0, 0.0, 0, None, None)[0] == "wait"


def test_helpers():
    assert pnl_pct(1.2, 1.0) == pytest.approx(20.0)
    assert locked_pct(1.1875, 1.0) == pytest.approx(18.75)


def test_engine_invariants_on_random_walk():
    """Floor monotone up, phases monotone up, exit fires exactly once."""
    import random

    random.seed(7)
    ratio, entry = 1.0, 1.0
    phase, peak, floor = 0, None, None
    fired = False
    for _ in range(3000):
        ratio *= random.uniform(0.90, 1.12)
        decision, state, _ = evaluate(ratio, entry, phase, peak, floor)
        if decision == "fire":
            fired = True
            break
        assert state["floor_ratio"] >= (floor or 0.0)
        assert state["phase"] >= phase
        phase, peak, floor = state["phase"], state["peak_ratio"], state["floor_ratio"]
    assert fired


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def test_schema_is_idempotent():
    db = make_db()
    db.apply_schema()  # second pass must not raise
    row = db.fetchone_sync("SELECT name FROM sqlite_master WHERE name='smart_triggers'")
    assert row is not None


async def test_add_cancels_previous_open_order_for_same_pair():
    db = make_db()
    make_user(db)
    first = await db.add_smart_trigger("7", MINT)
    assert first > 0
    second = await db.add_smart_trigger("7", MINT)
    open_rows = db.fetchall_sync("SELECT id FROM smart_triggers WHERE status='open'")
    assert [row["id"] for row in open_rows] == [second]
    old = db.fetchone_sync("SELECT status FROM smart_triggers WHERE id=?", (first,))
    assert old["status"] == "cancelled"


async def test_open_smart_triggers_joins_and_respects_pause():
    db = make_db()
    make_user(db)
    make_user(db, telegram_id="8", paused=1)
    await db.add_smart_trigger("7", MINT)
    await db.add_smart_trigger("8", MINT)
    rows = await db.open_smart_triggers()
    assert [row["telegram_id"] for row in rows] == ["7"]
    assert rows[0]["wallet_address"] == "W"
    assert rows[0]["slippage_bps"] == 500


async def test_smart_state_and_status_updates():
    db = make_db()
    make_user(db)
    trigger_id = await db.add_smart_trigger("7", MINT)
    await db.update_smart_state(trigger_id, 2, 1.25, 1.1875)
    row = await db.smart_trigger_for("7", MINT)
    assert row["phase"] == 2
    assert row["floor_ratio"] == pytest.approx(1.1875)
    await db.set_smart_status(trigger_id, "triggered", trade_id="tid-1")
    assert await db.smart_trigger_for("7", MINT) is None
    listing = await db.smart_triggers_for("7")
    assert listing[0]["status"] == "triggered"
    assert listing[0]["trade_id"] == "tid-1"


async def test_cancel_smart_trigger_ownership():
    db = make_db()
    make_user(db)
    make_user(db, telegram_id="9")
    trigger_id = await db.add_smart_trigger("7", MINT)
    assert await db.cancel_smart_trigger(trigger_id, "9") is False
    assert await db.cancel_smart_trigger(trigger_id, "7") is True


async def test_position_for_returns_wallet_and_chat():
    db = make_db()
    make_user(db)
    from sniper.accounting import apply_buy

    current = db.fetchone_sync(
        "SELECT amount_raw, cost_basis_sol FROM positions WHERE telegram_id=? AND token_mint=?",
        ("7", MINT),
    )
    raw, cost = apply_buy(
        int(current["amount_raw"]) if current else 0,
        float(current["cost_basis_sol"]) if current else 0.0,
        1_000_000,
        0.05,
    )
    db.execute_sync(
        "INSERT INTO positions (telegram_id, token_mint, amount_raw, cost_basis_sol, realized_pnl_sol, armed, updated_at) "
        "VALUES (?, ?, ?, ?, 0, 1, 'now') ON CONFLICT (telegram_id, token_mint) DO UPDATE SET amount_raw=?, cost_basis_sol=?",
        ("7", MINT, str(raw), cost, str(raw), cost),
    )
    row = await db.position_for("7", MINT)
    assert row["chat_id"] == "7"
    assert row["wallet_address"] == "W"
    assert int(row["amount_raw"]) == raw


# ---------------------------------------------------------------------------
# SmartMonitor tick paths
# ---------------------------------------------------------------------------


class FakeDB:
    def __init__(self, position):
        self.position = position
        self.updates = []
        self.statuses = []
        self.disarmed = []

    async def position_for(self, telegram_id, mint):
        return self.position

    async def update_smart_state(self, trigger_id, phase, peak, floor):
        self.updates.append((trigger_id, phase, peak, floor))

    async def set_smart_status(self, trigger_id, status, *, error=None, trade_id=None):
        self.statuses.append((trigger_id, status, error, trade_id))

    async def set_position_armed(self, telegram_id, mint, armed):
        self.disarmed.append((telegram_id, mint, armed))


class FakeQueue:
    def __init__(self, result=(None, None)):
        self.result = result
        self.notifies = []
        self.auto_sells = []

    async def notify(self, chat_id, text):
        self.notifies.append((chat_id, text))

    async def enqueue_auto_sell(self, row, kind, pnl, price):
        self.auto_sells.append((row, kind, pnl, price))
        return self.result


class FakeRPC:
    async def get_mint_info(self, mint):
        return {"decimals": 6}


def make_monitor(position, queue_result=(None, None)):
    db = FakeDB(position)
    queue = FakeQueue(queue_result)
    monitor = SmartMonitor(None, db, queue, None, FakeRPC(), {})
    return monitor, db, queue


POSITION = {
    "telegram_id": "7",
    "token_mint": MINT,
    "amount_raw": 5_000_000,  # 5.0 tokens at 6 decimals
    "cost_basis_sol": 0.25,   # 0.05 SOL per token entry
    "updated_at": "now",
    "chat_id": "7",
    "wallet_address": "W",
}

WSOL = "So11111111111111111111111111111111111111112"


async def test_monitor_advance_notifies_and_persists():
    monitor, db, queue = make_monitor(POSITION)
    row = smart_row(phase=0, peak=None, floor=None)  # fresh arm
    # token $100 with SOL $200 → 0.5 SOL/token over 0.05 entry → +900%
    await monitor._handle(row, {MINT: 100.0, WSOL: 200.0})
    assert db.updates and db.updates[0][1] == 5  # straight into MOONSHOT
    assert queue.notifies and "MOONSHOT" in queue.notifies[0][1]
    assert "locks" in queue.notifies[0][1]


async def test_monitor_fire_sells_whole_position():
    monitor, db, queue = make_monitor(POSITION, queue_result=("tid-9", None))
    # entry 0.05 SOL/token; LOCK-phase state: peak 0.0625 (+25%), floor 0.059375 (+18.75%)
    row = smart_row(phase=2, peak=0.0625, floor=0.059375)
    # token $11.6 with SOL $200 → 0.058 SOL/token → +16% — below the floor
    await monitor._handle(row, {MINT: 11.6, WSOL: 200.0})
    assert queue.auto_sells and queue.auto_sells[0][1] == "smart_trailing"
    assert queue.auto_sells[0][0]["amount_raw"] == 5_000_000
    assert queue.auto_sells[0][2] == pytest.approx(18.75, abs=0.01)
    assert db.statuses and db.statuses[0][1] == "triggered"
    assert db.statuses[0][3] == "tid-9"
    assert db.disarmed and db.disarmed[0][2] is False


async def test_monitor_rejected_sell_marks_failed():
    monitor, db, queue = make_monitor(POSITION, queue_result=(None, "insufficient liquidity"))
    row = smart_row(phase=2, peak=0.0625, floor=0.059375)
    await monitor._handle(row, {MINT: 11.6, WSOL: 200.0})
    assert db.statuses and db.statuses[0][1] == "failed"
    assert "liquidity" in db.statuses[0][2]


async def test_monitor_cancelled_when_position_gone():
    monitor, db, queue = make_monitor(None)
    row = smart_row(phase=0, peak=None, floor=None)
    await monitor._handle(row, {MINT: 100.0, WSOL: 200.0})
    assert db.statuses and db.statuses[0][1] == "cancelled"
    assert queue.notifies and "DISARMED" in queue.notifies[0][1]
