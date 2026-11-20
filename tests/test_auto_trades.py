"""Tests for the two automation features:

- Auto-buy on paste (Maestro-style): with the switch ON, every detected
  LIVE token is bought instantly with the preset amount — no panel.
- TP/SL auto-sell (Maestro-style): every position bought through the bot
  is auto-sold in full when price crosses +take_profit% / -stop_loss%
  against the averaged entry."""

from __future__ import annotations

import pytest

from sniper.db import Database
from sniper.errors import UserFacingError
from sniper.handlers.token_panel import detect_from_text
from sniper.positions_monitor import PositionMonitor, evaluate_position
from sniper.settings_logic import AmountError, validate_setting
from sniper.solana_service import WSOL
from sniper.trade_queue import TradeQueue

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


def seed_position(db, *, raw=5_000_000, cost=1.0, tp_sl_on=True, paused=False):
    """One user with a bought position: 5 tokens (6 decimals) for 1 SOL."""
    make_user(db)
    db._add_buy_position("7", MINT, raw, cost, "2026-01-01T00:00:00Z")
    if tp_sl_on:
        db.execute_sync("UPDATE settings SET tp_sl_enabled=1 WHERE telegram_id='7'", ())
    if paused:
        db.execute_sync("UPDATE users SET trading_paused=1 WHERE telegram_id='7'", ())
    return db


# ---------------------------------------------------------------------------
# evaluate_position — pure decision logic
# ---------------------------------------------------------------------------

POS = {"tokens_whole": 5.0, "entry_sol_per_token": 0.2}


def test_take_profit_fires_at_or_above_level():
    assert evaluate_position(POS, 30.0, 100.0, 50, 25)[0] == "take_profit"  # 0.3 SOL/token = +50%
    assert evaluate_position(POS, 36.0, 100.0, 50, 25)[0] == "take_profit"  # +80%


def test_stop_loss_fires_at_or_below_level():
    assert evaluate_position(POS, 15.0, 100.0, 50, 25)[0] == "stop_loss"  # 0.15 = −25%
    assert evaluate_position(POS, 10.0, 100.0, 50, 25)[0] == "stop_loss"  # −50%


def test_flat_price_waits():
    assert evaluate_position(POS, 24.0, 100.0, 50, 25) == ("wait", None)  # +20%, inside both bands


def test_stop_loss_wins_when_both_levels_cross_in_one_tick():
    decision, pnl = evaluate_position(POS, 2.0, 100.0, 50, 25)  # −90% vs entry
    assert decision == "stop_loss" and pnl == pytest.approx(-90.0)


def test_disabled_levels_never_fire():
    assert evaluate_position(POS, 36.0, 100.0, 0, 25) == ("wait", None)
    assert evaluate_position(POS, 2.0, 100.0, 50, 0) == ("wait", None)


def test_bad_inputs_wait():
    assert evaluate_position(POS, None, 100.0, 50, 25) == ("wait", None)
    assert evaluate_position(POS, 30.0, None, 50, 25) == ("wait", None)
    assert evaluate_position(POS, 30.0, 0.0, 50, 25) == ("wait", None)
    assert evaluate_position({"tokens_whole": 0, "entry_sol_per_token": 0.2}, 30.0, 100.0, 50, 25) == ("wait", None)


# ---------------------------------------------------------------------------
# DB layer — real sqlite with the production schema
# ---------------------------------------------------------------------------


async def test_new_settings_columns_and_defaults():
    db = make_db()
    make_user(db)
    settings = await db.get_settings("7")
    for column in ("auto_buy_enabled", "auto_buy_amount_sol", "tp_sl_enabled", "take_profit_pct", "stop_loss_pct"):
        assert column in settings, column
    assert int(settings["auto_buy_enabled"]) == 0
    assert float(settings["auto_buy_amount_sol"]) == 0.05
    assert int(settings["tp_sl_enabled"]) == 0
    assert float(settings["take_profit_pct"]) == 50
    assert float(settings["stop_loss_pct"]) == 25


