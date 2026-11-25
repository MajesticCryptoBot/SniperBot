"""Background loop for /automate jobs.

One task, started next to the other monitors. It never reads watchlist
rows, trade triggers, or Smart Trailing orders. Buys and sells go through
the trade queue, so the same spend guards and signing path apply.

While a job is managing the position, TP/SL is disarmed on that mint so
the two exit engines cannot both sell it. Cancelling the job does not
sell; it only stops this loop from touching the mint.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sniper.automate_logic import (
    any_kept,
    best_valid,
    dip_decision,
    indicators_from_candles,
    init_dip,
    init_trail,
    parse_gecko_ohlcv,
    phase_name,
    ratchet_dip,
    score_patterns,
    trail_tick,
    PHASES,
    PHASE_COUNT,
)
from sniper.prices import WSOL
from sniper.ui import card, esc, info, sol

logger = logging.getLogger(__name__)

ACTIVE = ("scanning", "dip", "buying", "managing", "selling")
CANDLE_TTL = 55.0
GECKO_URL = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/ohlcv/minute"


class AutomateMonitor:
    def __init__(self, cfg, db, queue, prices, safety, metrics: dict):
        self.cfg = cfg
        self.db = db
        self.queue = queue
        self.prices = prices
        self.safety = safety
        self.metrics = metrics
        self.last_error = ""
        self._stop = False
        self._candles: dict[str, tuple[float, list]] = {}

    def status_line(self) -> str:
        return self.last_error or "running"

    async def run(self) -> None:
        logger.info("automate monitor started")
        interval = float(getattr(self.cfg, "automate_poll_seconds", 12.0) or 12.0)
        while not self._stop:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                logger.warning("automate tick failed: %s", exc)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        if await self.db.get_state("global_pause") == "1":
            return
        rows = await self.db.open_automate_jobs()
        if not rows:
            return
        self.metrics["automate_ticks"] = self.metrics.get("automate_ticks", 0) + 1
        mints = []
        for row in rows:
            if row["token_mint"] not in mints:
                mints.append(row["token_mint"])
        if WSOL not in mints:
            mints.append(WSOL)
        try:
            prices = await self.prices.usd_prices(mints)
        except Exception as exc:
            logger.warning("automate prices failed: %s", exc.__class__.__name__)
            return
        for row in rows:
            try:
                await self._handle(row, prices)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("automate job %s failed: %s", row.get("id"), exc)
                self.metrics["automate_errors"] = self.metrics.get("automate_errors", 0) + 1

    async def _handle(self, row: dict, prices: dict) -> None:
        if int(row.get("trading_paused") or 0) == 1:
            return
        status = row["status"]
        if status == "scanning":
            await self._scan(row, prices)
        elif status == "dip":
            await self._dip(row, prices)
        elif status == "buying":
            await self._await_buy(row, prices)
        elif status == "managing":
            await self._trail(row, prices)
        elif status == "selling":
            await self._await_sell(row)

    async def _indicators(self, mint: str, price: float):
        candles = await self._candles_for(mint)
        ind = indicators_from_candles(candles) if candles else None
        if ind and price > 0:
            ind["price"] = price
        return ind

    async def _candles_for(self, mint: str) -> list | None:
        cached = self._candles.get(mint)
        now = time.monotonic()
        if cached and now - cached[0] < CANDLE_TTL:
            return cached[1]
        http = getattr(self.prices, "http", None)
        if http is None:
            return cached[1] if cached else None
        rows = await self._fetch_gecko(http, mint)
        if not rows and getattr(self.prices, "birdeye_api_key", ""):
            rows = await self._fetch_birdeye(http, mint)
        if rows:
            self._candles[mint] = (now, rows)
            return rows
        return cached[1] if cached else None

    async def _fetch_gecko(self, http, mint: str) -> list:
        try:
            response = await http.get(
                GECKO_URL.format(mint=mint),
                params={"aggregate": 5, "limit": 100, "currency": "usd"},
                headers={"Accept": "application/json"},
            )
            if response.status_code >= 400:
                return []
            return parse_gecko_ohlcv(response.json())
        except Exception as exc:
            logger.info("automate candles failed: %s", exc.__class__.__name__)
            return []

    async def _fetch_birdeye(self, http, mint: str) -> list:
        try:
            response = await http.get(
                "https://public-api.birdeye.so/defi/ohlcv",
                params={"address": mint, "type": "5m", "currency": "usd"},
                headers={
                    "X-API-KEY": self.prices.birdeye_api_key,
                    "x-chain": "solana",
                    "accept": "application/json",
                },
            )
            if response.status_code >= 400:
                return []
            items = (response.json().get("data") or {}).get("items") or []
        except Exception as exc:
            logger.info("automate birdeye candles failed: %s", exc.__class__.__name__)
            return []
        parsed = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                parsed.append(
                    (
                        int(item.get("unixTime") or item.get("unix_time") or 0),
                        float(item["o"]),
                        float(item["h"]),
                        float(item["l"]),
                        float(item["c"]),
                        float(item.get("v") or 0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        parsed.sort(key=lambda item: item[0])
        return parsed

    def _chat(self, row: dict) -> int:
        return int(row.get("chat_id") or row["telegram_id"])

    async def _say(self, row: dict, text: str) -> None:
        await self.queue.notify(self._chat(row), text)

    async def _scan(self, row: dict, prices: dict) -> None:
        price = prices.get(row["token_mint"])
        if not price or price <= 0:
            return
        ind = await self._indicators(row["token_mint"], price)
        if not ind:
            return
        winner = best_valid(score_patterns(ind, price))
        if winner is None:
            return
        distance, level, profile = init_dip(winner["number"], price, ind.get("atr") or 0.0, ind.get("volatility_index") or 0.0)
        await self.db.update_automate_job(
            row["id"],
            status="dip",
            pattern=winner["number"],
            pattern_name=winner["name"],
            pattern_score=winner["match_pct"],
            dip_distance=distance,
            dip_level=level,
            dip_trigger_price=price,
            dip_occurred=0,
            error=None,
        )
        self.metrics["automate_dips"] = self.metrics.get("automate_dips", 0) + 1
        await self._say(
            row,
            "\n".join(
                [
                    f"📉 <b>TRAILING DIP · P{winner['number']}</b>",
                    f"<i>{esc(winner['name'])} · score {winner['match_pct']}</i>",
                    "",
                    card(
                        [
                            f"Token: {esc(row['token_mint'])}",
                            f"Profile: {esc(profile)}",
                            f"Price: ${price:.8g}",
                            f"Buy level: ${level:.8g}",
                            f"Distance: {distance / price * 100:.2f}%",
                        ]
                    ),
                    "",
                    info("The level only moves down. A rebound into it buys. No buy yet."),
                ]
            ),
        )

    async def _dip(self, row: dict, prices: dict) -> None:
        price = prices.get(row["token_mint"])
        if not price or price <= 0:
            return
        ind = await self._indicators(row["token_mint"], price)
        patterns = score_patterns(ind, price) if ind else []
        winner = best_valid(patterns)
        pattern = int(row.get("pattern") or (winner or {}).get("number") or 18)
        level = float(row.get("dip_level") or 0)
        distance = float(row.get("dip_distance") or 0)
        trigger = float(row.get("dip_trigger_price") or price)
        if distance <= 0 or level <= 0:
            await self.db.update_automate_job(row["id"], status="scanning", dip_level=None, dip_distance=None)
            return
        new_level, dipped = ratchet_dip(price, trigger, level, distance)
        if new_level != level or (dipped and not int(row.get("dip_occurred") or 0)):
            await self.db.update_automate_job(
                row["id"],
                dip_level=new_level,
                dip_occurred=1 if dipped else int(row.get("dip_occurred") or 0),
                pattern_score=(winner or {}).get("match_pct"),
            )
            level = new_level
        atr_percent = float((ind or {}).get("atr_percent") or 0.0)
        action = dip_decision(price, level, pattern, atr_percent)
        if action == "escape":
            await self.db.update_automate_job(
                row["id"],
                status="scanning",
                dip_level=None,
                dip_distance=None,
                dip_trigger_price=None,
                dip_occurred=0,
                pattern=None,
            )
            await self._say(
                row,
                "\n".join(
                    [
                        "<b>TRAILING DIP CANCELLED</b>",
                        "Price ran through the bounce without a fill. Scanning again.",
                        "",
                        card([f"Token: {esc(row['token_mint'])}", f"Price: ${price:.8g}"]),
                    ]
                ),
            )
            return
        if action == "buy":
            if winner is None:
                return  # rebound, but the pattern is no longer acceptable
            await self._buy(row, price, winner)
            return
        if patterns and not any_kept(patterns):
            await self.db.update_automate_job(
                row["id"],
                status="scanning",
                dip_level=None,
                dip_distance=None,
                dip_trigger_price=None,
                dip_occurred=0,
                pattern=None,
            )
            await self._say(
                row,
                "\n".join(
                    [
                        "<b>TRAILING DIP RELEASED</b>",
                        "P15 and P18 both faded. Scanning again. Nothing was bought.",
                        "",
                        card([f"Token: {esc(row['token_mint'])}"]),
                    ]
                ),
            )

    async def _buy(self, row: dict, price: float, winner: dict) -> None:
        if not self.cfg.is_mainnet:
            return
        report = None
        if self.safety is not None:
            try:
                report = await self.safety.evaluate(row["token_mint"])
            except Exception as exc:
                logger.warning("automate safety check failed: %s", exc.__class__.__name__)
                return
            if report is not None and not report.ok_for_autosnipe:
                await self.db.update_automate_job(
                    row["id"],
                    status="failed",
                    error=("; ".join(report.blockers) or "safety blocked")[:500],
                )
                await self._say(
                    row,
                    "\n".join(
                        [
                            "<b>AUTOMATE STOPPED</b>",
                            "The rebound was there, but the safety check blocked the buy.",
                            "",
                            report.summary(),
                        ]
                    ),
                )
                return
        attempt = int(row.get("buy_attempt") or 0)
        trade_id, reason = await self.queue.enqueue_automate_buy(row, attempt)
        if trade_id is None:
            if reason == "already":
                await self.db.update_automate_job(row["id"], status="buying", pattern=winner["number"], pattern_score=winner["match_pct"])
                return
            await self.db.update_automate_job(row["id"], error=(reason or "buy rejected")[:500])
            await self._say(
                row,
                "\n".join(
                    [
                        "<b>AUTOMATE BUY NOT QUEUED</b>",
                        f"Reason: {esc(reason or 'rejected')}",
                        "",
                        info("The dip stays armed. /automate_off releases it without buying."),
                    ]
                ),
            )
            return
        await self.db.update_automate_job(
            row["id"],
            status="buying",
            pattern=winner["number"],
            pattern_name=winner["name"],
            pattern_score=winner["match_pct"],
            buy_trade_id=trade_id,
            entry_price_usd=price,
        )
        self.metrics["automate_buys"] = self.metrics.get("automate_buys", 0) + 1

    async def _await_buy(self, row: dict, prices: dict) -> None:
        trade_id = row.get("buy_trade_id")
        if not trade_id:
            await self.db.update_automate_job(row["id"], status="dip")
            return
        trade = await self.db.get_trade(trade_id)
        if not trade:
            return
        status = trade.get("status")
        if status in {"pending", "submitted"}:
            return
        if status != "confirmed":
            attempt = int(row.get("buy_attempt") or 0) + 1
            await self.db.update_automate_job(
                row["id"],
                status="dip",
                buy_attempt=attempt,
                buy_trade_id=None,
                error=(trade.get("error") or status or "buy failed")[:500],
            )
            await self._say(
                row,
                "\n".join(
                    [
                        "<b>AUTOMATE BUY FAILED</b>",
                        "The dip is still armed and will try the next rebound.",
                        "",
                        card([f"Reason: {esc(trade.get('error') or status)}"]),
                    ]
                ),
            )
            return
        position = await self.db.position_for(row["telegram_id"], row["token_mint"])
        if not position or int(position.get("amount_raw") or 0) <= 0:
            return
        cost = float(position.get("cost_basis_sol") or 0)
        raw = int(position.get("amount_raw") or 0)
        if cost <= 0 or raw <= 0:
            return
        decimals = await self._decimals(row["token_mint"])
        if decimals is None:
            return
        tokens = raw / (10 ** decimals)
        if tokens <= 0:
            return
        entry_ratio = cost / tokens
        trail = init_trail(entry_ratio)
        await self.db.set_position_armed(row["telegram_id"], row["token_mint"], False)
        await self.db.update_automate_job(
            row["id"],
            status="managing",
            entry_ratio=entry_ratio,
            profit_phase=trail["phase"],
            trailing_distance=trail["distance"],
            trailing_stop=trail["stop"],
            trail_peak=trail["peak"],
            last_ratchet_price=trail["last_ratchet"],
            error=None,
        )
        sol_usd = prices.get(WSOL) or 0
        token_usd = prices.get(row["token_mint"]) or float(row.get("entry_price_usd") or 0)
        await self._say(
            row,
            "\n".join(
                [
                    "🎯 <b>TRAILING STOP ARMED</b>",
                    "<i>Automate is managing this position. TP/SL stays off for this mint until the job ends.</i>",
                    "",
                    card(
                        [
                            f"Token: {esc(row['token_mint'])}",
                            f"Phase: RUN (1/{PHASE_COUNT})",
                            f"Buffer: {PHASES[0][4] * 100:.2f}% under the print",
                            f"Price: ${token_usd:.8g}" if token_usd else "Price: n/a",
                            f"SOL: ${sol_usd:.4f}" if sol_usd else None,
                        ]
                    ),
                    "",
                    info("Phases: RUN, BREATHE, LOCK, STEEL, HARVEST, MOONSHOT. The stop never moves down."),
                ]
            ),
        )

    async def _decimals(self, mint: str) -> int | None:
        rpc = getattr(self.queue, "rpc", None)
        if rpc is None:
            return None
        try:
            info = await rpc.get_mint_info(mint)
        except Exception:
            return None
        return int(info.get("decimals") or 0)

    async def _trail(self, row: dict, prices: dict) -> None:
        await self.db.set_position_armed(row["telegram_id"], row["token_mint"], False)
        token_usd = prices.get(row["token_mint"])
        sol_usd = prices.get(WSOL)
        if not token_usd or token_usd <= 0 or not sol_usd or sol_usd <= 0:
            return
        position = await self.db.position_for(row["telegram_id"], row["token_mint"])
        if not position or int(position.get("amount_raw") or 0) <= 0:
            await self.db.update_automate_job(row["id"], status="done", error="position closed")
            await self._say(
                row,
                "\n".join(
                    [
                        "<b>AUTOMATE FINISHED</b>",
                        "No tokens left on this mint, so the trailing stop was released.",
                    ]
                ),
            )
            return
        entry = float(row.get("entry_ratio") or 0)
        if entry <= 0:
            return
        current = float(token_usd) / float(sol_usd)
        decision, state = trail_tick(
            current,
            entry,
            int(row.get("profit_phase") or 0),
            float(row.get("trailing_distance") or 0),
            float(row.get("trailing_stop") or 0),
            row.get("trail_peak"),
            row.get("last_ratchet_price"),
        )
        if decision == "wait":
            return
        await self.db.update_automate_job(
            row["id"],
            profit_phase=state["phase"],
            trailing_distance=state["distance"],
            trailing_stop=state["stop"],
            trail_peak=state["peak"],
            last_ratchet_price=state["last_ratchet"],
        )
        if decision == "ratchet":
            return
        if decision == "advance":
            name = phase_name(state["phase"])
            await self._say(
                row,
                "\n".join(
                    [
                        f"🎯 <b>AUTOMATE · {esc(name)}</b>",
                        f"<i>Phase {int(state['phase']) + 1} of {PHASE_COUNT}</i>",
                        "",
                        card(
                            [
                                f"Token: {esc(row['token_mint'])}",
                                f"Profit: {state['pnl_pct']:+.2f}%",
                                f"Locked: {state['locked_pct']:+.2f}%",
                                f"Trail: {state['distance'] / current * 100:.2f}%",
                            ]
                        ),
                    ]
                ),
            )
            return
        if not self.cfg.is_mainnet:
            return
        attempt = int(row.get("sell_attempt") or 0)
        trade_id, reason = await self.queue.enqueue_automate_sell(row, position, state.get("pnl_pct") or 0.0, token_usd, attempt)
        if trade_id is None:
            if reason != "already":
                await self.db.update_automate_job(row["id"], error=(reason or "sell rejected")[:500])
                await self._say(
                    row,
                    "\n".join(
                        [
                            "<b>AUTOMATE SELL NOT QUEUED</b>",
                            f"Reason: {esc(reason or 'rejected')}",
                            "",
                            info("The stop stays armed and will try again. /sell still works."),
                        ]
                    ),
                )
            return
        await self.db.update_automate_job(row["id"], status="selling", sell_trade_id=trade_id)
        self.metrics["automate_sells"] = self.metrics.get("automate_sells", 0) + 1

    async def _await_sell(self, row: dict) -> None:
        trade = await self.db.get_trade(row.get("sell_trade_id"))
        if not trade:
            return
        status = trade.get("status")
        if status in {"pending", "submitted"}:
            return
        if status == "confirmed":
            await self.db.update_automate_job(row["id"], status="done", error=None)
            await self.db.set_position_armed(row["telegram_id"], row["token_mint"], False)
            return
        attempt = int(row.get("sell_attempt") or 0) + 1
        await self.db.update_automate_job(
            row["id"],
            status="managing",
            sell_attempt=attempt,
            sell_trade_id=None,
            error=(trade.get("error") or status or "sell failed")[:500],
        )
        await self._say(
            row,
            "\n".join(
                [
                    "<b>AUTOMATE SELL FAILED</b>",
                    "The trailing stop stays armed and will try again.",
                    "",
                    card([f"Reason: {esc(trade.get('error') or status)}"]),
                ]
            ),
        )
