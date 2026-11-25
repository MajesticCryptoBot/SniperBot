"""Tests for Automatic Trading: the settings switch, the paste-buy gate,
the post-buy auto-arm hook, and the guide copy."""

from __future__ import annotations

import pytest

from sniper.auto_trading import auto_arm_after_buy, auto_buy_allowed, armed_message
from sniper.db import Database
from sniper.texts import HELP, HELP_SECTIONS

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


# ---------------------------------------------------------------------------
# Settings column: schema, migration, and the guarded update path
# ---------------------------------------------------------------------------


def test_auto_trading_column_exists_with_default():
    db = make_db()
    make_user(db)
    settings = db.fetchone_sync("SELECT auto_trading_enabled FROM settings WHERE telegram_id=?", ("7",))
    assert settings is not None and int(settings["auto_trading_enabled"]) == 0


def test_migration_readds_the_column_on_legacy_databases():
    db = make_db()
    make_user(db)
    db.execute_sync("ALTER TABLE settings DROP COLUMN auto_trading_enabled")
    assert "auto_trading_enabled" not in db._table_columns_sync("settings")
    db._migrate_added_columns_sync()
    assert "auto_trading_enabled" in db._table_columns_sync("settings")
    row = db.fetchone_sync("SELECT auto_trading_enabled FROM settings LIMIT 1")
    assert row is not None and int(row["auto_trading_enabled"]) == 0


def test_auto_buy_absorption_migrates_legacy_users():
    """One paste pipeline, one switch: legacy auto-buy users become
    Automatic Trading users on migration, and a later boot never
    resurrects a switch the user turned off."""
    db = make_db()
    make_user(db)
    db.execute_sync("UPDATE settings SET auto_buy_enabled=1 WHERE telegram_id='7'")
    db._migrate_added_columns_sync()
    row = db.fetchone_sync("SELECT auto_buy_enabled, auto_trading_enabled FROM settings WHERE telegram_id='7'")
    assert int(row["auto_trading_enabled"]) == 1 and int(row["auto_buy_enabled"]) == 1
    db.execute_sync("UPDATE settings SET auto_buy_enabled=0, auto_trading_enabled=0 WHERE telegram_id='7'")
    db._migrate_added_columns_sync()
    row = db.fetchone_sync("SELECT auto_trading_enabled FROM settings WHERE telegram_id='7'")
    assert int(row["auto_trading_enabled"]) == 0


async def test_update_setting_accepts_the_switch():
    db = make_db()
    make_user(db)
    await db.update_setting("7", "auto_trading_enabled", 1)
    row = db.fetchone_sync("SELECT auto_trading_enabled FROM settings WHERE telegram_id=?", ("7",))
    assert int(row["auto_trading_enabled"]) == 1
    with pytest.raises(ValueError):
        await db.update_setting("7", "not_a_column", 1)


# ---------------------------------------------------------------------------
# Paste-buy gate: Automatic Trading implies auto-buy on paste
# ---------------------------------------------------------------------------


def test_gate_closed_when_both_switches_off():
    assert auto_buy_allowed({"auto_buy_enabled": 0, "auto_trading_enabled": 0}) is False
    assert auto_buy_allowed({}) is False
    assert auto_buy_allowed(None) is False


def test_gate_opens_with_either_switch():
    assert auto_buy_allowed({"auto_buy_enabled": 1, "auto_trading_enabled": 0}) is True
    assert auto_buy_allowed({"auto_buy_enabled": 0, "auto_trading_enabled": 1}) is True
    assert auto_buy_allowed({"auto_buy_enabled": 1, "auto_trading_enabled": 1}) is True


def test_balance_shortfall_names_the_real_problem():
    """The production bug: a 0.06 SOL buy on an empty wallet reached
    Jupiter and died as "Quote unavailable: Insufficient funds". The
    queue-side guard must refuse up front and name the real problem."""
    from sniper.auto_trading import balance_shortfall

    reason = balance_shortfall(0, 0.05, 100_000)
    assert reason and "balance" in reason and "Fund the wallet" in reason
    reason = balance_shortfall(50_000_000, 0.06, 100_000)  # 0.05 SOL held: short of 0.06
    assert reason and "0.06" in reason  # the message names the size it could not cover
    assert balance_shortfall(100 * 1_000_000_000, 0.06, 100_000) is None