async def test_position_buy_creates_and_a_rebuy_rearms():
    db = make_db()
    make_user(db)
    db._add_buy_position("7", MINT, 5_000_000, 1.0, "t0")
    row = (await db.list_positions("7"))[0]
    assert int(row["amount_raw"]) == 5_000_000 and float(row["cost_basis_sol"]) == 1.0 and int(row["armed"]) == 1
    await db.set_position_armed("7", MINT, False)
    assert int((await db.list_positions("7"))[0]["armed"]) == 0
    db._add_buy_position("7", MINT, 5_000_000, 1.0, "t1")  # a new buy re-arms TP/SL
    row = (await db.list_positions("7"))[0]
    assert int(row["armed"]) == 1 and int(row["amount_raw"]) == 10_000_000 and float(row["cost_basis_sol"]) == 2.0


async def test_positions_for_tp_sl_filters():
    db = seed_position(make_db(), tp_sl_on=False)
    assert await db.positions_for_tp_sl() == []  # switch is off by default
    await db.update_setting("7", "tp_sl_enabled", 1)
    rows = await db.positions_for_tp_sl()
    assert len(rows) == 1
    assert float(rows[0]["take_profit_pct"]) == 50 and float(rows[0]["stop_loss_pct"]) == 25
    assert rows[0]["wallet_address"] == "W" and rows[0]["chat_id"] == "7"
    await db.set_position_armed("7", MINT, False)
    assert await db.positions_for_tp_sl() == []  # fired positions are not re-checked
    await db.set_position_armed("7", MINT, True)
    await db.execute("UPDATE users SET trading_paused=1 WHERE telegram_id='7'", ())
    assert await db.positions_for_tp_sl() == []  # paused users keep their levels armed


async def test_stop_switches_off_automation():
    db = make_db()
    make_user(db)
    await db.update_setting("7", "auto_buy_enabled", 1)
    await db.update_setting("7", "tp_sl_enabled", 1)
    await db.set_paused("7", True)
    settings = await db.get_settings("7")
    assert int(settings["auto_buy_enabled"]) == 0  # /stop kills the instant-buy switch
    assert int(settings["tp_sl_enabled"]) == 1  # levels stay configured; paused users are skipped


# ---------------------------------------------------------------------------
# PositionMonitor tick — fake queue, real db
# ---------------------------------------------------------------------------


class MonitorQueue:
    def __init__(self):
        self.calls: list[tuple] = []
        self.notifications: list[str] = []

    async def enqueue_auto_sell(self, row, kind, pnl_pct, price_usd):
        self.calls.append((row["token_mint"], kind, pnl_pct, price_usd))
        return "trade-tpsl", None

    async def notify(self, chat_id, text):
        self.notifications.append(text)


class MonitorRpc:
    def __init__(self, decimals):
        self.decimals = decimals

    async def get_mint_info(self, mint):
        if mint in self.decimals:
            return {"decimals": self.decimals[mint]}
        raise UserFacingError("not found")


class MonitorPrices:
    def __init__(self, data):
        self.data = data

    async def usd_prices(self, mints):
        return {mint: self.data.get(mint, 0) for mint in mints}


def make_monitor(db, prices, queue=None, decimals=None):
    cfg = type("Cfg", (), {"is_mainnet": True, "trigger_poll_seconds": 60})()
    return PositionMonitor(
        cfg, db, queue or MonitorQueue(), prices, MonitorRpc(decimals or {MINT: 6}), {}
    )


async def test_monitor_fires_take_profit_and_disarms():
    db = seed_position(make_db())  # entry 0.2 SOL/token
    queue = MonitorQueue()
    monitor = make_monitor(db, MonitorPrices({MINT: 30.0, WSOL: 100.0}), queue)  # +50%
    await monitor._tick()
    assert len(queue.calls) == 1
    mint, kind, pnl, price = queue.calls[0]
    assert mint == MINT and kind == "take_profit" and pnl == pytest.approx(50.0) and price == 30.0
    assert int((await db.list_positions("7"))[0]["armed"]) == 0  # disarmed after firing


