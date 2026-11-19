"""Track user-armed limit orders and trailing stops against live prices.

The monitor polls every cfg.trigger_poll_seconds, batch-fetches prices for
all open triggers in one request, and evaluates each row:

- limit_buy     — fires when price <= target
- limit_sell    — fires when price >= target
- trailing_stop — tracks the highest price seen since arming (peak) and
                  fires when price falls trail_distance_pct below it

Fires go through the same trade queue as every other trade, so spend caps,
per-user serialization, idempotency keys, and result notifications all
apply. Console output here is deliberately coarse: no mints, no signatures.
"""

from __future__ import annotations

import asyncio
import logging

from sniper.ui import esc, info

logger = logging.getLogger(__name__)


def evaluate_trigger(row: dict, price: float | None) -> tuple[str, float | None]:
    """Decide what one open trigger should do at `price`.

    Returns ("wait", None), ("fire_buy", None), ("fire_sell", None), or
    ("update_peak", new_peak). Pure function so it is easy to test.
    """
    if price is None or price <= 0:
        return "wait", None
    kind = row["kind"]
    if kind == "limit_buy":
        target = float(row.get("target_price_usd") or 0)
        return ("fire_buy", None) if price <= target else ("wait", None)
    if kind == "limit_sell":
        target = float(row.get("target_price_usd") or 0)
        return ("fire_sell", None) if price >= target else ("wait", None)
    if kind == "trailing_stop":
        distance = float(row.get("trail_distance_pct") or 0)
        peak = row.get("peak_price_usd")
        peak = float(peak) if peak else None
        if peak is None or price > peak:
            return "update_peak", price
        if distance > 0 and price <= peak * (1 - distance / 100):
            return "fire_sell", None
        return "wait", None
    return "wait", None


class TriggerMonitor:
    def __init__(self, cfg, db, queue, prices, metrics: dict):
        self.cfg = cfg
        self.db = db
        self.queue = queue
        self.prices = prices
        self.metrics = metrics
        self.last_error = ""
        self._stop = False

    def status_line(self) -> str:
        return self.last_error or "running"

    async def run(self) -> None:
        logger.info("trigger monitor started")
        while not self._stop:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                logger.warning("trigger tick failed: %s", exc)
            try:
                await asyncio.sleep(self.cfg.trigger_poll_seconds)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        if not self.cfg.is_mainnet:
            # Execution is refused downstream anyway; skip the network chatter.
            return
        if await self.db.get_state("global_pause") == "1":
            return
        rows = await self.db.open_triggers()
        if not rows:
            return
        self.metrics["trigger_ticks"] = self.metrics.get("trigger_ticks", 0) + 1
        mints: list[str] = []
        for row in rows:
            if row["token_mint"] not in mints:
                mints.append(row["token_mint"])
        prices = await self.prices.usd_prices(mints)
        for row in rows:
            try:
                await self._handle(row, prices.get(row["token_mint"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("trigger %s evaluation failed: %s", row.get("id"), exc)
                self.metrics["trigger_errors"] = self.metrics.get("trigger_errors", 0) + 1

    async def _handle(self, row: dict, price: float | None) -> None:
        decision, peak = evaluate_trigger(row, price)
        if decision == "wait":
            return
        if decision == "update_peak":
            await self.db.update_trigger_peak(row["id"], float(peak))
            return
        self.metrics["trigger_fires"] = self.metrics.get("trigger_fires", 0) + 1
        if decision == "fire_buy":
            trade_id, reason = await self.queue.enqueue_trigger_buy(row)
        else:
            trade_id, reason = await self.queue.enqueue_trigger_sell(row)
        if trade_id is None:
            if reason == "already":
                # A previous run queued the trade but the process stopped
                # before the trigger was marked. Do not queue twice.
                await self.db.set_trigger_status(row["id"], "triggered")
                return
            await self.db.set_trigger_status(row["id"], "failed", error=(reason or "not accepted")[:500])
            await self.queue.notify(
                int(row.get("chat_id") or row["telegram_id"]),
                "\n".join(
                    [
                        "❌ <b>ORDER NOT EXECUTED</b>\nThe order could not be submitted and has been disarmed.",
                        f"Reason: {esc(reason)}",
                        "",
                        info("No transaction was signed · create a new order with /buy or /sell."),
                    ]
                ),
            )
            return
        await self.db.set_trigger_status(row["id"], "triggered", trade_id=trade_id)