# ---------------------------------------------------------------------------
# auto_arm_after_buy — the Automatic Trading hook after a confirmed buy
# ---------------------------------------------------------------------------


class ArmDB:
    def __init__(self, settings, user, position):
        self._settings = settings
        self._user = user
        self._position = position
        self.armed_triggers = []
        self.disarmed = []

    async def get_settings(self, telegram_id):
        return self._settings

    async def get_user(self, telegram_id):
        return self._user

    async def position_for(self, telegram_id, mint):
        return self._position

    async def add_smart_trigger(self, telegram_id, mint):
        self.armed_triggers.append((telegram_id, mint))
        return 42

    async def set_position_armed(self, telegram_id, mint, armed):
        self.disarmed.append((telegram_id, mint, armed))


class ArmPrices:
    async def token_profile(self, mint):
        return {"symbol": "TEST"}


TRADE = {
    "id": "t-1",
    "telegram_id": "7",
    "token_mint": MINT,
    "side": "buy",
    "chat_id": "7",
}

POSITION = {
    "telegram_id": "7",
    "token_mint": MINT,
    "amount_raw": 5_000_000,
    "cost_basis_sol": 0.25,
    "chat_id": "7",
    "wallet_address": "W",
}


def arm_harness(db):
    metrics: dict = {}
    notified: list[tuple[int, str]] = []
    return metrics, notified, lambda chat_id, text: notified.append((chat_id, text))


async def test_auto_arm_fires_when_switch_on():
    db = ArmDB({"auto_trading_enabled": 1}, {"trading_paused": 0}, POSITION)
    metrics, notified, notify = arm_harness(db)
    await auto_arm_after_buy(db, ArmPrices(), metrics, notify, TRADE)
    assert db.armed_triggers == [("7", MINT)]
    assert db.disarmed and db.disarmed[0][2] is False
    assert notified and "SMART TRAILING ARMED" in notified[0][1]
    assert "TEST" in notified[0][1]
    assert metrics.get("smart_auto_armed") == 1


async def test_no_auto_arm_when_switch_off():
    db = ArmDB({"auto_trading_enabled": 0}, {"trading_paused": 0}, POSITION)
    metrics, notified, notify = arm_harness(db)
    await auto_arm_after_buy(db, ArmPrices(), metrics, notify, TRADE)
    assert not db.armed_triggers and not db.disarmed and not notified


async def test_no_auto_arm_when_paused():
    db = ArmDB({"auto_trading_enabled": 1}, {"trading_paused": 1}, POSITION)
    metrics, notified, notify = arm_harness(db)
    await auto_arm_after_buy(db, ArmPrices(), metrics, notify, TRADE)
    assert not db.armed_triggers


async def test_no_auto_arm_without_position():
    db = ArmDB({"auto_trading_enabled": 1}, {"trading_paused": 0}, None)
    metrics, notified, notify = arm_harness(db)
    await auto_arm_after_buy(db, ArmPrices(), metrics, notify, TRADE)
    assert not db.armed_triggers
    empty = ArmDB({"auto_trading_enabled": 1}, {"trading_paused": 0}, {**POSITION, "amount_raw": 0})
    await auto_arm_after_buy(empty, ArmPrices(), {}, lambda c, t: None, TRADE)
    assert not empty.armed_triggers


async def test_auto_arm_failure_never_raises():
    class BrokenDB(ArmDB):
        async def add_smart_trigger(self, telegram_id, mint):
            raise RuntimeError("db gone")

    db = BrokenDB({"auto_trading_enabled": 1}, {"trading_paused": 0}, POSITION)
    await auto_arm_after_buy(db, ArmPrices(), {}, lambda c, t: None, TRADE)  # must swallow


async def test_armed_message_renders():
    text = await armed_message(ArmPrices(), MINT, 42)
    assert "SMART TRAILING ARMED" in text
    assert "Order #42" in text
    assert "Phase RUN (1/6)" in text
    assert MINT[:6] in text


# ---------------------------------------------------------------------------
# Guide copy
# ---------------------------------------------------------------------------


def test_help_has_auto_trading():
    assert "/auto" in HELP and "Automatic Trading" in HELP
    section = HELP_SECTIONS["auto"]
    for word in ("AUTOMATIC TRADING", "Paste", "Scan", "Buy", "Trail", "Exit", "Smart Trailing"):
        assert word in section
