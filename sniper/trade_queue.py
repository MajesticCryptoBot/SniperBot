"""In-process trade queue.

A fixed number of workers pull jobs. Each user is serialized. Idempotency
keys stop a double tap or a repeated watchlist trigger from sending two swaps.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass

import httpx
from solders.keypair import Keypair

from sniper.accounting import spend_decision
from sniper.amounts import sol_to_lamports
from sniper.db import _is_connection_error
from sniper.errors import UserFacingError
from sniper.jupiter import actual_fee_amount
from sniper.signing import partial_sign
from sniper.solana_service import WSOL
from sniper.solana_tx import build_sol_transfer
from sniper.timeutil import age_seconds, start_of_utc_day, utcnow
from sniper.ui import esc, info

logger = logging.getLogger(__name__)

# Trade history pruning: keep the newest cfg.trades_keep_per_user rows per
# user. Trades inside the grace window are never deleted — hourly and daily
# spend caps still count them.
TRADES_PRUNE_GRACE_SECONDS = 48 * 3600
TRADES_PRUNE_INTERVAL = 600.0

# --- Network resilience -----------------------------------------------------
# A dropped internet connection must never destroy a trade the user already
# confirmed. Transport-level failures (connection reset, DNS failure, timeout,
# RPC or Jupiter unreachable) put the trade back on the queue with a backoff
# and keep retrying ENDLESSLY until the connection is restored — an outage of
# any length must not cost the user their trade. The backoff grows from 15s
# up to a 120s ceiling that is then held, so a long outage produces one
# attempt every two minutes instead of hammering a dead network. A trade that
# already has a signature is never retried here — it was submitted on-chain,
# and the reconcile loop re-checks that signature once the network is back,
# so no swap can be sent twice.
TRANSIENT_EXCEPTIONS = (httpx.TransportError, ConnectionError, TimeoutError, OSError)
TRADE_RETRY_BASE_DELAY_S = 15.0  # doubles per attempt: 15s, 30s, 60s, then held
TRADE_RETRY_MAX_DELAY_S = 120.0  # backoff ceiling, held for the rest of the outage
PENDING_RESUME_MAX_AGE_S = 10 * 60.0  # restart grace for queued-but-unprocessed trades


@dataclass
class PendingPreview:
    preview_id: str
    telegram_id: str
    chat_id: int
    side: str
    mint: str
    amount_sol: float | None
    amount_in_raw: int | None
    destination: str | None
    label: str
    created_monotonic: float
    # Per-trade overrides from the buy/sell panels (None = user settings).
    slippage_bps: int | None = None
    priority_fee_lamports: int | None = None


class TradeQueue:
    def __init__(self, cfg, db, cipher, rpc, jupiter, prices, metrics: dict):
        self.cfg = cfg
        self.db = db
        self.cipher = cipher
        self.rpc = rpc
        self.jupiter = jupiter
        self.prices = prices
        self.metrics = metrics
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=cfg.trade_queue_max)
        self.previews: dict[str, PendingPreview] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._last_prune = -TRADES_PRUNE_INTERVAL  # first reconcile tick prunes too
        self.bot = None

    def user_lock(self, telegram_id: str) -> asyncio.Lock:
        lock = self._user_locks.get(telegram_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[telegram_id] = lock
        return lock

    async def worker(self, index: int) -> None:
        logger.info("trade worker %s started", index)
        while True:
            trade_id = await self.queue.get()
            try:
                trade = await self.db.get_trade(trade_id)
                if not trade:
                    continue
                async with self.user_lock(trade["telegram_id"]):
                    await self.process_trade(trade_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("trade worker failed for id=%s", trade_id)
                self.metrics["worker_errors"] = self.metrics.get("worker_errors", 0) + 1
            finally:
                self.queue.task_done()

    async def reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile_once()
                await self._flush_outbox()
                if time.monotonic() - self._last_prune >= TRADES_PRUNE_INTERVAL:
                    self._last_prune = time.monotonic()
                    removed = await self.db.prune_trades(
                        self.cfg.trades_keep_per_user, TRADES_PRUNE_GRACE_SECONDS
                    )
                    if removed:
                        self.metrics["trades_pruned"] = self.metrics.get("trades_pruned", 0) + removed
                        logger.info("pruned %s old trade rows", removed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _is_connection_error(exc):
                    # open_trades / prune hit a Turso blip the db layer could
                    # not ride out. Nothing was submitted; the next pass retries.
                    logger.warning("reconcile skipped, database unreachable (will retry): %s", exc)
                else:
                    logger.exception("reconcile failed")
            await asyncio.sleep(30)

    async def _flush_outbox(self) -> None:
        """Deliver notifications that failed while the network was down.

        Swept every 30s by the reconcile loop; the first undeliverable message
        stops the sweep so a still-down connection does not burn through the
        backlog one failure at a time."""
        if self.bot is None:
            return
        try:
            rows = await self.db.outbox_rows()
        except Exception as exc:
            if _is_connection_error(exc):
                logger.warning("outbox read skipped, database unreachable: %s", exc)
            else:
                logger.exception("outbox read failed")
            return
        delivered: list[int] = []
        for row in rows:
            try:
                await self.bot.send_message(chat_id=int(row["chat_id"]), text=str(row["text"])[:3900], parse_mode="HTML")
                delivered.append(int(row["id"]))
            except Exception as exc:
                logger.info("outbox flush paused: %s", exc.__class__.__name__)
                break
        if delivered:
            await self.db.outbox_delete(delivered)
            self.metrics["outbox_flushed"] = self.metrics.get("outbox_flushed", 0) + len(delivered)
            logger.info("delivered %s notifications queued during the outage", len(delivered))

    async def reconcile_once(self) -> None:
        rows = await self.db.open_trades()
        for row in rows:
            try:
                age = age_seconds(row["created_at"])
            except Exception:
                age = 0
            status = row["status"]
            signature = row.get("signature")
            attempts = int(row.get("attempts") or 0)
            if status == "pending" and not signature and attempts > 0:
                # A trade in its network-retry cycle: its backoff timer is
                # pending, or the process restarted and the timer died with
                # it — then pick it up here, however long the outage has
                # lasted. Retrying never gives up. A duplicate queue entry is
                # harmless because claim_trade only succeeds once.
                try:
                    updated_age = age_seconds(row["updated_at"])
                except Exception:
                    updated_age = 0
                if updated_age > 60:
                    try:
                        self.queue.put_nowait(row["id"])
                    except asyncio.QueueFull:
                        pass
                continue
            if status in {"pending", "processing"} and not signature and age > 90:
                await self.db.finalize(
                    row["id"],
                    success=False,
                    error="Cancelled before submission.",
                )
                await self._notify(row.get("chat_id"), "❌ <b>TRADE CANCELLED</b>\nCancelled before submission. No transaction was sent.")
                continue
            if status == "submitted" and signature:
                ok, err = await self.rpc.confirm(signature, timeout_s=8)
                if ok:
                    fee = await self.rpc.transaction_fee(signature)
                    await self.db.finalize(row["id"], success=True, fee_lamports=fee)
                    await self._notify(row.get("chat_id"), f"✅ <b>TRANSACTION CONFIRMED</b>\n\nSignature:\n{esc(signature)}")
                elif err and "Timed out" not in err and age > 20:
                    await self.db.finalize(row["id"], success=False, error=err)
                    await self._notify(row.get("chat_id"), f"❌ <b>TRANSACTION FAILED</b>\n\nReason: {esc(err)}\nSignature:\n{esc(signature)}")
                elif age > 600 and err:
                    await self.db.finalize(row["id"], success=False, error=err)
                    await self._notify(row.get("chat_id"), f"⏳ <b>CONFIRMATION NOT RECEIVED</b>\nThe transaction may still have settled.\n\nSignature:\n{esc(signature)}\n\n" + info("Verify its status before retrying."))

    async def enqueue_preview(self, preview: PendingPreview, source: str) -> str:
        trade_id = str(uuid.uuid4())
        now = utcnow()
        inserted = await self.db.insert_trade(
            {
                "id": trade_id,
                "telegram_id": preview.telegram_id,
                "chat_id": str(preview.chat_id),
                "side": preview.side,
                "token_mint": preview.mint,
                "destination": preview.destination,
                "amount_in_raw": None if preview.amount_in_raw is None else str(preview.amount_in_raw),
                "amount_out_raw": None,
                "amount_sol": preview.amount_sol,
                "slippage_bps": preview.slippage_bps,
                "price_impact_pct": None,
                "signature": None,
                "status": "pending",
                "error": None,
                "source": source,
                "idempotency_key": preview.preview_id,
                "fee_lamports": None,
                "sol_usd": None,
                "realized_pnl_sol": None,
                "request_id": None,
                "claim_token": None,
                "priority_fee_lamports": preview.priority_fee_lamports,
                "created_at": now,
                "updated_at": now,
            }
        )
        if not inserted:
            raise UserFacingError("This confirmation was already submitted. Check /orders.")
        try:
            self.queue.put_nowait(trade_id)
        except asyncio.QueueFull as exc:
            await self.db.finalize(trade_id, success=False, error="Service busy, trade not accepted.")
            self.metrics["queue_full"] = self.metrics.get("queue_full", 0) + 1
            logger.warning("trade queue full; refusing trade %s", trade_id)
            raise UserFacingError("The execution service is currently at capacity. Please try again shortly.") from exc
        self.metrics["enqueued"] = self.metrics.get("enqueued", 0) + 1
        return trade_id

    async def enqueue_watch_snipe(self, entry: dict, report) -> tuple[str | None, str | None]:
        """Queue the buy for one watchlist entry.

        Returns (trade_id, None) when the buy was queued, (None, reason) when
        it was not, where reason is "already" for a duplicate of a queued buy
        or the spend-guard failure text. This only ever runs for a token the
        user put on their own list.
        """
        telegram_id = entry["telegram_id"]
        mint = entry["token_mint"]
        idem = f"snipe:{telegram_id}:{mint}:{entry['id']}"
        if await self.db.find_idempotency(idem):
            return None, "already"
        amount = float(entry["amount_sol"])
        from datetime import datetime, timedelta, timezone

        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        decision_ok, reason = spend_decision(
            amount_sol=amount,
            max_per_trade=float(entry["max_sol_per_trade"]),
            spent_today=await self.db.spent_today(telegram_id, start_of_utc_day()),
            daily_limit=float(entry["daily_spend_limit_sol"]),
            trades_last_hour=await self.db.trades_since(telegram_id, hour_ago),
            max_trades_per_hour=self.cfg.max_trades_per_hour,
            user_paused=bool(entry["trading_paused"]),
            global_paused=(await self.db.get_state("global_pause")) == "1",
            absolute_max_sol=self.cfg.absolute_max_sol,
        )
        if not decision_ok:
            return None, reason
        preview = PendingPreview(
            preview_id=idem,
            telegram_id=telegram_id,
            chat_id=int(entry.get("chat_id") or telegram_id),
            side="buy",
            mint=mint,
            amount_sol=amount,
            amount_in_raw=None,
            destination=None,
            label="autosnipe buy",
            created_monotonic=0,
        )
        try:
            trade_id = await self.enqueue_preview(preview, source="autosnipe")
        except UserFacingError as exc:
            return None, str(exc)
        self.metrics["snipes_enqueued"] = self.metrics.get("snipes_enqueued", 0) + 1
        await self._notify(
            preview.chat_id,
            "\n".join(
                [
                    "🎯 <b>AUTOSNIPE TRIGGERED</b>",
                    "",
                    f"Mint: {esc(mint)}",
                    f"Entry: {amount:.4f} SOL",
                    f"Reference: {trade_id}",
                    "",
                    "🛡️ Safety: PASSED",
                    report.summary(),
                ]
            ),
        )
        return trade_id, None

    async def enqueue_trigger_buy(self, row: dict) -> tuple[str | None, str | None]:
        """Queue the buy for a user-armed limit trigger. Same spend guards as
        autosnipe; safety checks are skipped like any other manual trade.

        Returns (trade_id, None) or (None, reason)."""
        telegram_id = row["telegram_id"]
        mint = row["token_mint"]
        idem = f"trigger:{row['id']}"
        if await self.db.find_idempotency(idem):
            return None, "already"
        amount = float(row.get("amount_sol") or 0)
        from datetime import datetime, timedelta, timezone

        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        decision_ok, reason = spend_decision(
            amount_sol=amount,
            max_per_trade=float(row["max_sol_per_trade"]),
            spent_today=await self.db.spent_today(telegram_id, start_of_utc_day()),
            daily_limit=float(row["daily_spend_limit_sol"]),
            trades_last_hour=await self.db.trades_since(telegram_id, hour_ago),
            max_trades_per_hour=self.cfg.max_trades_per_hour,
            user_paused=False,  # open_triggers already excludes paused users
            global_paused=(await self.db.get_state("global_pause")) == "1",
            absolute_max_sol=self.cfg.absolute_max_sol,
        )
        if not decision_ok:
            return None, reason
        target = row.get("target_price_usd")
        preview = PendingPreview(
            preview_id=idem,
            telegram_id=telegram_id,
            chat_id=int(row.get("chat_id") or telegram_id),
            side="buy",
            mint=mint,
            amount_sol=amount,
            amount_in_raw=None,
            destination=None,
            label=f"limit buy {amount:.4f} SOL",
            created_monotonic=0,
            slippage_bps=int(row["slippage_bps"]) if row.get("slippage_bps") else None,
            priority_fee_lamports=int(row["priority_fee_lamports"]) if row.get("priority_fee_lamports") else None,
        )
        try:
            trade_id = await self.enqueue_preview(preview, source="trigger")
        except UserFacingError as exc:
            return None, str(exc)
        self.metrics["triggers_enqueued"] = self.metrics.get("triggers_enqueued", 0) + 1
        await self._notify(
            preview.chat_id,
            "\n".join(
                [
                    "⚡ <b>LIMIT BUY TRIGGERED</b>",
                    "",
                    f"Mint: {esc(mint)}",
                    f"Trigger: ${target:g}",
                    f"Size: {amount:.4f} SOL",
                    f"Reference: {trade_id}",
                ]
            ),
        )
        return trade_id, None

    async def enqueue_trigger_sell(self, row: dict) -> tuple[str | None, str | None]:
        """Queue the sell for a limit-sell or trailing-stop trigger.

        Returns (trade_id, None) or (None, reason)."""
        telegram_id = row["telegram_id"]
        mint = row["token_mint"]
        idem = f"trigger:{row['id']}"
        if await self.db.find_idempotency(idem):
            return None, "already"
        accounts = await self.rpc.get_token_accounts(row["wallet_address"])
        match = [item for item in accounts if item["mint"] == mint]
        if not match:
            return None, "no holdings"
        from sniper.amounts import parse_sell_spec

        balance_raw = sum(item["amount_raw"] for item in match)
        decimals = match[0]["decimals"]
        percent = float(row.get("sell_percent") or 100)
        try:
            raw, label = parse_sell_spec(f"{percent}%", balance_raw, decimals)
        except Exception as exc:
            return None, str(exc)
        if raw <= 0:
            return None, "sell amount rounded to zero"
        if row["kind"] == "trailing_stop":
            headline = (
                f"⚡ <b>TRAILING STOP TRIGGERED</b>\n\nMint: {esc(mint)}\n"
                f"Pullback: {float(row['trail_distance_pct']):g}% below peak ${float(row.get('peak_price_usd') or 0):g}"
            )
        else:
            headline = f"⚡ <b>LIMIT SELL TRIGGERED</b>\n\nMint: {esc(mint)}\nTarget reached: ${float(row.get('target_price_usd') or 0):g}"
        preview = PendingPreview(
            preview_id=idem,
            telegram_id=telegram_id,
            chat_id=int(row.get("chat_id") or telegram_id),
            side="sell",
            mint=mint,
            amount_sol=None,
            amount_in_raw=raw,
            destination=None,
            label=f"auto sell {label}",
            created_monotonic=0,
            slippage_bps=int(row["slippage_bps"]) if row.get("slippage_bps") else None,
            priority_fee_lamports=int(row["priority_fee_lamports"]) if row.get("priority_fee_lamports") else None,
        )
        try:
            trade_id = await self.enqueue_preview(preview, source="trigger")
        except UserFacingError as exc:
            return None, str(exc)
        self.metrics["triggers_enqueued"] = self.metrics.get("triggers_enqueued", 0) + 1
        await self._notify(
            preview.chat_id,
            "\n".join([headline, f"Sell quantity: {esc(label)}", f"Reference: {trade_id}"]),
        )
        return trade_id, None

    async def enqueue_auto_buy(self, telegram_id: str, mint: str, amount_sol: float) -> tuple[str | None, str | None]:
        """Queue the instant buy behind the auto-buy-on-paste switch.

        The token came from the user's own message, so like every manual
        trade it skips the autosnipe safety checks; the spend guards and
        the price-impact limit still apply at execution time. Each paste
        is a deliberate act and gets a fresh idempotency key — paste the
        same token twice and you buy twice. Returns (trade_id, None) or
        (None, reason)."""
        user = await self.db.get_user(telegram_id)
        settings = await self.db.get_settings(telegram_id)
        if not user or not settings or not user.get("wallet_address"):
            return None, "no wallet"
        if int(settings.get("auto_buy_enabled") or 0) != 1:
            return None, "auto-buy is off"
        if bool(user["trading_paused"]):
            return None, "Trading is paused for this account. Use /resume before submitting new requests."
        from datetime import datetime, timedelta, timezone

        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        decision_ok, reason = spend_decision(
            amount_sol=amount_sol,
            max_per_trade=float(settings["max_sol_per_trade"]),
            spent_today=await self.db.spent_today(telegram_id, start_of_utc_day()),
            daily_limit=float(settings["daily_spend_limit_sol"]),
            trades_last_hour=await self.db.trades_since(telegram_id, hour_ago),
            max_trades_per_hour=self.cfg.max_trades_per_hour,
            user_paused=False,
            global_paused=(await self.db.get_state("global_pause")) == "1",
            absolute_max_sol=self.cfg.absolute_max_sol,
        )
        if not decision_ok:
            return None, reason
        preview = PendingPreview(
            preview_id=f"autobuy:{telegram_id}:{mint}:{uuid.uuid4().hex[:8]}",
            telegram_id=telegram_id,
            chat_id=int(user.get("chat_id") or telegram_id),
            side="buy",
            mint=mint,
            amount_sol=amount_sol,
            amount_in_raw=None,
            destination=None,
            label=f"auto buy {amount_sol:.4f} SOL",
            created_monotonic=0,
        )
        try:
            trade_id = await self.enqueue_preview(preview, source="auto_buy")
        except UserFacingError as exc:
            return None, str(exc)
        self.metrics["auto_buys_enqueued"] = self.metrics.get("auto_buys_enqueued", 0) + 1
        await self._notify(
            preview.chat_id,
            "\n".join(
                [
                    "🤖 <b>AUTO-BUY TRIGGERED</b>",
                    "",
                    f"Mint: {esc(mint)}",
                    f"Size: {amount_sol:.4f} SOL",
                    f"Reference: {trade_id}",
                    "",
                    info("Auto-buy skips confirmation and the safety gate · /stop pauses activity."),
                ]
            ),
        )
        return trade_id, None

    async def enqueue_auto_sell(self, row: dict, kind: str, pnl_pct: float, price_usd) -> tuple[str | None, str | None]:
        """Queue the full-position sell for a fired take-profit/stop-loss.

        `row` is a positions_for_tp_sl() row. The position's updated_at is
        part of the idempotency key, so one armed level can never queue two
        sells. Returns (trade_id, None) or (None, reason)."""
        telegram_id = row["telegram_id"]
        mint = row["token_mint"]
        idem = f"tpsl:{telegram_id}:{mint}:{kind}:{row['updated_at']}"
        if await self.db.find_idempotency(idem):
            return None, "already"
        accounts = await self.rpc.get_token_accounts(row["wallet_address"])
        match = [item for item in accounts if item["mint"] == mint]
        if not match:
            return None, "no holdings"
        from sniper.amounts import parse_sell_spec

        balance_raw = sum(item["amount_raw"] for item in match)
        decimals = match[0]["decimals"]
        try:
            raw, label = parse_sell_spec("100%", balance_raw, decimals)
        except Exception as exc:
            return None, str(exc)
        if raw <= 0:
            return None, "sell amount rounded to zero"
        price_text = f"${float(price_usd):g}" if price_usd else "n/a"
        if kind == "stop_loss":
            headline = f"🛑 <b>STOP-LOSS TRIGGERED</b>\n\nMint: {esc(mint)}\nPerformance: {pnl_pct:.1f}% · price {price_text}"
        else:
            headline = f"📈 <b>TAKE-PROFIT TRIGGERED</b>\n\nMint: {esc(mint)}\nPerformance: +{pnl_pct:.1f}% · price {price_text}"
        preview = PendingPreview(
            preview_id=idem,
            telegram_id=telegram_id,
            chat_id=int(row.get("chat_id") or telegram_id),
            side="sell",
            mint=mint,
            amount_sol=None,
            amount_in_raw=raw,
            destination=None,
            label=f"auto sell {label}",
            created_monotonic=0,
        )
        try:
            trade_id = await self.enqueue_preview(preview, source="tp_sl")
        except UserFacingError as exc:
            return None, str(exc)
        self.metrics["tp_sl_enqueued"] = self.metrics.get("tp_sl_enqueued", 0) + 1
        await self._notify(
            preview.chat_id,
            "\n".join(
                [
                    headline,
                    f"Exit quantity: {esc(label)}",
                    f"Reference: {trade_id}",
                ]
            ),
        )
        return trade_id, None

    async def process_trade(self, trade_id: str) -> None:
        token = secrets.token_hex(8)
        claimed = await self.db.claim_trade(trade_id, token)
        if not claimed:
            logger.info("trade %s was not pending; skipping", trade_id)
            return
        trade = await self.db.get_trade(trade_id)
        if not trade:
            return
        try:
            if trade["side"] == "withdraw":
                await self._withdraw(trade)
            elif trade["side"] in {"buy", "sell"}:
                await self._swap(trade)
            else:
                raise UserFacingError("This transaction type is not supported.")
        except UserFacingError as exc:
            current = await self.db.get_trade(trade_id)
            if current and current.get("signature"):
                await self.db.update_trade_fields(trade_id, status="submitted", error=str(exc))
            else:
                await self.db.finalize(trade_id, success=False, error=str(exc))
            if current and current.get("signature"):
                notice = (
                    f"⏳ <b>TRADE SUBMITTED · VERIFY STATUS</b>\n\n"
                    f"Reason: {esc(str(exc))}\nSignature:\n{esc(current['signature'])}\n\n"
                    + info("It may still settle · check /orders before retrying.")
                )
            else:
                notice = f"❌ <b>TRADE NOT EXECUTED</b>\n\nReason: {esc(str(exc))}\n\n" + info("No signature recorded · review before retrying.")
            await self._notify(trade.get("chat_id"), notice)
            self.metrics["trades_failed"] = self.metrics.get("trades_failed", 0) + 1
        except Exception as exc:
            logger.exception("unexpected trade failure id=%s", trade_id)
            current = await self.db.get_trade(trade_id)
            if await self._retry_transient(trade_id, trade, current, exc):
                return
            message = "⚠️ Unexpected execution error. Check /orders before retrying the trade."
            if current and current.get("signature"):
                await self.db.update_trade_fields(trade_id, status="submitted", error=exc.__class__.__name__)
            else:
                await self.db.finalize(trade_id, success=False, error=exc.__class__.__name__)
            await self._notify(trade.get("chat_id"), message)
            self.metrics["trades_failed"] = self.metrics.get("trades_failed", 0) + 1

    async def _retry_transient(self, trade_id: str, trade: dict, current: dict | None, exc: Exception) -> bool:
        """Re-queue a trade killed by a transient network failure.

        Returns True when this method owns the outcome (trade re-queued with a
        backoff); False when the caller should finalize it as failed. The
        retry is endless — it keeps going until the connection is restored —
        with the delay held at TRADE_RETRY_MAX_DELAY_S once the backoff caps.
        The user is notified on the first three retries and then every tenth,
        so a multi-hour outage informs without flooding the chat."""
        row = current or trade
        if row.get("signature"):
            # Already submitted on-chain — the reconcile loop owns it now.
            return False
        if not isinstance(exc, TRANSIENT_EXCEPTIONS) and not _is_connection_error(exc):
            return False
        attempts = int(row.get("attempts") or 0)
        delay = min(TRADE_RETRY_BASE_DELAY_S * (2**attempts), TRADE_RETRY_MAX_DELAY_S)
        await self.db.update_trade_fields(
            trade_id,
            status="pending",
            error=f"Network hiccup · retry {attempts + 1}",
            attempts=attempts + 1,
        )
        self.metrics["trade_retries"] = self.metrics.get("trade_retries", 0) + 1
        if attempts < 3 or (attempts + 1) % 10 == 0:
            await self._notify(
                row.get("chat_id"),
                "\n".join(
                    [
                        "📡 <b>NETWORK INTERRUPTION</b>\nYour request has been returned to the execution queue.",
                        f"Retry attempt: {attempts + 1} · next in ~{delay:.0f}s",
                        f"Reference: {trade_id}",
                    ]
                ),
            )
        asyncio.create_task(self._requeue_later(trade_id, delay))
        return True

    async def _requeue_later(self, trade_id: str, delay: float) -> None:
        """Put a retried trade back on the queue after its backoff delay."""
        try:
            await asyncio.sleep(delay)
            row = await self.db.get_trade(trade_id)
            if not row or row["status"] != "pending" or row.get("signature"):
                return
            try:
                self.queue.put_nowait(trade_id)
            except asyncio.QueueFull:
                await self.db.finalize(
                    trade_id, success=False, error="Service busy after a network interruption."
                )
                await self._notify(
                    row.get("chat_id"),
                    "❌ <b>TRADE NOT SUBMITTED</b>\nQueue full after a network interruption. No transaction was sent.",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("delayed requeue failed for id=%s", trade_id)

    async def resume_pending(self) -> int:
        """Re-queue trades that were confirmed but not yet executed when the
        process last stopped (restart during an outage, crash, deploy).

        Only young trades are resumed here; older ones are left for the
        reconcile loop, which cancels them with an explicit notice. Every
        execution path re-checks the spend guards and claim_trade makes a
        duplicate queue entry harmless, so resuming is always safe."""
        rows = await self.db.open_trades()
        resumed = 0
        for row in rows:
            if row["status"] != "pending" or row.get("signature"):
                continue
            try:
                age = age_seconds(row["created_at"])
            except Exception:
                age = 0
            if age > PENDING_RESUME_MAX_AGE_S:
                continue
            try:
                self.queue.put_nowait(row["id"])
                resumed += 1
            except asyncio.QueueFull:
                break
        if resumed:
            logger.info("resumed %s queued trade(s) from the previous run", resumed)
        return resumed

    async def _guards(self, trade: dict, amount_sol: float) -> dict:
        user = await self.db.get_user(trade["telegram_id"])
        settings = await self.db.get_settings(trade["telegram_id"])
        if not user or not settings or not user.get("wallet_address"):
            raise UserFacingError("No connected wallet was found. Complete /start before trading.")
        from datetime import datetime, timedelta, timezone

        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # The current pending/processing row is already inside spent_today and the hourly count.
        spent = await self.db.spent_today(trade["telegram_id"], start_of_utc_day())
        if trade["side"] in {"buy", "withdraw"}:
            spent = max(0.0, spent - float(trade.get("amount_sol") or 0))
        hourly = await self.db.trades_since(trade["telegram_id"], hour_ago)
        hourly = max(0, hourly - 1)
        ok, reason = spend_decision(
            amount_sol=amount_sol if trade["side"] != "sell" else min(float(settings["max_sol_per_trade"]), max(amount_sol, 0.000001)),
            max_per_trade=float(settings["max_sol_per_trade"]) if trade["side"] != "sell" else max(float(settings["max_sol_per_trade"]), amount_sol, 0.000001),
            spent_today=spent if trade["side"] != "sell" else 0,
            daily_limit=float(settings["daily_spend_limit_sol"]) if trade["side"] != "sell" else max(float(settings["daily_spend_limit_sol"]), 1),
            trades_last_hour=hourly,
            max_trades_per_hour=self.cfg.max_trades_per_hour,
            user_paused=bool(user["trading_paused"]),
            global_paused=(await self.db.get_state("global_pause")) == "1",
            absolute_max_sol=self.cfg.absolute_max_sol if trade["side"] != "sell" else max(self.cfg.absolute_max_sol, amount_sol),
        )
        if trade["side"] == "sell":
            # Sells are not SOL spend. Re-check only pause and the hourly cap.
            if user["trading_paused"]:
                raise UserFacingError("Trading is paused for this account. Use /resume before submitting new requests.")
            if await self.db.get_state("global_pause") == "1":
                raise UserFacingError("Trading is temporarily paused. Please try again later.")
            if hourly >= self.cfg.max_trades_per_hour:
                raise UserFacingError(f"Hourly trade cap reached ({self.cfg.max_trades_per_hour} per user).")
        elif not ok:
            raise UserFacingError(reason)
        return {"user": user, "settings": settings}

    async def _load_keypair(self, telegram_id: str) -> Keypair:
        payload = await self.db.get_secret_payload(telegram_id)
        if not payload:
            raise UserFacingError("No connected wallet is stored. Complete /start before trading.")
        try:
            secret = self.cipher.decrypt_json(payload)
            keypair = Keypair.from_bytes(secret)
        except Exception as exc:
            logger.warning("wallet decrypt failed for user=%s: %s", telegram_id, exc.__class__.__name__)
            raise UserFacingError("The connected wallet is temporarily unavailable. Please try again later.") from exc
        return keypair

    async def _swap(self, trade: dict) -> None:
        if not self.cfg.is_mainnet:
            logger.warning("swap refused: network is %s", self.cfg.network)
            raise UserFacingError("Trading is unavailable on the current network configuration.")
        settings_amount = float(trade.get("amount_sol") or 0)
        ctx = await self._guards(trade, settings_amount if trade["side"] == "buy" else 0.000001)
        user = ctx["user"]
        settings = ctx["settings"]
        # Panel overrides ride on the trade row; None falls back to settings.
        trade_slip = trade.get("slippage_bps")
        effective_slippage = int(trade_slip) if trade_slip else int(settings["slippage_bps"])
        keypair = await self._load_keypair(trade["telegram_id"])
        try:
            if str(keypair.pubkey()) != user["wallet_address"]:
                logger.warning("stored key mismatch for user=%s", trade["telegram_id"])
                raise UserFacingError("Wallet verification failed. No transaction was signed or sent.")
            if trade["side"] == "buy":
                lamports = sol_to_lamports(trade["amount_sol"])
                input_mint, output_mint, amount = WSOL, trade["token_mint"], lamports
            else:
                raw = int(trade.get("amount_in_raw") or 0)
                if raw <= 0:
                    raise UserFacingError("The sell quantity resolved to zero. No transaction was sent.")
                input_mint, output_mint, amount = trade["token_mint"], WSOL, raw
            # Sell-side bot fee: Jupiter's referral fee is charged atomically
            # inside the swap — no second transaction. Jupiter picks the fee
            # mint by its priority list (SOL first), so token->SOL sells pay
            # the fee in SOL.
            fee_account, fee_bps = (
                (self.cfg.jupiter_referral_account, self.cfg.bot_fee_bps)
                if trade["side"] == "sell" and self.cfg.fee_active
                else (None, None)
            )
            order = await self.jupiter.order(
                input_mint=input_mint,
                output_mint=output_mint,
                amount=amount,
                taker=user["wallet_address"],
                slippage_bps=effective_slippage,
                priority_fee_lamports=int(trade["priority_fee_lamports"]) if trade.get("priority_fee_lamports") else None,
                referral_account=fee_account,
                referral_fee_bps=fee_bps,
            )
            limit = float(settings["autosnipe_max_price_impact_pct"] if trade["source"] == "autosnipe" else settings["max_price_impact_pct"])
            if order.price_impact_pct > limit:
                raise UserFacingError(
                    f"Fresh quote price impact is {order.price_impact_pct:.2f}%, above your limit of {limit:.2f}%. Nothing was signed."
                )
            signed = partial_sign(order.transaction, bytes(keypair))
            quote_fields = {
                "request_id": order.request_id,
                "slippage_bps": effective_slippage,
                "price_impact_pct": order.price_impact_pct,
                "amount_in_raw": str(order.in_amount),
                "amount_out_raw": str(order.out_amount),
                "sol_usd": order.in_usd if trade["side"] == "buy" else order.out_usd,
            }
            if trade["side"] == "sell":
                quote_fields["amount_sol"] = order.out_amount / 1_000_000_000
            await self.db.update_trade_fields(trade["id"], **quote_fields)
            result = await self.jupiter.execute(signed, order.request_id)
            if result.input_amount or result.output_amount:
                fields = {}
                if result.input_amount:
                    fields["amount_in_raw"] = str(result.input_amount)
                if result.output_amount:
                    fields["amount_out_raw"] = str(result.output_amount)
                if trade["side"] == "sell" and result.output_amount:
                    fields["amount_sol"] = result.output_amount / 1_000_000_000
                if trade["side"] == "buy" and result.input_amount:
                    fields["amount_sol"] = result.input_amount / 1_000_000_000
                await self.db.update_trade_fields(trade["id"], **fields)
            if result.signature:
                await self.db.update_trade_fields(trade["id"], signature=result.signature, status="submitted", error=result.error)
            if result.signature and trade["side"] == "sell" and order.fee_mint:
                # The swap landed and Jupiter reported a fee — audit row.
                fee_raw = actual_fee_amount(order, result, input_mint, output_mint)
                await self.db.insert_fee_ledger(
                    {
                        "trade_id": trade["id"],
                        "side": "sell",
                        "token_mint": trade["token_mint"],
                        "fee_mint": order.fee_mint,
                        "fee_bps": order.fee_bps if order.fee_bps is not None else self.cfg.bot_fee_bps,
                        "fee_amount_raw": fee_raw,
                        "fee_value_sol": fee_raw / 1_000_000_000 if fee_raw is not None and order.fee_mint == WSOL else None,
                        "volume_sol": (result.output_amount or order.out_amount) / 1_000_000_000,
                    }
                )
                self.metrics["bot_fees_charged"] = self.metrics.get("bot_fees_charged", 0) + 1
            if not result.ok:
                if result.signature:
                    ok, err = await self.rpc.confirm(result.signature, timeout_s=20)
                    if ok:
                        result_ok = True
                    else:
                        raise UserFacingError(result.error or err or "The swap could not be completed. Please try again shortly.")
                else:
                    raise UserFacingError(result.error or "The swap could not be completed. Please try again shortly.")
            else:
                result_ok = True
            if result_ok and result.signature:
                ok, err = await self.rpc.confirm(result.signature, timeout_s=45)
                fee = await self.rpc.transaction_fee(result.signature)
                if ok:
                    await self.db.finalize(trade["id"], success=True, fee_lamports=fee)
                    self.metrics["trades_confirmed"] = self.metrics.get("trades_confirmed", 0) + 1
                    await self._notify(
                        trade.get("chat_id"),
                        f"✅ <b>{trade['side'].upper()} CONFIRMED</b>\n\nReference: {trade['id']}\nMint: {esc(trade['token_mint'])}\nSignature:\n{esc(result.signature)}",
                    )
                else:
                    await self.db.update_trade_fields(trade["id"], status="submitted", error=err, fee_lamports=fee)
                    await self._notify(
                        trade.get("chat_id"),
                        f"⏳ <b>{trade['side'].upper()} SUBMITTED · CONFIRMATION PENDING</b>\n\nReference: {trade['id']}\nMint: {esc(trade['token_mint'])}\nSignature:\n{esc(result.signature)}\nDetail: {esc(err)}\n\n" + info("It may still settle · verify before retrying."),
                    )
        finally:
            del keypair

    async def _withdraw(self, trade: dict) -> None:
        amount = float(trade.get("amount_sol") or 0)
        ctx = await self._guards(trade, amount)
        user = ctx["user"]
        settings = ctx["settings"]
        keypair = await self._load_keypair(trade["telegram_id"])
        try:
            if str(keypair.pubkey()) != user["wallet_address"]:
                logger.warning("stored key mismatch for user=%s", trade["telegram_id"])
                raise UserFacingError("Wallet verification failed. No transaction was signed or sent.")
            lamports = sol_to_lamports(amount)
            balance = await self.rpc.get_balance_lamports(user["wallet_address"])
            reserve = 5000 + int(settings["priority_fee_lamports"]) + 10000
            if lamports + reserve > balance:
                raise UserFacingError(
                    f"Insufficient SOL. Current balance: {balance / 1_000_000_000:.6f} SOL. "
                    f"Keep approximately {reserve / 1_000_000_000:.6f} SOL available for network fees and reserve."
                )
            blockhash = await self.rpc.latest_blockhash()
            raw = build_sol_transfer(
                bytes(keypair),
                trade["destination"],
                lamports,
                blockhash,
                int(settings["priority_fee_lamports"]),
            )
            signature = await self.rpc.send_raw(raw)
            await self.db.update_trade_fields(trade["id"], signature=signature, status="submitted", amount_in_raw=str(lamports))
            ok, err = await self.rpc.confirm(signature, timeout_s=45)
            fee = await self.rpc.transaction_fee(signature)
            if ok:
                await self.db.finalize(trade["id"], success=True, fee_lamports=fee)
                self.metrics["trades_confirmed"] = self.metrics.get("trades_confirmed", 0) + 1
                await self._notify(trade.get("chat_id"), f"✅ <b>WITHDRAWAL CONFIRMED</b>\n\nReference: {trade['id']}\nAmount: {amount:.6f} SOL\nTo: {esc(trade['destination'])}\nSignature:\n{esc(signature)}")
            else:
                await self.db.update_trade_fields(trade["id"], status="submitted", error=err, fee_lamports=fee)
                await self._notify(trade.get("chat_id"), f"⏳ <b>WITHDRAWAL SUBMITTED · CONFIRMATION PENDING</b>\n\nReference: {trade['id']}\nAmount: {amount:.6f} SOL\nTo: {esc(trade['destination'])}\nSignature:\n{esc(signature)}\nDetail: {esc(err)}")
        finally:
            del keypair

    async def notify(self, chat_id, text: str) -> None:
        """Public message helper for background jobs (watchlist monitor)."""
        await self._notify(chat_id, text)

    async def _notify(self, chat_id, text: str) -> None:
        if not self.bot or not chat_id:
            return
        try:
            await self.bot.send_message(chat_id=int(chat_id), text=text[:3900], parse_mode="HTML")
        except Exception as exc:
            logger.warning("telegram notify failed: %s", exc.__class__.__name__)
            # The message still has to reach the user after the network is
            # back — park it and let the reconcile sweep deliver it.
            try:
                await self.db.outbox_add(chat_id, text)
            except Exception:
                logger.exception("outbox add failed")
