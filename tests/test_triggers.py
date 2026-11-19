"""Tests for limit orders / trailing stops: evaluation logic, DB layer,
migration, the TriggerMonitor tick, and queue execution paths."""

from __future__ import annotations

from sniper.db import Database
from sniper.trade_queue import TradeQueue
from sniper.trigger_monitor import TriggerMonitor, evaluate_trigger

MINT = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"


def make_db():
    db = Database.connect_sqlite(":memory:")
    db.apply_schema()
    return db


def make_user(db, telegram_id="7"):
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


def limit_buy_row(target=0.5, amount=0.05, trigger_id=1):
    return {
        "id": trigger_id, "telegram_id": "7", "kind": "limit_buy", "token_mint": MINT,
        "target_price_usd": target, "trail_distance_pct": None, "peak_price_usd": None,
        "amount_sol": amount, "sell_percent": None, "chat_id": "7", "wallet_address": "W",
        "slippage_bps": 500, "max_sol_per_trade": 0.1, "daily_spend_limit_sol": 0.5,
        "max_price_impact_pct": 50, "autosnipe_max_price_impact_pct": 30, "priority_fee_lamports": 100000,
    }


def trailing_stop_row(distance=20, peak=None, percent=100, trigger_id=2):
    return {
        "id": trigger_id, "telegram_id": "7", "kind": "trailing_stop", "token_mint": MINT,
        "target_price_usd": None, "trail_distance_pct": distance, "peak_price_usd": peak,
        "amount_sol": None, "sell_percent": percent, "chat_id": "7", "wallet_address": "W",
        "slippage_bps": 500, "max_sol_per_trade": 0.1, "daily_spend_limit_sol": 0.5,
        "max_price_impact_pct": 50, "autosnipe_max_price_impact_pct": 30, "priority_fee_lamports": 100000,
    }


def limit_sell_row(target=2.0, percent=50, trigger_id=3):
    row = trailing_stop_row(trigger_id=trigger_id)
    row.update(kind="limit_sell", target_price_usd=target, trail_distance_pct=None, sell_percent=percent)
    return row


# ---------------------------------------------------------------------------
# evaluate_trigger — pure decision logic
# ---------------------------------------------------------------------------

def test_limit_buy_fires_at_or_below_target():
    assert evaluate_trigger(limit_buy_row(target=0.5), 0.49)[0] == "fire_buy"
    assert evaluate_trigger(limit_buy_row(target=0.5), 0.5)[0] == "fire_buy"
    assert evaluate_trigger(limit_buy_row(target=0.5), 0.51)[0] == "wait"


def test_limit_sell_fires_at_or_above_target():
    assert evaluate_trigger(limit_sell_row(target=2.0), 2.1)[0] == "fire_sell"
    assert evaluate_trigger(limit_sell_row(target=2.0), 1.9)[0] == "wait"


def test_trailing_stop_tracks_peak_then_fires_on_drop():
    row = trailing_stop_row(distance=20, peak=None)
    assert evaluate_trigger(row, 2.0) == ("update_peak", 2.0)
    assert evaluate_trigger(row, 2.5) == ("update_peak", 2.5)
    row = dict(row, peak_price_usd=2.5)  # the monitor persists the peak between ticks
    assert evaluate_trigger(row, 2.2)[0] == "wait"  # 12% below peak — inside the band
    assert evaluate_trigger(row, 1.99)[0] == "fire_sell"  # 20.4% below peak 2.5
    assert evaluate_trigger(trailing_stop_row(distance=20, peak=None), 0)[0] == "wait"


def test_missing_price_waits():
    assert evaluate_trigger(limit_buy_row(), None) == ("wait", None)
    assert evaluate_trigger(limit_buy_row(), 0) == ("wait", None)


# ---------------------------------------------------------------------------
# DB layer (real sqlite with the production schema)
# ---------------------------------------------------------------------------

async def test_trigger_db_roundtrip():
    db = make_db()
    make_user(db)
    trigger_id = await db.add_trade_trigger(
        telegram_id="7", kind="limit_buy", mint=MINT, target_price_usd=0.42, amount_sol=0.05
    )
    assert trigger_id > 0
    rows = await db.open_triggers()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "limit_buy" and row["target_price_usd"] == 0.42 and row["wallet_address"] == "W"
    await db.update_trigger_peak(trigger_id, 1.23)
    rows = await db.open_triggers()
    assert float(rows[0]["peak_price_usd"]) == 1.23
    await db.update_trigger_distance(trigger_id, "7", 20.0)  # wrong kind → refused
    assert not await db.update_trigger_distance(trigger_id, "7", 20.0)
    assert await db.cancel_trigger(trigger_id, "7")
    assert await db.open_triggers() == []
    listed = await db.triggers_for("7")
    assert listed[0]["status"] == "cancelled"


