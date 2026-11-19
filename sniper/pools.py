"""Best-effort detection of new SOL pairs from Raydium initialization transactions."""

from __future__ import annotations

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {WSOL, USDC, USDT}

RAYDIUM_PROGRAMS = (
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # CLMM
)

POOL_LOG_MARKERS = (
    "initialize2",
    "init_pc_amount",
    "initializepool",
    "createpool",
    "instruction: initialize",
    "instruction: createpool",
)


def logs_look_like_pool_init(logs: list[str] | None) -> bool:
    if not logs:
        return False
    blob = "\n".join(logs).lower()
    return any(marker in blob for marker in POOL_LOG_MARKERS)


def extract_pool_candidate(tx: dict | None) -> dict | None:
    """Return {'mint', 'liquidity_sol'} when a confirmed tx holds WSOL plus exactly one other mint.

    This is intentionally conservative. Ambiguous transactions are ignored.
    Liquidity is the summed WSOL ui amount in the transaction, which for a pool
    init is the opening SOL side. It is an approximation, not an oracle.
    """
    if not isinstance(tx, dict):
        return None
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    totals: dict[str, float] = {}
    for balance in meta.get("postTokenBalances") or []:
        if not isinstance(balance, dict):
            continue
        mint = balance.get("mint")
        if not mint:
            continue
        ui = (balance.get("uiTokenAmount") or {}).get("uiAmount")
        try:
            amount = float(ui or 0)
        except (TypeError, ValueError):
            amount = 0.0
        totals[mint] = totals.get(mint, 0.0) + amount
    if totals.get(WSOL, 0) <= 0:
        return None
    others = [mint for mint in totals if mint not in QUOTE_MINTS]
    if len(others) != 1:
        return None
    return {"mint": others[0], "liquidity_sol": totals[WSOL]}
