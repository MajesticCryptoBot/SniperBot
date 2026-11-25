"""Find Solana token mints anywhere in a message.

Professional bots detect tokens the way their users share them: a bare
CA, a CA inside a sentence ("check this 89Voaks… "), or a CA inside a
link (pump.fun, DexScreener, Solscan, Birdeye, Photon…). All of those are
the same thing at the text level — a 43-44 character base58 run — because
inside a URL the mint is delimited by non-base58 characters like ``/``,
``?`` or ``=``. One regex finds all of them.

Some share links carry a pool/pair address instead of the mint (the
DexScreener URL format is ``dexscreener.com/solana/<pair>``; Photon uses
``…/lp/<pair>``). :func:`resolve_pair_to_mint` maps such an address back
to its base token mint through DexScreener's search endpoint, which
answers for pair addresses (the plain token endpoint does not).
"""

from __future__ import annotations

import logging
import re

from sniper.prices import _best_pair, parse_dexscreener_pairs

logger = logging.getLogger(__name__)

# A Solana public key (mint or wallet) is 43-44 base58 characters.
# Transaction signatures are 64+, private keys 87+, and 64-char hex
# strings contain excluded characters (0, l, I, O), so they never match.
# The lookarounds keep a run from starting or ending mid-word: they make
# the match maximal, so "…/token/<mint>?…" yields exactly the mint.
_BASE58 = "1-9A-HJ-NP-Za-km-z"
# Both lookarounds are bracketed: an unbracketed lookbehind would match the
# literal text instead of the character set, letting the tail of a long
# base58 run (a transaction signature) pass as a mint.
_MINT_RE = re.compile(
    rf"(?<![{_BASE58}])[{_BASE58}]{{43,44}}(?![{_BASE58}])"
)


def extract_mints(text: str) -> list[str]:
    """Every distinct mint-shaped base58 run in reading order.

    Matches a bare CA, a CA embedded in a message and a CA inside a link
    (the caller still verifies on-chain that a candidate really is an SPL
    token mint before anything is shown or traded).
    """
    found: list[str] = []
    for match in _MINT_RE.findall(text or ""):
        if match not in found:
            found.append(match)
    return found


async def resolve_pair_to_mint(svc, address: str) -> str | None:
    """Base-token mint when DexScreener knows this address as a pool/pair.

    Only called after the on-chain mint check failed, so the address is
    never returned unchanged — a known pair maps to its base token, and
    anything else (a wallet, an unindexed pool) returns None.
    """
    try:
        response = await svc.prices.http.get(
            f"https://api.dexscreener.com/latest/dex/search?q={address}"
        )
        if response.status_code >= 400:
            return None
        pairs = [
            pair
            for pair in parse_dexscreener_pairs(response.json())
            if pair.get("pairAddress") == address
        ]
    except Exception as exc:
        logger.info("pair resolution failed for a pasted address: %s", exc.__class__.__name__)
        return None
    base = (_best_pair(pairs) or {}).get("baseToken") or {}
    mint = base.get("address")
    if isinstance(mint, str) and mint and mint != address:
        return mint
    return None