async def test_open_triggers_excludes_paused_users():
    db = make_db()
    make_user(db)
    await db.add_trade_trigger(telegram_id="7", kind="limit_buy", mint=MINT, target_price_usd=0.4, amount_sol=0.05)
    await db.execute("UPDATE users SET trading_paused=1 WHERE telegram_id='7'")
    assert await db.open_triggers() == []


OLD_TRADES_COLUMNS = """
    id TEXT PRIMARY KEY,
    telegram_id TEXT NOT NULL,
    chat_id TEXT,
    side TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    destination TEXT,
    amount_in_raw TEXT,
    amount_out_raw TEXT,
    amount_sol REAL,
    slippage_bps INTEGER,
    price_impact_pct REAL,
    signature TEXT,
    status TEXT NOT NULL,
    error TEXT,
    source TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    fee_lamports INTEGER,
    sol_usd REAL,
    realized_pnl_sol REAL,
    request_id TEXT,
    claim_token TEXT,
    position_applied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
"""


async def test_schema_migration_adds_fee_column_to_old_deployments():
    db = Database.connect_sqlite(":memory:")
    # Simulate a deployment created before the column existed.
    db.execute_sync(f"CREATE TABLE trades ({OLD_TRADES_COLUMNS})")
    db.execute_sync(
        "INSERT INTO trades (id, telegram_id, side, token_mint, status, source, idempotency_key, created_at, updated_at) "
        "VALUES ('old', '7', 'buy', 'MINTOLD', 'confirmed', 'manual', 'k0', 'x', 'x')"
    )
    db.apply_schema()  # adds missing columns/tables idempotently
    await db.insert_trade({
        "id": "t1", "telegram_id": "7", "chat_id": "7", "side": "buy", "token_mint": MINT,
        "amount_sol": 0.05, "slippage_bps": 300, "status": "pending", "source": "trigger",
        "idempotency_key": "k1", "priority_fee_lamports": 50_000, "created_at": "x", "updated_at": "x",
    })
    trade = await db.get_trade("t1")
    assert int(trade["priority_fee_lamports"]) == 50_000
    db.apply_schema()  # second run must be a no-op, not a duplicate-column error


# ---------------------------------------------------------------------------
# TriggerMonitor tick
# ---------------------------------------------------------------------------

class FakeMonitorDb:
    def __init__(self, rows, paused=False):
        self.rows = rows
        self.paused = paused
        self.peaks: dict[int, float] = {}
        self.statuses: dict[int, tuple] = {}

    async def get_state(self, key):
        return "1" if self.paused else None

    async def open_triggers(self):
        return self.rows

    async def update_trigger_peak(self, trigger_id, peak):
        self.peaks[trigger_id] = peak

    async def set_trigger_status(self, trigger_id, status, *, error=None, trade_id=None):
        self.statuses[trigger_id] = (status, error, trade_id)


class FakeMonitorPrices:
    def __init__(self, prices):
        self.prices = prices

    async def usd_prices(self, mints):
        return {m: self.prices.get(m, 0) for m in mints}


class FakeMonitorQueue:
    def __init__(self):
        self.buys: list[int] = []
        self.sells: list[int] = []
        self.notes: list[str] = []

    async def enqueue_trigger_buy(self, row):
        self.buys.append(row["id"])
        return "t1", None

    async def enqueue_trigger_sell(self, row):
        self.sells.append(row["id"])
        return "t2", None

    async def notify(self, chat_id, text):
        self.notes.append(text)


def make_monitor(rows, prices, paused=False):
    cfg = type("C", (), {"is_mainnet": True, "trigger_poll_seconds": 15})()
    db = FakeMonitorDb(rows, paused)
    queue = FakeMonitorQueue()
    monitor = TriggerMonitor(cfg, db, queue, FakeMonitorPrices(prices), {})
    return monitor, db, queue


async def test_monitor_fires_limit_buy():
    monitor, db, queue = make_monitor([limit_buy_row(target=0.5)], {MINT: 0.45})
    await monitor._tick()
    assert queue.buys == [1]
    assert db.statuses[1][0] == "triggered" and db.statuses[1][2] == "t1"
    metrics = monitor.metrics
    assert metrics["trigger_fires"] == 1 and metrics["trigger_ticks"] == 1


