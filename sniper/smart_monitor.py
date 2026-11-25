"""Smart Trailing monitor — the phased profit-locking exit, evaluated live.

Every cfg.smart_poll_seconds this monitor loads the open Smart Trailing
orders, resolves the SOL-stable price ratio for each (token USD price over
the live SOL price, against the position's averaged cost basis), and feeds
one tick into the phase engine (smart_trailing.py):

- advance — a profit threshold was crossed; the floor tightens and the
  user gets a phase notification with the locked percentage
- ratchet — the floor moved up silently inside the current phase
- fire    — the floor was touched; the full position is sold through the
  same trade queue as every other trade, so spend guards, per-user
  serialization, idempotency keys, and result notifications all apply

A position without holdings cancels its order (nothing left to protect).
Console output here is deliberately coarse: no mints, no signatures.
"""

from __future__ import annotations

import asyncio
import logging

from sniper.smart_trailing import PHASE_COUNT, evaluate, phase_name
from sniper.ui import card, esc, info

logger = logging.getLogger(__name__)

# Local copy (as in prices.py): keeps this module importable without the
# solana SDK — the monitor only needs the mint string, never the client.
WSOL = "So11111111111111111111111111111111111111112"


class SmartMonitor:
    def __init__(self, cfg, db, queue, prices, rpc, metrics: dict):
        self.cfg = cfg
        self.db = db
        self.queue = queue
        self.prices = prices
        self.rpc = rpc
        self.metrics = metrics
        self.last_error = ""
        self._stop = False
        self._decimals: dict[str, int] = {}

    def status_line(self) -> str:
        return self.last_error or "running"

    async def run(self) -> None:
        logger.info("smart trailing monitor started")
        while not self._stop:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                logger.warning("smart trailing tick failed: %s", exc)
            try:
                await asyncio.sleep(self.cfg.smart_poll_seconds)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        if not self.cfg.is_mainnet:
            # Execution is refused downstream anyway; skip the network chatter.
            return
        if await self.db.get_state("global_pause") == "1":
            return
        rows = await self.db.open_smart_triggers()
        if not rows:
            return
        self.metrics["smart_ticks"] = self.metrics.get("smart_ticks", 0) + 1
        mints: list[str] = []
        for row in rows:
            if row["token_mint"] not in mints:
                mints.append(row["token_mint"])
        if WSOL not in mints:
            # Ratios are SOL-stable — the SOL price is needed every tick.
            mints.append(WSOL)
        prices = await self.prices.usd_prices(mints)
        for row in rows:
            try:
                await self._handle(row, prices)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("smart trigger %s evaluation failed: %s", row.get("id"), exc)
                self.metrics["smart_errors"] = self.metrics.get("smart_errors", 0) + 1

    async def _mint_decimals(self, mint: str) -> int | None:
        if mint in self._decimals:
            return self._decimals[mint]
        try:
            mint_info = await self.rpc.get_mint_info(mint)
        except Exception:
            return None  # retried on the next tick; only successes are cached
        decimals = int(mint_info.get("decimals") or 0)
        self._decimals[mint] = decimals
        return decimals

    async def _handle(self, row: dict, prices: dict[str, float]) -> None:
        token_usd = prices.get(row["token_mint"])
        sol_usd = prices.get(WSOL)
        if not token_usd or token_usd <= 0 or not sol_usd or sol_usd <= 0:
            return  # no usable prices this tick; retried next cycle
        position = await self.db.position_for(row["telegram_id"], row["token_mint"])
        tokens_whole = 0.0
        if position and position.get("amount_raw"):
            decimals = await self._mint_decimals(row["token_mint"])
            if decimals is None:
                return
            tokens_whole = int(position["amount_raw"]) / (10**decimals)
        if tokens_whole <= 0 or not position or float(position.get("cost_basis_sol") or 0) <= 0:
            # Nothing left to protect — the position was closed or sold.
            await self.db.set_smart_status(row["id"], "cancelled", error="position closed")
            await self.queue.notify(
                int(row.get("chat_id") or row["telegram_id"]),
                "\n".join(
                    [
                        "🎯 <b>SMART TRAILING DISARMED</b>",
                        "No open position is left for this token, so the order was released.",
                        "",
                        info("A new arm from the sell panel protects the next position."),
                    ]
                ),
            )
            return

        entry_ratio = float(position["cost_basis_sol"]) / tokens_whole
        current_ratio = float(token_usd) / float(sol_usd)
        decision, state, tick = evaluate(
            current_ratio,
            entry_ratio,
            int(row.get("phase") or 0),
            row.get("peak_ratio"),
            row.get("floor_ratio"),
        )
        if decision == "wait":
            return

        if decision in {"advance", "ratchet"}:
            await self.db.update_smart_state(row["id"], state["phase"], state["peak_ratio"], state["floor_ratio"])
        if decision == "ratchet":
            return

        chat_id = int(row.get("chat_id") or row["telegram_id"])
        if decision == "advance":
            self.metrics["smart_phase_advances"] = self.metrics.get("smart_phase_advances", 0) + 1
            await self.queue.notify(chat_id, self._phase_message(row, tick))
            return

        # fire — the floor was touched: sell the whole position once.
        self.metrics["smart_fires"] = self.metrics.get("smart_fires", 0) + 1
        locked = float(tick.get("locked_pct") or 0.0)
        sell_row = {
            "telegram_id": row["telegram_id"],
            "token_mint": row["token_mint"],
            "amount_raw": position["amount_raw"],
            "cost_basis_sol": position["cost_basis_sol"],
            "updated_at": position["updated_at"],
            "chat_id": row.get("chat_id"),
            "wallet_address": row["wallet_address"],
        }
        trade_id, reason = await self.queue.enqueue_auto_sell(sell_row, "smart_trailing", locked, token_usd)
        if trade_id is None:
            if reason == "already":
                # A previous run queued the sell but the order was not closed
                # yet. Do not queue twice; the idempotency key holds.
                return
            await self.db.set_smart_status(row["id"], "failed", error=(reason or "not accepted")[:500])
            await self.queue.notify(
                chat_id,
                "\n".join(
                    [
                        "<b>SMART TRAILING EXIT NOT QUEUED</b>\nThe sell was rejected and the order has been disarmed.",
                        f"Reason: {esc(reason)}",
                        "",
                        info("Sell manually from /sell if you still want out."),
                    ]
                ),
            )
            return
        await self.db.set_smart_status(row["id"], "triggered", trade_id=trade_id)
        # The whole position is on its way out; the TP/SL flag must not
        # fire on the remnant afterwards.
        await self.db.set_position_armed(row["telegram_id"], row["token_mint"], False)

    def _phase_message(self, row: dict, tick: dict) -> str:
        """User notification when a profit phase locks in."""
        index = int(tick.get("phase") or 0)
        name = phase_name(index)
        profit = float(tick.get("pnl_pct") or 0.0)
        locked = float(tick.get("locked_pct") or 0.0)
        short = f"{row['token_mint'][:6]}…{row['token_mint'][-4:]}"
        return "\n".join(
            [
                f"🎯 <b>SMART TRAILING — {esc(name)}</b>",
                f"<i>Phase {index + 1} of {PHASE_COUNT} · profit is being locked</i>",
                "",
                card(
                    [
                        f"Token: {esc(short)}",
                        f"Profit: +{profit:.1f}%",
                        f"Floor now locks ≈ {locked:+.1f}%",
                    ]
                ),
                "",
                info("Exit fires if the floor is touched — full position, with a report. /triggers shows it live."),
            ]
        )
