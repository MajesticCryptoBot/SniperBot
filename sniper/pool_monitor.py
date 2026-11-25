"""Watch Raydium programs for new pools.

Primary path: outbound WebSocket logsSubscribe (works from a home PC behind NAT).
Fallback: getSignaturesForAddress polling when the socket is down.
Helius webhooks are intentionally not used — they need a public URL.
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets

from sniper.pools import RAYDIUM_PROGRAMS, extract_pool_candidate, logs_look_like_pool_init
from sniper.solana_service import http_to_ws
from sniper.timeutil import utcnow

logger = logging.getLogger(__name__)


class PoolMonitor:
    def __init__(self, cfg, db, rpc, safety, queue, metrics: dict):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.safety = safety
        self.queue = queue
        self.metrics = metrics
        self.ws_ok = False
        self.last_error = ""
        self._stop = False

    async def run(self) -> None:
        poll_task = asyncio.create_task(self._poll_loop(), name="pool-poll")
        ws_task = asyncio.create_task(self._ws_loop(), name="pool-ws")
        try:
            await asyncio.gather(poll_task, ws_task)
        except asyncio.CancelledError:
            poll_task.cancel()
            ws_task.cancel()
            raise

    async def _ws_loop(self) -> None:
        backoff = 2
        url = http_to_ws(self.cfg.rpc_urls[0])
        while not self._stop:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2_000_000) as socket:
                    self.ws_ok = True
                    self.last_error = ""
                    backoff = 2
                    self.metrics["ws_connects"] = self.metrics.get("ws_connects", 0) + 1
                    for index, program in enumerate(RAYDIUM_PROGRAMS, start=1):
                        request = {
                            "jsonrpc": "2.0",
                            "id": index,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [program]}, {"commitment": "confirmed"}],
                        }
                        await socket.send(json.dumps(request))
                    async for raw in socket:
                        unsupported = await self._on_ws_message(raw)
                        if unsupported:
                            self.ws_ok = False
                            self.last_error = "RPC refused logsSubscribe; using signature polling only"
                            logger.warning(self.last_error)
                            return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ws_ok = False
                self.last_error = f"{exc.__class__.__name__}: logsSubscribe disconnected"
                logger.warning("logsSubscribe disconnected: %s", exc.__class__.__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _on_ws_message(self, raw: str) -> bool:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return False
        if data.get("error") and data.get("id") in {1, 2, 3}:
            return True
        if data.get("method") != "logsNotification":
            return False
        value = (((data.get("params") or {}).get("result") or {}).get("value") or {})
        signature = value.get("signature")
        if not signature or value.get("err"):
            return False
        logs = value.get("logs") or []
        if not logs_look_like_pool_init(logs):
            return False
        await self._handle_signature(signature)
        return False

    async def _poll_loop(self) -> None:
        while not self._stop:
            try:
                if not self.ws_ok:
                    await self._poll_once()
                    self.metrics["poll_cycles"] = self.metrics.get("poll_cycles", 0) + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"poll failed: {exc.__class__.__name__}"
                logger.warning("pool poll failed: %s", exc.__class__.__name__)
            try:
                await asyncio.sleep(self.cfg.pool_poll_seconds)
            except asyncio.CancelledError:
                raise

    async def _poll_once(self) -> None:
        for program in RAYDIUM_PROGRAMS:
            signatures = await self.rpc.get_signatures(program, limit=15)
            for item in signatures:
                if item.get("err"):
                    continue
                signature = item.get("signature")
                if signature:
                    await self._handle_signature(signature, fetch_logs=True)

    async def _handle_signature(self, signature: str, fetch_logs: bool = False) -> None:
        fresh = await self.db.mark_seen_pool(signature, None, None)
        if not fresh:
            return
        self.metrics["pools_seen"] = self.metrics.get("pools_seen", 0) + 1
        try:
            tx = await self.rpc.get_transaction(signature)
        except Exception as exc:
            logger.info("getTransaction failed for a pool candidate: %s", exc.__class__.__name__)
            return
        if fetch_logs:
            logs = ((tx or {}).get("meta") or {}).get("logMessages") or []
            if not logs_look_like_pool_init(logs):
                return
        candidate = extract_pool_candidate(tx)
        if not candidate:
            return
        mint = candidate["mint"]
        liquidity = float(candidate["liquidity_sol"])
        await self.db.execute(
            "UPDATE seen_pools SET token_mint=?, liquidity_sol=? WHERE pool_key=?",
            (mint, liquidity, signature),
        )
        logger.info(
            "pool candidate mint=%s liquidity_sol=%.4f signature=%s at %s",
            mint,
            liquidity,
            signature,
            utcnow(),
        )
        await self._fanout(mint, liquidity)

    async def _fanout(self, mint: str, liquidity: float) -> None:
        if not self.cfg.is_mainnet:
            logger.info("skipping auto-snipe because SOLANA_NETWORK is not mainnet-beta")
            return
        if await self.db.get_state("global_pause") == "1":
            logger.info("global pause set; not enqueueing snipes")
            return
        users = await self.db.autosnipe_users()
        if not users:
            return
        interested = [user for user in users if liquidity + 1e-9 >= float(user["autosnipe_min_liquidity_sol"])]
        if not interested:
            logger.info("pool liquidity %.2f SOL matched nobody's minimum", liquidity)
            return
        report = await self.safety.evaluate(mint, liquidity_sol=liquidity, min_liquidity_sol=0)
        # Per-user min liquidity was already applied. Global blockers still apply.
        if not report.ok_for_autosnipe:
            logger.info("auto-snipe skipped mint=%s reasons=%s", mint, "; ".join(report.blockers))
            self.metrics["snipes_blocked"] = self.metrics.get("snipes_blocked", 0) + 1
            return
        chosen = await self._rotate(interested)
        for user in chosen:
            await self.queue.enqueue_autosnipe(user, mint, liquidity, report)

    async def _rotate(self, users: list[dict]) -> list[dict]:
        cap = self.cfg.max_snipes_per_pool
        if len(users) <= cap:
            return users
        raw = await self.db.get_state("snipe_cursor")
        try:
            cursor = int(raw or "0")
        except ValueError:
            cursor = 0
        start = cursor % len(users)
        rotated = users[start:] + users[:start]
        await self.db.set_state("snipe_cursor", str(cursor + cap))
        logger.info("auto-snipe fanout capped at %s of %s users", cap, len(users))
        return rotated[:cap]
