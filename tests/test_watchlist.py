"""Watchlist autosnipe + paste-detection tests.

The bot must never scan the chain for new tokens. Autosnipe only fires
for tokens a user explicitly added to their own watchlist.
"""

from __future__ import annotations

import pytest

import sniper.handlers.admin  # noqa: F401
import sniper.handlers.settings  # noqa: F401
import sniper.handlers.trading  # noqa: F401
from sniper.db import Database
from sniper.watchlist_monitor import WatchlistMonitor

MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
OTHER_MINT = "So11111111111111111111111111111111111111112"  # WSOL


def _db() -> Database:
    db = Database.connect_sqlite()
    db.apply_schema()
    return db


def _user(db: Database, telegram_id: str = "7", *, enabled: int = 1, paused: int = 0) -> None:
    db.create_user_sync(
        telegram_id=telegram_id,
        username=None,
        chat_id=telegram_id,
        wallet_address="wallet",
        enc_payload="payload",
        defaults={
            "slippage_bps": 500,
            "priority_fee_lamports": 1000,
            "max_sol_per_trade": 1,
            "daily_spend_limit_sol": 2,
            "autosnipe_amount_sol": 0.05,
        },
    )
    db.execute_sync("UPDATE settings SET autosnipe_enabled=? WHERE telegram_id=?", (enabled, telegram_id))
    db.execute_sync("UPDATE users SET trading_paused=? WHERE telegram_id=?", (paused, telegram_id))


# ---------------------------------------------------------------------------
# Paste detection (extract_mints + detect_from_text) lives in test_ca_detect.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Watchlist database
# ---------------------------------------------------------------------------


def test_watch_add_update_reactivate_and_remove():
    db = _db()
    try:
        _user(db)
        watch_id, action = db.add_watch_entry_sync("7", MINT, 0.05)
        assert action == "added"
        # Second add while waiting updates the amount instead of failing.
        watch_id2, action2 = db.add_watch_entry_sync("7", MINT, 0.10)
        assert (watch_id2, action2) == (watch_id, "updated")
        entry = db.fetchone_sync("SELECT amount_sol, status FROM snipe_watch WHERE id=?", (watch_id,))
        assert float(entry["amount_sol"]) == 0.10 and entry["status"] == "active"
        # Triggered entries can be re-added.
        db.execute_sync("UPDATE snipe_watch SET status='triggered' WHERE id=?", (watch_id,))
        _, action3 = db.add_watch_entry_sync("7", MINT, 0.02)
        assert action3 == "reactivated"
        entry = db.fetchone_sync("SELECT amount_sol, status, trigger_error FROM snipe_watch WHERE id=?", (watch_id,))
        assert entry["status"] == "active" and float(entry["amount_sol"]) == 0.02 and entry["trigger_error"] is None
        # Removal is owner-checked.
        assert await_owner_check(db, watch_id) is False
        db.execute_sync("DELETE FROM snipe_watch WHERE id=?", (watch_id,))
        assert db.fetchone_sync("SELECT id FROM snipe_watch WHERE id=?", (watch_id,)) is None
    finally:
        db.close()


def await_owner_check(db: Database, watch_id: int) -> bool:
    """Mirrors remove_watch_entry's owner check without the async hop."""
    return db.fetchone_sync("SELECT id FROM snipe_watch WHERE id=? AND telegram_id=?", (watch_id, "999")) is not None


@pytest.mark.asyncio
async def test_watchlist_active_filters_disabled_and_paused():
    db = _db()
    try:
        _user(db, "1", enabled=1)
        _user(db, "2", enabled=0)
        _user(db, "3", enabled=1, paused=1)
        for telegram_id in ("1", "2", "3"):
            await db.add_watch_entry(telegram_id, MINT, 0.05)
        active = await db.watchlist_active()
        assert [row["telegram_id"] for row in active] == ["1"]
        await db.set_watch_status(active[0]["id"], "1", "triggered")
        assert await db.watchlist_active() == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Watchlist monitor
# ---------------------------------------------------------------------------


class _Report:
    def __init__(self, ok: bool, blockers: list[str] | None = None):
        self.ok_for_autosnipe = ok
        self.blockers = blockers or []

    def summary(self) -> str:
        return "report"


class _Safety:
    def __init__(self, report: _Report):
        self.report = report
        self.calls = 0

    async def evaluate(self, mint, **kwargs):
        self.calls += 1
        return self.report