async def test_monitor_fires_stop_loss_when_price_collapses():
    db = seed_position(make_db())
    queue = MonitorQueue()
    monitor = make_monitor(db, MonitorPrices({MINT: 5.0, WSOL: 100.0}), queue)  # −75%
    await monitor._tick()
    assert queue.calls[0][1] == "stop_loss"


async def test_monitor_waits_inside_the_bands():
    db = seed_position(make_db())
    queue = MonitorQueue()
    monitor = make_monitor(db, MonitorPrices({MINT: 24.0, WSOL: 100.0}), queue)  # +20%
    await monitor._tick()
    assert queue.calls == []
    assert int((await db.list_positions("7"))[0]["armed"]) == 1


async def test_monitor_skips_paused_users():
    db = seed_position(make_db(), paused=True)
    queue = MonitorQueue()
    monitor = make_monitor(db, MonitorPrices({MINT: 2.0, WSOL: 100.0}), queue)  # deep loss
    await monitor._tick()
    assert queue.calls == []
    assert int((await db.list_positions("7"))[0]["armed"]) == 1  # protection stays armed


async def test_monitor_refires_are_blocked_after_disarm():
    db = seed_position(make_db())
    queue = MonitorQueue()
    monitor = make_monitor(db, MonitorPrices({MINT: 30.0, WSOL: 100.0}), queue)
    await monitor._tick()
    await monitor._tick()  # same prices — the position is disarmed, nothing new
    assert len(queue.calls) == 1


# ---------------------------------------------------------------------------
# TradeQueue.enqueue_auto_buy — spend guards + fresh idempotency
# ---------------------------------------------------------------------------


class AutoBuyDb:
    def __init__(self, *, settings=None, user=None):
        self.inserted: list[dict] = []
        self._settings = settings or {"auto_buy_enabled": 1, "auto_buy_amount_sol": 0.05, "max_sol_per_trade": 0.1, "daily_spend_limit_sol": 0.5}
        self._user = user or {"wallet_address": "W", "trading_paused": 0, "chat_id": "7"}

    async def get_user(self, telegram_id):
        return self._user

    async def get_settings(self, telegram_id):
        return self._settings

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


class QueueCfg:
    trade_queue_max = 10
    max_trades_per_hour = 20
    absolute_max_sol = 100.0


def make_auto_buy_queue(db=None):
    metrics = {}
    return TradeQueue(QueueCfg(), db or AutoBuyDb(), None, None, None, None, metrics), metrics


async def test_enqueue_auto_buy_queues_instant_buy():
    queue, metrics = make_auto_buy_queue()
    trade_id, reason = await queue.enqueue_auto_buy("7", MINT, 0.05)
    assert trade_id and reason is None
    inserted = queue.db.inserted[0]
    assert inserted["side"] == "buy" and inserted["amount_sol"] == 0.05
    assert inserted["source"] == "auto_buy"
    assert inserted["idempotency_key"].startswith(f"autobuy:7:{MINT}:")
    assert metrics["auto_buys_enqueued"] == 1


async def test_enqueue_auto_buy_refuses_when_switch_off():
    queue, _ = make_auto_buy_queue(AutoBuyDb(settings={"auto_buy_enabled": 0, "auto_buy_amount_sol": 0.05, "max_sol_per_trade": 0.1, "daily_spend_limit_sol": 0.5}))
    trade_id, reason = await queue.enqueue_auto_buy("7", MINT, 0.05)
    assert trade_id is None and reason == "auto-buy is off"
    assert queue.db.inserted == []


async def test_enqueue_auto_buy_refuses_when_paused():
    queue, _ = make_auto_buy_queue(AutoBuyDb(user={"wallet_address": "W", "trading_paused": 1, "chat_id": "7"}))
    trade_id, reason = await queue.enqueue_auto_buy("7", MINT, 0.05)
    assert trade_id is None and "paused" in reason


async def test_enqueue_auto_buy_respects_max_sol_per_trade():
    queue, _ = make_auto_buy_queue()
    trade_id, reason = await queue.enqueue_auto_buy("7", MINT, 0.5)  # max is 0.1
    assert trade_id is None and reason
    assert queue.db.inserted == []


