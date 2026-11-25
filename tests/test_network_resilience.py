"""Network-interruption resilience: queued trades survive an outage.

Covers the recovery paths for "the internet dropped mid-trade": endless
transient-error retry with capped backoff (never gives up until the network
is back), restart resume of queued trades, reconcile pickup of orphaned
retry rows, and the notification outbox that replays messages sent while
Telegram was unreachable.
"""

import asyncio
import base64
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from sniper.crypto import EnvelopeCipher
from sniper.db import Database
from sniper.jupiter import ExecuteResult, SwapOrder
from sniper.trade_queue import TradeQueue
from sniper.timeutil import utcnow


class _Cfg:
    is_mainnet = True
    network = "mainnet-beta"
    absolute_max_sol = 100
    max_trades_per_hour = 20
    trade_queue_max = 10
    max_concurrent_swaps = 1


class FlakyJupiter:
    """order() raises a transport error until .ok is flipped True."""

    def __init__(self, transaction: str):
        self.transaction = transaction
        self.ok = False
        self.executed = 0

    async def order(self, **kwargs):
        if not self.ok:
            raise httpx.ConnectError("network unreachable")
        return SwapOrder(
            request_id="req",
            transaction=self.transaction,
            in_amount=int(kwargs["amount"]),
            out_amount=4242,
            other_amount_threshold=4000,
            price_impact_pct=1.5,
            slippage_bps=int(kwargs["slippage_bps"]),
            in_usd=10.0,
            out_usd=9.9,
            route="metis",
        )

    async def execute(self, signed_b64, request_id):
        self.executed += 1
        return ExecuteResult(True, "sig-from-jupiter", None, int(1e8), 4242)


class FakeRpc:
    async def confirm(self, signature, timeout_s=45):
        return True, None

    async def transaction_fee(self, signature):
        return 5000

    async def get_token_accounts(self, pubkey):
        return []

    async def get_balance_lamports(self, pubkey):
        return 2_000_000_000


class FlakyBot:
    """send_message raises while .fail is set, records afterwards."""

    def __init__(self):
        self.fail = True
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        if self.fail:
            raise httpx.ConnectError("telegram unreachable")
        self.sent.append((int(chat_id), text))


def _unsigned_tx(keypair: Keypair) -> str:
    dest = Keypair().pubkey()
    ix = transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=dest, lamports=10))
    message = MessageV0.try_compile(keypair.pubkey(), [ix], [], Hash.default())
    blank = VersionedTransaction.populate(message, [Signature.default()])
    return base64.b64encode(bytes(blank)).decode()


