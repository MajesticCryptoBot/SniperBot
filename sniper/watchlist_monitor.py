"""Watch the tokens users added to their autosnipe list.

The bot never scans the chain for newly created pools or tokens. The only
tokens it can auto-buy are the ones a user explicitly added to their own
watchlist from an announcement they trust (a project's official channel,
for example). A watch entry fires when the token first has a live market
price — meaning liquidity exists — and the safety checks pass.

Console output here is deliberately coarse: no mints, no signatures.
"""

from __future__ import annotations

import asyncio
import logging

from sniper.solana_service import WSOL
from sniper.ui import esc, info

logger = logging.getLogger(__name__)

# Refusal texts for the add-to-watchlist gate. Plain text on purpose: the
# flow engine and the error handler send them escaped.
LIVE_TOKEN_REFUSAL = (
    "🎯 Not added: this token is already live and tradeable.\n\n"
    "Autosnipe watches only tokens with no market yet — the buy fires the "
    "moment the token goes live. Nothing was saved.\n\n"
    "To buy it now, paste the mint in chat (auto-buy or the token panel)."
)
MARKET_CHECK_REFUSAL = (
    "🎯 Autosnipe could not verify the token's market status. Try again in a moment."
)


async def watchlist_add_rejection(prices, mint: str) -> str | None:
    """Refusal text when a mint must not enter the autosnipe watchlist, else None.

    Autosnipe exists to buy a token the moment it goes live, so a token that
    already trades is refused here instead of triggering on the next tick.
    The same price source the monitor fires on decides. WSOL travels with
    every request and always has a price: a response without it means the
    price service never answered — an unverifiable token is refused rather
    than risked, because accepting one could snipe a live token instantly.
    """
    try:
        quotes = await prices.usd_prices([mint, WSOL])
    except Exception:
        return MARKET_CHECK_REFUSAL
    if float(quotes.get(mint, 0)) > 0:
        return LIVE_TOKEN_REFUSAL
    if float(quotes.get(WSOL, 0)) <= 0:
        return MARKET_CHECK_REFUSAL
    return None


class WatchlistMonitor:
    def __init__(self, cfg, db, safety, queue, prices, metrics: dict):
        self.cfg = cfg
        self.db = db
        self.safety = safety
        self.queue = queue
        self.prices = prices
        self.metrics = metrics
        self.last_error = ""
        self._stop = False

    def status_line(self) -> str:
        return self.last_error or "running"

    async def run(self) -> None:
        logger.info("watchlist monitor started")
        while not self._stop:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                logger.warning("watchlist tick failed: %s", exc)
            try:
                await asyncio.sleep(self.cfg.watchlist_poll_seconds)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        if not self.cfg.is_mainnet:
            # Execution is refused downstream anyway; skip the network chatter.
            return
        if await self.db.get_state("global_pause") == "1":
            return
        entries = await self.db.watchlist_active()
        if not entries:
            return
        self.metrics["watch_ticks"] = self.metrics.get("watch_ticks", 0) + 1
        mints: list[str] = []
        for entry in entries:
            mint = entry["token_mint"]
            if mint not in mints:
                mints.append(mint)
        prices = await self.prices.usd_prices(mints)
        for mint in mints:
            if float(prices.get(mint, 0)) <= 0:
                continue
            report = await self.safety.evaluate(mint, min_liquidity_sol=0)
            for entry in [item for item in entries if item["token_mint"] == mint]:
                await self._fire(entry, report)

    async def _fire(self, entry: dict, report) -> None:
        self.metrics["watch_triggers"] = self.metrics.get("watch_triggers", 0) + 1
        telegram_id = entry["telegram_id"]
        chat_id = int(entry.get("chat_id") or telegram_id)
        if not report.ok_for_autosnipe:
            reason = "; ".join(report.blockers) or "safety checks failed"
            await self.db.set_watch_status(entry["id"], telegram_id, "aborted", error=reason[:500])
            await self.queue.notify(
                chat_id,
                "\n".join(
                    [
                        "🎯 <b>AUTOSNIPE BLOCKED</b>\nSafety checks failed. No buy was submitted.",
                        f"Reason: {esc(reason)}",
                        "",
                        info("The entry was removed. Add only tokens you trust. A report is not a guarantee."),
                    ]
                ),
            )
            self.metrics["snipes_blocked"] = self.metrics.get("snipes_blocked", 0) + 1
            return
        trade_id, reason = await self.queue.enqueue_watch_snipe(entry, report)
        if trade_id is None:
            if reason == "already":
                # A previous run queued the buy but the process stopped before
                # the entry was marked. Do not queue twice.
                await self.db.set_watch_status(entry["id"], telegram_id, "triggered")
                return
            await self.db.set_watch_status(entry["id"], telegram_id, "aborted", error=(reason or "buy not accepted")[:500])
            await self.queue.notify(
                chat_id,
                "\n".join(
                    [
                        "🎯 <b>AUTOSNIPE NOT QUEUED</b>\nThe buy was rejected before it entered the queue.",
                        f"Reason: {esc(reason)}",
                        "",
                        info("Nothing was signed. Review the reason before you add it again."),
                    ]
                ),
            )
            self.metrics["snipes_blocked"] = self.metrics.get("snipes_blocked", 0) + 1
            return
        await self.db.set_watch_status(entry["id"], telegram_id, "triggered")