# ---------------------------------------------------------------------------
# TradeQueue.enqueue_auto_sell — full position, idempotent per armed level
# ---------------------------------------------------------------------------


def tpsl_row(updated_at="t1"):
    return {
        "telegram_id": "7",
        "token_mint": MINT,
        "amount_raw": "5000000",
        "cost_basis_sol": 1.0,
        "updated_at": updated_at,
        "take_profit_pct": 50,
        "stop_loss_pct": 25,
        "chat_id": "7",
        "wallet_address": "W",
    }


class SellRpc:
    def __init__(self, accounts):
        self.accounts = accounts

    async def get_token_accounts(self, owner):
        return self.accounts


def make_auto_sell_queue(accounts):
    metrics = {}
    return TradeQueue(QueueCfg(), AutoBuyDb(), None, SellRpc(accounts), None, None, metrics), metrics


async def test_enqueue_auto_sell_queues_full_holding():
    queue, metrics = make_auto_sell_queue([{"mint": MINT, "amount_raw": 2_000_000, "decimals": 6}])
    trade_id, reason = await queue.enqueue_auto_sell(tpsl_row(), "take_profit", 55.0, 30.0)
    assert trade_id and reason is None
    inserted = queue.db.inserted[0]
    assert inserted["side"] == "sell" and inserted["amount_in_raw"] == "2000000"
    assert inserted["source"] == "tp_sl"
    assert inserted["idempotency_key"] == f"tpsl:7:{MINT}:take_profit:t1"
    assert metrics["tp_sl_enqueued"] == 1
    trade_id2, reason2 = await queue.enqueue_auto_sell(tpsl_row(), "take_profit", 55.0, 30.0)
    assert trade_id2 is None and reason2 == "already"  # one armed level → one sell


async def test_enqueue_auto_sell_without_holdings_refuses():
    queue, _ = make_auto_sell_queue([])
    trade_id, reason = await queue.enqueue_auto_sell(tpsl_row(), "stop_loss", -30.0, 5.0)
    assert trade_id is None and reason == "no holdings"


# ---------------------------------------------------------------------------
# Detection hook — auto-buy fires on a detected LIVE token
# ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.sent: list[str] = []
        self.markups: list = []

    async def reply_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.sent.append(text)
        self.markups.append(reply_markup)


def make_update(message):
    return type(
        "Upd",
        (),
        {
            "effective_message": message,
            "message": message,
            "effective_user": type("U", (), {"id": 7})(),
            "effective_chat": type("C", (), {"type": "private"})(),
            "callback_query": None,
        },
    )()


class DetectionRpc:
    def __init__(self, freeze=None):
        self.freeze = freeze

    async def get_mint_info(self, address):
        return {"freeze_authority": self.freeze, "mint_authority": None}


class DetectionQueue:
    def __init__(self):
        self.buys: list[tuple] = []

    async def enqueue_auto_buy(self, telegram_id, mint, amount):
        self.buys.append((telegram_id, mint, amount))
        return "trade-auto", None


class FakeSafety:
    def __init__(self, ok=True, blockers=None, warnings=None, fail=False):
        self.ok = ok
        self.blockers = list(blockers or [])
        self.warnings = list(warnings or [])
        self.fail = fail

    async def evaluate(self, mint, liquidity_sol=None, *, min_liquidity_sol=0):
        if self.fail:
            raise RuntimeError("scan down")
        return type(
            "Report",
            (),
            {
                "ok_for_autosnipe": self.ok,
                "blockers": self.blockers,
                "warnings": self.warnings,
            },
        )()


def make_detect_svc(settings, queue, safety=None):
    return type(
        "Svc",
        (),
        {
            "rpc": DetectionRpc(),
            "prices": MonitorPrices({MINT: 0.5}),  # token is LIVE
            "db": AutoBuyDb(settings=settings),
            "queue": queue,
            "safety": safety if safety is not None else FakeSafety(),
        },
    )()