def _stamp(minutes_ago: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _insert_trade(db: Database, trade_id: str, *, minutes_ago: float = 0.0, updated_minutes_ago: float | None = None) -> None:
    inserted = await db.insert_trade(
        {
            "id": trade_id,
            "telegram_id": "7",
            "chat_id": "7",
            "side": "buy",
            "token_mint": "Mint111111111111111111111111111111111111111",
            "destination": None,
            "amount_in_raw": None,
            "amount_out_raw": None,
            "amount_sol": 0.1,
            "slippage_bps": None,
            "price_impact_pct": None,
            "signature": None,
            "status": "pending",
            "error": None,
            "source": "manual",
            "idempotency_key": f"preview-{trade_id}",
            "fee_lamports": None,
            "sol_usd": None,
            "realized_pnl_sol": None,
            "request_id": None,
            "claim_token": None,
            "priority_fee_lamports": None,
            "created_at": _stamp(minutes_ago),
            "updated_at": _stamp(updated_minutes_ago if updated_minutes_ago is not None else minutes_ago),
        }
    )
    assert inserted


_DEFAULTS = {
    "slippage_bps": 100,
    "priority_fee_lamports": 1000,
    "max_sol_per_trade": 1,
    "daily_spend_limit_sol": 2,
    "autosnipe_amount_sol": 0.05,
}


@pytest.mark.asyncio
async def test_transient_outage_requeues_then_trade_completes(monkeypatch):
    """Internet drops while quoting: trade waits, retries, then completes."""
    monkeypatch.setattr("sniper.trade_queue.TRADE_RETRY_BASE_DELAY_S", 0.01)
    keypair = Keypair()
    cipher = EnvelopeCipher(os.urandom(32))
    db = Database.connect_sqlite()
    db.apply_schema()
    db.create_user_sync(
        telegram_id="7",
        username="bob",
        chat_id="7",
        wallet_address=str(keypair.pubkey()),
        enc_payload=cipher.encrypt_json(bytes(keypair)),
        defaults=_DEFAULTS,
    )
    jupiter = FlakyJupiter(_unsigned_tx(keypair))
    bot = FlakyBot()
    bot.fail = False
    queue = TradeQueue(_Cfg(), db, cipher, FakeRpc(), jupiter, None, {})
    queue.bot = bot
    await _insert_trade(db, "trade-1")

    await queue.process_trade("trade-1")
    stored = await db.get_trade("trade-1")
    assert stored["status"] == "pending"  # NOT failed
    assert stored["attempts"] == 1
    assert "Network hiccup" in stored["error"]
    assert jupiter.executed == 0
    assert any("returned to the execution queue" in text for _, text in bot.sent)

    # The network is back: the backoff timer fires and the trade completes.
    jupiter.ok = True
    await asyncio.sleep(0.05)  # shrink the delay below, let the timer fire
    assert queue.queue.qsize() == 1
    trade_id = queue.queue.get_nowait()
    assert trade_id == "trade-1"
    await queue.process_trade(trade_id)
    stored = await db.get_trade("trade-1")
    assert stored["status"] == "confirmed"
    assert stored["signature"] == "sig-from-jupiter"
    assert jupiter.executed == 1
    db.close()


@pytest.mark.asyncio
async def test_retry_never_gives_up_and_backoff_caps(monkeypatch):
    """Even after 99 failures the trade keeps retrying — no attempt cap.

    The requeue timer must fire promptly despite attempts=99, which proves
    the backoff is held at the ceiling instead of growing forever."""
    monkeypatch.setattr("sniper.trade_queue.TRADE_RETRY_BASE_DELAY_S", 0.01)
    monkeypatch.setattr("sniper.trade_queue.TRADE_RETRY_MAX_DELAY_S", 0.05)
    keypair = Keypair()
    cipher = EnvelopeCipher(os.urandom(32))
    db = Database.connect_sqlite()
    db.apply_schema()
    db.create_user_sync(
        telegram_id="7",
        username="bob",
        chat_id="7",
        wallet_address=str(keypair.pubkey()),
        enc_payload=cipher.encrypt_json(bytes(keypair)),
        defaults=_DEFAULTS,
    )
    jupiter = FlakyJupiter(_unsigned_tx(keypair))
    bot = FlakyBot()
    bot.fail = False
    queue = TradeQueue(_Cfg(), db, cipher, FakeRpc(), jupiter, None, {})
    queue.bot = bot
    await _insert_trade(db, "trade-1")
    await db.update_trade_fields("trade-1", attempts=99)

    await queue.process_trade("trade-1")
    stored = await db.get_trade("trade-1")
    assert stored["status"] == "pending"  # still alive, NOT failed
    assert int(stored["attempts"]) == 100
    assert any("NETWORK INTERRUPTION" in text for _, text in bot.sent)

    # Network restored: the capped timer fires and the trade completes.
    jupiter.ok = True
    await asyncio.sleep(0.1)
    assert queue.queue.qsize() == 1
    await queue.process_trade(queue.queue.get_nowait())
    stored = await db.get_trade("trade-1")
    assert stored["status"] == "confirmed"
    assert jupiter.executed == 1
    db.close()


@pytest.mark.asyncio
async def test_retry_notifications_do_not_flood(monkeypatch):
    """11 failed attempts → 4 notifications (retries 1, 2, 3, then every 10th)."""
    monkeypatch.setattr("sniper.trade_queue.TRADE_RETRY_BASE_DELAY_S", 0.01)
    monkeypatch.setattr("sniper.trade_queue.TRADE_RETRY_MAX_DELAY_S", 0.05)
    keypair = Keypair()
    cipher = EnvelopeCipher(os.urandom(32))
    db = Database.connect_sqlite()
    db.apply_schema()
    db.create_user_sync(
        telegram_id="7",
        username="bob",
        chat_id="7",
        wallet_address=str(keypair.pubkey()),
        enc_payload=cipher.encrypt_json(bytes(keypair)),
        defaults=_DEFAULTS,
    )

    class _BigQueueCfg(_Cfg):
        trade_queue_max = 100  # duplicate requeue puts must not fill it

    jupiter = FlakyJupiter(_unsigned_tx(keypair))
    bot = FlakyBot()
    bot.fail = False
    queue = TradeQueue(_BigQueueCfg(), db, cipher, FakeRpc(), jupiter, None, {})
    queue.bot = bot
    await _insert_trade(db, "trade-1")

    for _ in range(11):
        await queue.process_trade("trade-1")  # network still down each time
        await asyncio.sleep(0)  # let pending timers make progress

    stored = await db.get_trade("trade-1")
    assert stored["status"] == "pending"
    assert int(stored["attempts"]) == 11
    assert jupiter.executed == 0
    retries_reported = [text for _, text in bot.sent if "returned to the execution queue" in text]
    assert len(retries_reported) == 4  # retries 1, 2, 3 and 10
    db.close()


@pytest.mark.asyncio
async def test_non_transient_error_fails_immediately():
    keypair = Keypair()
    cipher = EnvelopeCipher(os.urandom(32))
    db = Database.connect_sqlite()
    db.apply_schema()
    db.create_user_sync(
        telegram_id="7",
        username="bob",
        chat_id="7",
        wallet_address=str(keypair.pubkey()),
        enc_payload=cipher.encrypt_json(bytes(keypair)),
        defaults=_DEFAULTS,
    )

    class BrokenJupiter:
        async def order(self, **kwargs):
            raise ValueError("bad request shape")

    queue = TradeQueue(_Cfg(), db, cipher, FakeRpc(), BrokenJupiter(), None, {})
    await _insert_trade(db, "trade-1")
    await queue.process_trade("trade-1")
    stored = await db.get_trade("trade-1")
    assert stored["status"] == "failed"
    assert int(stored["attempts"]) == 0
    assert queue.queue.qsize() == 0
    db.close()


@pytest.mark.asyncio
async def test_signed_trade_is_never_retried():
    """A signature means the swap is on-chain — reconcile owns it, not retry."""
    queue = TradeQueue(_Cfg(), Database.connect_sqlite(), None, None, None, None, {})
    trade = {"id": "t", "created_at": utcnow(), "attempts": 0, "chat_id": "7"}
    current = {"id": "t", "created_at": utcnow(), "attempts": 0, "signature": "sig", "chat_id": "7"}
    assert not await queue._retry_transient("t", trade, current, httpx.ConnectError("down"))


@pytest.mark.asyncio
async def test_reconcile_picks_up_orphaned_retry_row():
    """Process restarted mid-backoff: reconcile re-queues the waiting trade."""
    db = Database.connect_sqlite()
    db.apply_schema()
    queue = TradeQueue(_Cfg(), db, None, None, None, None, {})
    await _insert_trade(db, "trade-1")
    await db.update_trade_fields("trade-1", status="pending", attempts=1)
    # update_trade_fields stamps updated_at=now (timer fresh); simulate that
    # the timer died with a restart by ageing the last update past 60s.
    await db.execute("UPDATE trades SET updated_at=? WHERE id=?", (_stamp(5.0), "trade-1"))
    await queue.reconcile_once()
    assert queue.queue.qsize() == 1
    assert queue.queue.get_nowait() == "trade-1"
    db.close()


@pytest.mark.asyncio
async def test_reconcile_leaves_backoff_rows_alone():
    db = Database.connect_sqlite()
    db.apply_schema()
    queue = TradeQueue(_Cfg(), db, None, None, None, None, {})
    await _insert_trade(db, "trade-1", updated_minutes_ago=0.2)  # 12s ago
    await db.update_trade_fields("trade-1", attempts=1)
    await queue.reconcile_once()
    assert queue.queue.qsize() == 0
    stored = await db.get_trade("trade-1")
    assert stored["status"] == "pending"
    db.close()


@pytest.mark.asyncio
async def test_reconcile_requeues_stale_retry_row_endlessly():
    """No age limit: a retrying trade is picked up again no matter how long
    the outage has already lasted."""
    db = Database.connect_sqlite()
    db.apply_schema()
    queue = TradeQueue(_Cfg(), db, None, None, None, None, {})
    await _insert_trade(db, "trade-1", minutes_ago=40.0)
    await db.update_trade_fields("trade-1", attempts=1)
    # Age the last update past 60s: nothing happened for a full minute, so
    # the backoff timer must have died with the previous process.
    await db.execute("UPDATE trades SET updated_at=? WHERE id=?", (_stamp(40.0), "trade-1"))
    await queue.reconcile_once()
    assert queue.queue.qsize() == 1
    assert queue.queue.get_nowait() == "trade-1"
    stored = await db.get_trade("trade-1")
    assert stored["status"] == "pending"
    db.close()


@pytest.mark.asyncio
async def test_resume_pending_requeues_young_trades_only():
    db = Database.connect_sqlite()
    db.apply_schema()
    queue = TradeQueue(_Cfg(), db, None, None, None, None, {})
    await _insert_trade(db, "young")
    await _insert_trade(db, "old", minutes_ago=11.0)
    resumed = await queue.resume_pending()
    assert resumed == 1
    assert queue.queue.get_nowait() == "young"
    db.close()


@pytest.mark.asyncio
async def test_notify_outage_parks_messages_then_flush_delivers():
    db = Database.connect_sqlite()
    db.apply_schema()
    bot = FlakyBot()
    queue = TradeQueue(_Cfg(), db, None, None, None, None, {})
    queue.bot = bot

    await queue._notify("7", "✅ BUY confirmed during the outage")
    await queue._notify("7", "📉 Trailing stop triggered")
    rows = await db.outbox_rows()
    assert [row["text"] for row in rows] == [
        "✅ BUY confirmed during the outage",
        "📉 Trailing stop triggered",
    ]

    bot.fail = False  # network restored
    await queue._flush_outbox()
    assert [text for _, text in bot.sent] == [
        "✅ BUY confirmed during the outage",
        "📉 Trailing stop triggered",
    ]
    assert await db.outbox_rows() == []
    db.close()


@pytest.mark.asyncio
async def test_outbox_flush_stops_when_still_offline():
    db = Database.connect_sqlite()
    db.apply_schema()
    bot = FlakyBot()
    queue = TradeQueue(_Cfg(), db, None, None, None, None, {})
    queue.bot = bot
    await queue._notify("7", "hello during outage")
    await queue._flush_outbox()  # bot still failing
    assert bot.sent == []
    assert len(await db.outbox_rows()) == 1  # kept for the next sweep
    db.close()
