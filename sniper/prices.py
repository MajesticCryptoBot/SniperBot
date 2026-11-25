from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

WSOL = "So11111111111111111111111111111111111111112"


def parse_jupiter_prices(payload) -> dict[str, float]:
    """Accept Jupiter price v3 `{mint: {usdPrice}}` and v2 `{data: {mint: {price}}}`."""
    prices: dict[str, float] = {}
    if not isinstance(payload, dict):
        return prices
    source = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for mint, info in source.items():
        if mint in {"data", "timeTaken"} or not isinstance(info, dict):
            continue
        raw = info.get("usdPrice", info.get("price"))
        try:
            prices[mint] = float(raw)
        except (TypeError, ValueError):
            continue
    return prices


def parse_dexscreener_pairs(payload) -> list[dict]:
    """Normalize a DexScreener token response into a list of pair dicts."""
    if isinstance(payload, dict):
        pairs = payload.get("pairs")
    elif isinstance(payload, list):
        pairs = payload
    else:
        pairs = None
    if not isinstance(pairs, list):
        return []
    return [pair for pair in pairs if isinstance(pair, dict)]


def _best_pair(pairs: list[dict]) -> dict | None:
    """The deepest-liquidity pair carries the most reliable market data."""
    best = None
    best_usd = -1.0
    for pair in pairs:
        try:
            usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)
        except (TypeError, ValueError):
            usd = 0.0
        if usd > best_usd:
            best, best_usd = pair, usd
    return best


def _profile_from_pair(pair: dict) -> dict | None:
    """Market snapshot dict from one DexScreener pair (None when unusable)."""
    if not isinstance(pair, dict):
        return None
    base = pair.get("baseToken") or {}
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    change = pair.get("priceChange") or {}

    def _float(container, key):
        try:
            return float(container.get(key))
        except (TypeError, ValueError):
            return None

    return {
        "name": (base.get("name") or "")[:48],
        "symbol": base.get("symbol") or "",
        "price_usd": _float(pair, "priceUsd"),
        "liquidity_usd": _float(liquidity, "usd"),
        "market_cap_usd": _float(pair, "marketCap") or _float(pair, "fdv"),
        "volume_24h_usd": _float(volume, "h24"),
        "change_24h_pct": _float(change, "h24"),
        "dex": pair.get("dexId") or "",
    }


async def _fetch_pairs(http: httpx.AsyncClient, mints: list[str]) -> dict[str, list[dict]]:
    """DexScreener pairs per mint (batched, up to 30 mints per request)."""
    unique = []
    for mint in mints:
        if mint and mint not in unique:
            unique.append(mint)
    result: dict[str, list[dict]] = {}
    for start in range(0, len(unique), 30):
        chunk = unique[start:start + 30]
        try:
            response = await http.get(f"https://api.dexscreener.com/tokens/v1/solana/{','.join(chunk)}")
            if response.status_code >= 400:
                continue
            payload = response.json()
        except Exception as exc:
            logger.info("DexScreener batch lookup failed: %s", exc.__class__.__name__)
            continue
        pairs = payload if isinstance(payload, list) else (payload.get("pairs") if isinstance(payload, dict) else [])
        for pair in pairs or []:
            if not isinstance(pair, dict):
                continue
            address = (pair.get("baseToken") or {}).get("address")
            if address:
                result.setdefault(address, []).append(pair)
    return result


class PriceService:
    def __init__(self, http: httpx.AsyncClient, jupiter_price_url: str, jupiter_api_key: str = "", birdeye_api_key: str = ""):
        self.http = http
        self.jupiter_price_url = jupiter_price_url
        self.jupiter_api_key = jupiter_api_key
        self.birdeye_api_key = birdeye_api_key

    async def usd_prices(self, mints: list[str]) -> dict[str, float]:
        unique = []
        for mint in mints:
            if mint and mint not in unique:
                unique.append(mint)
        if WSOL not in unique:
            unique.append(WSOL)
        prices = await self._jupiter(unique)
        missing = [mint for mint in unique if mint not in prices]
        if missing and self.birdeye_api_key:
            for mint in missing:
                price = await self._birdeye(mint)
                if price is not None:
                    prices[mint] = price
        return prices

    async def _jupiter(self, mints: list[str]) -> dict[str, float]:
        if not mints:
            return {}
        headers = {"Accept": "application/json"}
        if self.jupiter_api_key:
            headers["x-api-key"] = self.jupiter_api_key
        try:
            response = await self.http.get(
                self.jupiter_price_url,
                params={"ids": ",".join(mints)},
                headers=headers,
            )
            response.raise_for_status()
            return parse_jupiter_prices(response.json())
        except Exception as exc:
            logger.warning("Jupiter price request failed: %s", exc.__class__.__name__)
            return {}

    async def _birdeye(self, mint: str) -> float | None:
        try:
            response = await self.http.get(
                "https://public-api.birdeye.so/defi/price",
                params={"address": mint},
                headers={"X-API-KEY": self.birdeye_api_key, "accept": "application/json"},
            )
            response.raise_for_status()
            value = (response.json().get("data") or {}).get("value")
            return float(value) if value is not None else None
        except Exception as exc:
            logger.warning("Birdeye price failed for one mint: %s", exc.__class__.__name__)
            return None

    async def pair_liquidity_sol(self, mint: str, sol_usd: float | None) -> float | None:
        try:
            response = await self.http.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
            if response.status_code >= 400:
                return None
            payload = response.json()
        except Exception as exc:
            logger.info("DexScreener lookup failed: %s", exc.__class__.__name__)
            return None
        pairs = payload if isinstance(payload, list) else (payload.get("pairs") if isinstance(payload, dict) else [])
        best = None
        for pair in pairs or []:
            if not isinstance(pair, dict):
                continue
            quote = ((pair.get("quoteToken") or {}).get("symbol") or "").upper()
            liquidity = pair.get("liquidity") or {}
            candidate = None
            if quote in {"SOL", "WSOL"} and liquidity.get("quote") is not None:
                try:
                    candidate = float(liquidity["quote"])
                except (TypeError, ValueError):
                    candidate = None
            elif sol_usd and liquidity.get("usd") is not None:
                try:
                    candidate = float(liquidity["usd"]) / sol_usd
                except (TypeError, ValueError, ZeroDivisionError):
                    candidate = None
            if candidate is not None:
                best = candidate if best is None else max(best, candidate)
        return best

    async def token_profile(self, mint: str) -> dict | None:
        """Market snapshot for one mint from DexScreener (free, no API key).

        Returns None when the token is unlisted or the service is unreachable;
        callers must degrade gracefully.
        """
        try:
            response = await self.http.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
            if response.status_code >= 400:
                return None
            payload = response.json()
        except Exception as exc:
            logger.info("DexScreener token profile failed: %s", exc.__class__.__name__)
            return None
        return _profile_from_pair(_best_pair(parse_dexscreener_pairs(payload)))

    async def token_profiles(self, mints: list[str]) -> dict[str, dict]:
        """Batch market snapshots keyed by mint (one request per 30 mints).

        Missing mints are simply absent from the result; callers degrade.
        """
        try:
            pairs_by_mint = await _fetch_pairs(self.http, mints)
        except Exception as exc:
            logger.info("DexScreener batch profiles failed: %s", exc.__class__.__name__)
            return {}
        profiles: dict[str, dict] = {}
        for mint, pairs in pairs_by_mint.items():
            profile = _profile_from_pair(_best_pair(pairs))
            if profile:
                profiles[mint] = profile
        return profiles
