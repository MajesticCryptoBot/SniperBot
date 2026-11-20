"""Take-profit / stop-loss auto-sell for open positions (Maestro-style).

Every position bought through the bot carries a cost basis. When the user
switches TP/SL on, this monitor polls their open positions each cycle,
computes the price move against the averaged entry, and auto-sells the
whole holding when a level is crossed:

- take_profit — price is +take_profit_pct% above entry
- stop_loss   — price is -stop_loss_pct% below entry

PnL is measured in SOL terms (entry SOL per token from the cost basis,
current SOL per token from the USD price over the live SOL price), so a
moving SOL price does not distort the level. Fires go through the same
trade queue as every other trade, so spend guards, per-user
serialization, idempotency keys, and result notifications all apply. A
position is disarmed the moment its level fires; the next buy re-arms it
against the new averaged entry. Console output here is deliberately
coarse: no mints, no signatures.
"""

from __future__ import annotations

import asyncio
import logging

from sniper.solana_service import WSOL
from sniper.ui import esc, info

logger = logging.getLogger(__name__)


def evaluate_position(
    row: dict,
    price_usd: float | None,
    sol_usd: float | None,
    take_profit_pct,
    stop_loss_pct,
) -> tuple[str, float | None]:
    """Decide what one open position should do at the current prices.

    `row` needs tokens_whole and entry_sol_per_token. Returns
    ("wait", None), ("take_profit", pnl_pct) or ("stop_loss", pnl_pct).
    The stop-loss wins when both levels are crossed in one tick —
    protecting capital comes first. Pure function so it is easy to test.
    """
    if price_usd is None or price_usd <= 0 or sol_usd is None or sol_usd <= 0:
        return "wait", None
    tokens = float(row.get("tokens_whole") or 0)
    entry = float(row.get("entry_sol_per_token") or 0)
    if tokens <= 0 or entry <= 0:
        return "wait", None
    current_sol_per_token = float(price_usd) / float(sol_usd)
    pnl_pct = (current_sol_per_token / entry - 1) * 100
    # A hair of tolerance: float division can land a hair below the exact
    # level (0.3/0.2 → 1.4999…), and a level that is exactly reached must fire.
    sl = float(stop_loss_pct or 0)
    if sl > 0 and pnl_pct <= -sl + 1e-9:
        return "stop_loss", pnl_pct
    tp = float(take_profit_pct or 0)
    if tp > 0 and pnl_pct >= tp - 1e-9:
        return "take_profit", pnl_pct
    return "wait", None


class PositionMonitor:
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
        logger.info("position monitor started")
        while not self._stop:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                logger.warning("position tick failed: %s", exc)
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
        rows = await self.db.positions_for_tp_sl()
        if not rows:
            return
        self.metrics["position_ticks"] = self.metrics.get("position_ticks", 0) + 1
        mints: list[str] = []
        for row in rows:
            if row["token_mint"] not in mints:
                mints.append(row["token_mint"])
        if WSOL not in mints:
            # Levels are expressed in SOL terms — the SOL price is needed.
            mints.append(WSOL)
        prices = await self.prices.usd_prices(mints)
        for row in rows:
            try:
                await self._handle(row, prices)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("position %s/%s evaluation failed: %s", row.get("telegram_id"), row.get("token_mint"), exc)
                self.metrics["position_errors"] = self.metrics.get("position_errors", 0) + 1

    async def _mint_decimals(self, mint: str) -> int | None:
        if mint in self._decimals:
            return self._decimals[mint]
        try:
            info = await self.rpc.get_mint_info(mint)
        except Exception:
            return None  # retried on the next tick; only successes are cached
        decimals = int(info.get("decimals") or 0)
        self._decimals[mint] = decimals
        return decimals

    async def _handle(self, row: dict, prices: dict[str, float]) -> None:
        decimals = await self._mint_decimals(row["token_mint"])
        if decimals is None:
            return
        tokens_whole = int(row["amount_raw"]) / (10**decimals)
        entry_sol = float(row["cost_basis_sol"]) / tokens_whole if tokens_whole > 0 else 0.0
        enriched = {"tokens_whole": tokens_whole, "entry_sol_per_token": entry_sol}
        decision, pnl = evaluate_position(
            enriched,
            prices.get(row["token_mint"]),
            prices.get(WSOL),
            row.get("take_profit_pct"),
            row.get("stop_loss_pct"),
        )
        if decision == "wait":
            return
        self.metrics["tp_sl_fires"] = self.metrics.get("tp_sl_fires", 0) + 1
        trade_id, reason = await self.queue.enqueue_auto_sell(row, decision, pnl, prices.get(row["token_mint"]))
        await self.db.set_position_armed(row["telegram_id"], row["token_mint"], False)
        if trade_id is None:
            if reason == "already":
                # A previous run queued the sell but the process stopped
                # before the position was disarmed. Do not queue twice.
                return
            await self.queue.notify(
                int(row.get("chat_id") or row["telegram_id"]),
                "\n".join(
                    [
                        "<b>EXIT NOT QUEUED</b>\nThe take-profit or stop-loss sell was rejected. The position is disarmed.",
                        f"Reason: {esc(reason)}",
                        "",
                        info("Sell manually if you still want out. A new buy arms the levels again."),
                    ]
                ),
            )
