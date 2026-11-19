"""Solana RPC with failover.

solders + solana-py are used for balances, blockhashes, sending, and confirmation.
Parsed account calls go through JSON-RPC on the same URL list because the parsed
shapes are easier to keep stable.
"""

from __future__ import annotations

import logging

import httpx
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.pubkey import Pubkey
from solders.signature import Signature

from sniper.errors import UserFacingError

logger = logging.getLogger(__name__)

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WSOL = "So11111111111111111111111111111111111111112"


class SolanaService:
    def __init__(self, urls: list[str], http: httpx.AsyncClient | None = None):
        if not urls:
            raise UserFacingError("The service is not available right now. Please try again later.")
        self.urls = urls
        self.http = http
        self._owns_http = http is None
        self._clients: list[AsyncClient] = []
        self._index = 0

    async def start(self) -> None:
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
            self._owns_http = True
        self._clients = [AsyncClient(url, timeout=20) for url in self.urls]

    async def aclose(self) -> None:
        for client in self._clients:
            try:
                await client.close()
            except Exception:
                logger.warning("failed to close an RPC client")
        self._clients.clear()
        if self._owns_http and self.http is not None:
            await self.http.aclose()
            self.http = None

    async def _with_client(self, operation):
        if not self._clients:
            raise UserFacingError("The service is starting up. Please try again in a moment.")
        last = None
        for attempt in range(len(self._clients)):
            client = self._clients[(self._index + attempt) % len(self._clients)]
            try:
                return await operation(client)
            except UserFacingError:
                raise
            except Exception as exc:
                last = exc
                logger.warning("RPC attempt %s failed: %s", attempt + 1, exc.__class__.__name__)
                self._index = (self._index + 1) % len(self._clients)
        logger.warning("all RPC endpoints failed: %s", last)
        raise UserFacingError("The network is not responding right now. Please try again shortly.")

    async def rpc(self, method: str, params: list):
        if self.http is None:
            raise UserFacingError("The service is starting up. Please try again in a moment.")
        last = None
        for attempt in range(len(self.urls)):
            url = self.urls[(self._index + attempt) % len(self.urls)]
            try:
                response = await self.http.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"])[:500])
                return payload.get("result")
            except Exception as exc:
                last = exc
                logger.warning("JSON-RPC %s attempt %s failed: %s", method, attempt + 1, exc.__class__.__name__)
                self._index = (self._index + 1) % len(self.urls)
        logger.warning("JSON-RPC %s failed: %s", method, last)
        raise UserFacingError("The network request failed. Please try again shortly.")

    async def get_balance_lamports(self, pubkey: str) -> int:
        owner = Pubkey.from_string(pubkey)

        async def operation(client: AsyncClient):
            response = await client.get_balance(owner, commitment=Confirmed)
            return int(response.value)

        return await self._with_client(operation)

    async def latest_blockhash(self) -> str:
        async def operation(client: AsyncClient):
            response = await client.get_latest_blockhash(commitment=Confirmed)
            return str(response.value.blockhash)

        return await self._with_client(operation)

    async def send_raw(self, raw: bytes) -> str:
        opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=2)

        async def operation(client: AsyncClient):
            response = await client.send_raw_transaction(raw, opts=opts)
            return str(response.value)

        try:
            return await self._with_client(operation)
        except UserFacingError as exc:
            raise UserFacingError(_friendly_rpc(str(exc))) from exc

    async def confirm(self, signature: str, timeout_s: float = 45) -> tuple[bool, str | None]:
        import asyncio
        import time

        sig = Signature.from_string(signature)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                status = await self._signature_status(sig)
            except UserFacingError:
                # RPC hiccup during polling; keep trying until the deadline.
                await asyncio.sleep(1)
                continue
            if status is not None:
                err = getattr(status, "err", None)
                if err:
                    return False, _friendly_rpc(str(err))
                conf = str(getattr(status, "confirmation_status", "") or "").lower()
                if "confirmed" in conf or "finalized" in conf:
                    return True, None
            await asyncio.sleep(1)
        return False, "Confirmation is taking longer than expected. The transaction was submitted and may still confirm."

    async def _signature_status(self, sig: Signature):
        async def operation(client: AsyncClient):
            response = await client.get_signature_statuses([sig])
            values = response.value
            return values[0] if values else None

        return await self._with_client(operation)

    async def get_mint_info(self, mint: str) -> dict:
        try:
            Pubkey.from_string(mint)
        except Exception as exc:
            raise UserFacingError("The supplied mint is not a valid Solana public key.") from exc
        result = await self.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        value = (result or {}).get("value")
        if not value:
            raise UserFacingError("The token mint could not be found on-chain.")
        parsed = ((value.get("data") or {}).get("parsed") or {})
        if parsed.get("type") != "mint":
            raise UserFacingError("The supplied address is not an SPL token mint.")
        info = parsed.get("info") or {}
        return {
            "decimals": int(info.get("decimals") or 0),
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "supply": str(info.get("supply") or "0"),
        }

    async def get_token_accounts(self, owner: str) -> list[dict]:
        accounts: list[dict] = []
        for program in (TOKEN_PROGRAM, TOKEN_2022):
            result = await self.rpc(
                "getTokenAccountsByOwner",
                [owner, {"programId": program}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
            for item in (result or {}).get("value") or []:
                info = (((item.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                token_amount = info.get("tokenAmount") or {}
                raw = int(token_amount.get("amount") or 0)
                if raw <= 0:
                    continue
                accounts.append(
                    {
                        "mint": info.get("mint"),
                        "amount_raw": raw,
                        "decimals": int(token_amount.get("decimals") or 0),
                        "ui_amount": float(token_amount.get("uiAmount") or 0),
                        "program": program,
                    }
                )
        return accounts

    async def get_transaction(self, signature: str) -> dict | None:
        result = await self.rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 1}],
        )
        return result

    async def transaction_fee(self, signature: str) -> int | None:
        try:
            tx = await self.get_transaction(signature)
        except UserFacingError:
            return None
        if not tx:
            return None
        fee = (tx.get("meta") or {}).get("fee")
        return int(fee) if fee is not None else None


def _friendly_rpc(text: str) -> str:
    low = text.lower()
    if "blockhash" in low:
        return "The transaction expired before confirmation because its blockhash was no longer valid. Request a fresh quote and try again."
    if "insufficient" in low or "0x1" in low:
        return "Insufficient SOL to cover the requested amount, network fees and account rent."
    if "attempt to debit" in low:
        return "The wallet does not contain the required token or SOL balance for this transaction."
    trimmed = text.replace("\n", " ")
    if len(trimmed) > 350:
        trimmed = trimmed[:350] + "..."
    return f"The transaction was rejected by the network: {trimmed}"