async def test_auto_buy_fires_instead_of_the_panel():
    queue = DetectionQueue()
    message = FakeMessage(text=MINT)
    svc = make_detect_svc({"auto_buy_enabled": 1, "auto_buy_amount_sol": 0.05}, queue)
    handled = await detect_from_text(make_update(message), None, svc, MINT)
    assert handled
    assert queue.buys == [("7", MINT, 0.05)]
    assert any(text.startswith("🤖") and "AUTO-BUY REQUEST QUEUED" in text for text in message.sent)
    assert any("Scan: clear" in text for text in message.sent)
    assert not any("TOKEN DETECTED" in text for text in message.sent)  # no panel — it bought


async def test_auto_buy_blocks_a_flagged_token():
    queue = DetectionQueue()
    message = FakeMessage(text=MINT)
    safety = FakeSafety(ok=False, blockers=["Token is reported as a honeypot or cannot be sold"])
    svc = make_detect_svc({"auto_buy_enabled": 1, "auto_buy_amount_sol": 0.05}, queue, safety)
    handled = await detect_from_text(make_update(message), None, svc, MINT)
    assert handled
    assert queue.buys == []
    assert any("AUTO-BUY BLOCKED" in text and "honeypot" in text for text in message.sent)
    assert any("TOKEN DETECTED" in text for text in message.sent)


async def test_auto_buy_holds_when_the_scan_fails():
    queue = DetectionQueue()
    message = FakeMessage(text=MINT)
    svc = make_detect_svc(
        {"auto_buy_enabled": 1, "auto_buy_amount_sol": 0.05},
        queue,
        FakeSafety(fail=True),
    )
    handled = await detect_from_text(make_update(message), None, svc, MINT)
    assert handled
    assert queue.buys == []
    assert any("AUTO-BUY HELD" in text for text in message.sent)
    assert any("TOKEN DETECTED" in text for text in message.sent)


async def test_auto_buy_off_shows_the_panel():
    queue = DetectionQueue()
    message = FakeMessage(text=MINT)
    svc = make_detect_svc({"auto_buy_enabled": 0, "auto_buy_amount_sol": 0.05}, queue)
    handled = await detect_from_text(make_update(message), None, svc, MINT)
    assert handled
    assert queue.buys == []
    assert "TOKEN DETECTED" in message.sent[0]


async def test_auto_buy_never_touches_frozen_tokens():
    queue = DetectionQueue()
    message = FakeMessage(text=MINT)
    svc = type(
        "Svc",
        (),
        {
            "rpc": DetectionRpc(freeze="SomeAuthority"),
            "prices": MonitorPrices({MINT: 0.5}),
            "db": AutoBuyDb(settings={"auto_buy_enabled": 1, "auto_buy_amount_sol": 0.05}),
            "queue": queue,
        },
    )()
    handled = await detect_from_text(make_update(message), None, svc, MINT)
    assert handled
    assert queue.buys == []  # frozen tokens are never auto-bought
    assert "TOKEN DETECTED" in message.sent[0]
    assert "freeze authority is active" in message.sent[0]  # the panel warns instead


# ---------------------------------------------------------------------------
# New /config settings
# ---------------------------------------------------------------------------


def test_validate_new_settings():
    assert validate_setting("auto_buy_amount", "0.1", absolute_max_sol=100) == ("auto_buy_amount_sol", 0.1)
    assert validate_setting("take_profit", "120", absolute_max_sol=100) == ("take_profit_pct", 120.0)
    assert validate_setting("stop_loss", "25", absolute_max_sol=100) == ("stop_loss_pct", 25.0)


def test_validate_new_settings_caps():
    with pytest.raises(AmountError):
        validate_setting("auto_buy_amount", "200", absolute_max_sol=100)
    with pytest.raises(AmountError):
        validate_setting("take_profit", "5001", absolute_max_sol=100)
    with pytest.raises(AmountError):
        validate_setting("stop_loss", "100", absolute_max_sol=100)
    with pytest.raises(AmountError):
        validate_setting("take_profit", "0", absolute_max_sol=100)