async def test_monitor_trailing_stop_updates_peak_then_fires():
    row = trailing_stop_row(distance=20, peak=None)
    monitor, db, queue = make_monitor([row], {MINT: 2.0})
    await monitor._tick()
    assert db.peaks[2] == 2.0 and not queue.sells
    row = dict(row, peak_price_usd=2.5)
    monitor2, db2, queue2 = make_monitor([row], {MINT: 2.2})
    await monitor2._tick()
    assert not queue2.sells  # inside the band
    monitor3, db3, queue3 = make_monitor([dict(row)], {MINT: 1.99})
    await monitor3._tick()
    assert queue3.sells == [2] and db3.statuses[2][0] == "triggered"


async def test_monitor_skips_when_no_price_or_paused_or_empty():
    monitor, db, queue = make_monitor([limit_buy_row()], {})
    await monitor._tick()
    assert not queue.buys and not db.statuses
    monitor2, db2, queue2 = make_monitor([limit_buy_row()], {MINT: 0.1}, paused=True)
    await monitor2._tick()
    assert not queue2.buys and monitor2.metrics == {}
    monitor3, _, queue3 = make_monitor([], {MINT: 0.1})
    await monitor3._tick()
    assert not queue3.buys and monitor3.metrics == {}


async def test_monitor_marks_failed_when_queue_refuses():
    cfg = type("C", (), {"is_mainnet": True, "trigger_poll_seconds": 15})()

    class RefusingQueue(FakeMonitorQueue):
        async def enqueue_trigger_buy(self, row):
            return None, "daily limit reached"

    db = FakeMonitorDb([limit_buy_row()], False)
    queue = RefusingQueue()
    monitor = TriggerMonitor(cfg, db, queue, FakeMonitorPrices({MINT: 0.1}), {})
    await monitor._tick()
    assert db.statuses[1] == ("failed", "daily limit reached", None)
    assert queue.notes and "disarmed" in queue.notes[0]


# ---------------------------------------------------------------------------
# TradeQueue trigger execution paths
# ---------------------------------------------------------------------------

class QueueDb:
    def __init__(self):
        self.inserted: list[dict] = []

    async def find_idempotency(self, key):
        for row in self.inserted:
            if row["idempotency_key"] == key:
                return {"id": row["id"], "status": row["status"]}
        return None

    async def insert_trade(self, row):
        self.inserted.append(row)
        return True

    async def get_state(self, key):
        return None

    async def spent_today(self, telegram_id, day_start):
        return 0.0

    async def trades_since(self, telegram_id, since):
        return 0


class QueueRpc:
    def __init__(self, accounts):
        self.accounts = accounts

    async def get_token_accounts(self, owner):
        return self.accounts


class QueueCfg:
    trade_queue_max = 10
    max_trades_per_hour = 20
    absolute_max_sol = 100.0


def make_queue(accounts):
    metrics = {}
    queue = TradeQueue(QueueCfg(), QueueDb(), None, QueueRpc(accounts), None, None, metrics)
    return queue, metrics


async def test_enqueue_trigger_sell_without_holdings_refuses():
    queue, _ = make_queue([])
    trade_id, reason = await queue.enqueue_trigger_sell(trailing_stop_row(peak=2.0))
    assert trade_id is None and reason == "no holdings"


async def test_enqueue_trigger_sell_queues_percent_of_holding():
    queue, metrics = make_queue([{"mint": MINT, "amount_raw": 2_000_000, "decimals": 6}])
    row = trailing_stop_row(distance=20, peak=2.0, percent=50)
    trade_id, reason = await queue.enqueue_trigger_sell(row)
    assert trade_id and reason is None
    inserted = queue.db.inserted[0]
    assert inserted["side"] == "sell" and inserted["amount_in_raw"] == "1000000"
    assert inserted["source"] == "trigger" and inserted["idempotency_key"] == "trigger:2"
    assert metrics["triggers_enqueued"] == 1
    trade_id2, reason2 = await queue.enqueue_trigger_sell(row)
    assert trade_id2 is None and reason2 == "already"  # idempotency key already used


async def test_enqueue_trigger_buy_queues_amount_with_overrides():
    queue, metrics = make_queue([])
    row = limit_buy_row(target=0.5, amount=0.05)
    trade_id, reason = await queue.enqueue_trigger_buy(row)
    assert trade_id and reason is None
    inserted = queue.db.inserted[0]
    assert inserted["side"] == "buy" and inserted["amount_sol"] == 0.05
    assert inserted["slippage_bps"] == 500 and inserted["priority_fee_lamports"] == 100000
    assert metrics["triggers_enqueued"] == 1