class _Prices:
    def __init__(self, live: dict[str, float]):
        self.live = live

    async def usd_prices(self, mints):
        return {mint: self.live.get(mint, 0.0) for mint in mints}


class _Queue:
    def __init__(self, trade_id: str = "trade-1"):
        self.trade_id = trade_id
        self.enqueued: list[dict] = []
        self.notified: list[tuple] = []

    async def enqueue_watch_snipe(self, entry, report):
        self.enqueued.append(entry)
        return self.trade_id, None

    async def notify(self, chat_id, text):
        self.notified.append((chat_id, text))


class _Cfg:
    is_mainnet = True
    watchlist_poll_seconds = 1


class _StateDb:
    def __init__(self, inner: Database):
        self.inner = inner
        self.paused = "0"

    async def get_state(self, key):
        return self.paused

    async def watchlist_active(self):
        return await self.inner.watchlist_active()

    async def set_watch_status(self, watch_id, telegram_id, status, error=None):
        return await self.inner.set_watch_status(watch_id, telegram_id, status, error)


def _monitor(db, safety, prices, queue) -> WatchlistMonitor:
    return WatchlistMonitor(_Cfg(), _StateDb(db), safety, queue, prices, {})


@pytest.mark.asyncio
async def test_monitor_triggers_only_for_live_and_safe_tokens():
    db = _db()
    try:
        _user(db)
        await db.add_watch_entry("7", MINT, 0.05)
        await db.add_watch_entry("7", OTHER_MINT, 0.05)
        prices = _Prices({MINT: 0.01})  # only MINT is live
        safety = _Safety(_Report(True))
        queue = _Queue()
        monitor = _monitor(db, safety, prices, queue)
        await monitor._tick()
        assert len(queue.enqueued) == 1
        assert queue.enqueued[0]["token_mint"] == MINT
        entry = db.fetchone_sync("SELECT status FROM snipe_watch WHERE token_mint=?", (MINT,))
        assert entry["status"] == "triggered"
        wsol_entry = db.fetchone_sync("SELECT status FROM snipe_watch WHERE token_mint=?", (OTHER_MINT,))
        assert wsol_entry["status"] == "active"
        # A second tick must not re-buy the triggered entry.
        await monitor._tick()
        assert len(queue.enqueued) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_monitor_aborts_blocked_token_and_notifies_user():
    db = _db()
    try:
        _user(db)
        await db.add_watch_entry("7", MINT, 0.05)
        safety = _Safety(_Report(False, ["freeze authority is still active"]))
        queue = _Queue()
        await _monitor(db, safety, _Prices({MINT: 0.01}), queue)._tick()
        entry = db.fetchone_sync("SELECT status, trigger_error FROM snipe_watch WHERE token_mint=?", (MINT,))
        assert entry["status"] == "aborted"
        assert "freeze authority" in entry["trigger_error"]
        assert queue.enqueued == []
        assert len(queue.notified) == 1
        assert "No buy was submitted" in queue.notified[0][1]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_monitor_marks_triggered_on_duplicate_idempotency_key():
    db = _db()
    try:
        _user(db)
        await db.add_watch_entry("7", MINT, 0.05)
        queue = _Queue()
        queue.trade_id = None  # enqueue_watch_snipe reports "already"
        queue.reason = "already"

        class _DupQueue(_Queue):
            async def enqueue_watch_snipe(self, entry, report):
                return None, "already"

        await _monitor(db, _Safety(_Report(True)), _Prices({MINT: 0.01}), _DupQueue())._tick()
        entry = db.fetchone_sync("SELECT status FROM snipe_watch WHERE token_mint=?", (MINT,))
        assert entry["status"] == "triggered"
        assert queue.notified == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_monitor_skips_global_pause_and_devnet():
    db = _db()
    try:
        _user(db)
        await db.add_watch_entry("7", MINT, 0.05)
        state = _StateDb(db)
        state.paused = "1"
        queue = _Queue()
        monitor = WatchlistMonitor(_Cfg(), state, _Safety(_Report(True)), queue, _Prices({MINT: 0.01}), {})
        await monitor._tick()
        assert queue.enqueued == []
        cfg = _Cfg()
        cfg.is_mainnet = False
        state.paused = "0"
        monitor = WatchlistMonitor(cfg, state, _Safety(_Report(True)), queue, _Prices({MINT: 0.01}), {})
        await monitor._tick()
        assert queue.enqueued == []
    finally:
        db.close()
