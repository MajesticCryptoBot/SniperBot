"""Automatic Trading — the glue that makes one switch run the whole job.

The user-facing contract: read the guide (/auto), toggle ON, paste tokens.
Everything else is automatic — scan, buy (through the normal preview/guard
pipeline), and then this module arms 🎯 Smart Trailing on every freshly
confirmed buy so the position is managed from the first tick.

Pure logic only: the TradeQueue calls :func:`auto_arm_after_buy` after a
confirmed buy, passing its own database, price service, metrics and notify
callback. Keeping the logic out of the queue class keeps it importable and
testable without the solders SDK.
"""

from __future__ import annotations

import logging

from sniper.amounts import sol_to_lamports
from sniper.ui import card, esc, info, sol

logger = logging.getLogger(__name__)

# Reserve for an automatic buy: base fee + ATA rent for a new token
# account. The priority fee comes from the user's settings at call time.
BASE_FEE_LAMPORTS = 5_000
ATA_RENT_LAMPORTS = 2_100_000


def balance_shortfall(balance_lamports: int, amount_sol: float, priority_fee_lamports: int) -> str | None:
    """The wallet check for automatic buys, run before anything is queued.

    The quote needs real lamports: refuse an underfunded wallet up front
    with the real reason instead of letting Jupiter fail the swap later
    with a bare "Insufficient funds" the user cannot act on. Returns the
    refusal text, or None when the balance covers the buy plus fees.
    """
    required = sol_to_lamports(amount_sol) + BASE_FEE_LAMPORTS + int(priority_fee_lamports) + ATA_RENT_LAMPORTS
    if balance_lamports >= required:
        return None
    return (
        f"Wallet balance {sol(balance_lamports / 1_000_000_000)} is below the {sol(amount_sol)} buy size "
        f"plus fees ({sol(required / 1_000_000_000)} needed). Fund the wallet and paste the token again."
    )


def auto_buy_allowed(settings: dict | None) -> bool:
    """Paste-buys fire when either the auto-buy or the Automatic Trading switch is on."""
    settings = settings or {}
    return (
        int(settings.get("auto_buy_enabled") or 0) == 1
        or int(settings.get("auto_trading_enabled") or 0) == 1
    )


async def auto_arm_after_buy(db, prices, metrics: dict, notify, trade: dict) -> None:
    """Arm 🎯 Smart Trailing on a freshly bought position.

    Runs after every confirmed buy when the user's Automatic Trading switch
    is on — paste-buys, panel buys, and watchlist buys all land here. The
    arm itself is a database row plus one notification; the SmartMonitor
    manages the position from there and every exit goes back through the
    trade queue. Failures never undo the buy — they only leave it untrailed.
    """
    try:
        settings = await db.get_settings(trade["telegram_id"])
        if not settings or int(settings.get("auto_trading_enabled") or 0) != 1:
            return
        user = await db.get_user(trade["telegram_id"])
        if not user or user.get("trading_paused"):
            return
        mint = trade["token_mint"]
        position = await db.position_for(trade["telegram_id"], mint)
        if not position or not position.get("amount_raw") or int(position["amount_raw"]) <= 0:
            return
        if float(position.get("cost_basis_sol") or 0) <= 0:
            return
        trigger_id = await db.add_smart_trigger(trade["telegram_id"], mint)
        # One exit manager per position: the TP/SL flag stays off while
        # the Smart Trailing floor is armed.
        await db.set_position_armed(trade["telegram_id"], mint, False)
        metrics["smart_auto_armed"] = metrics.get("smart_auto_armed", 0) + 1
        chat_id = int(trade.get("chat_id") or trade["telegram_id"])
        await notify(chat_id, await armed_message(prices, mint, trigger_id))
    except Exception as exc:
        logger.warning("auto-arm after buy failed: %s", exc)


async def armed_message(prices, mint: str, trigger_id: int) -> str:
    """User notification that Automatic Trading has taken over a position."""
    symbol = ""
    try:
        profile = await prices.token_profile(mint)
        if profile and profile.get("symbol"):
            symbol = f"{profile['symbol']} · "
    except Exception:
        symbol = ""
    return "\n".join(
        [
            "🤖 <b>SMART TRAILING ARMED</b>",
            "<i>Automatic Trading is managing this position.</i>",
            "",
            card(
                [
                    f"Token: {symbol}{esc(mint)}",
                    f"Order #{trigger_id} · Phase RUN (1/6)",
                    "Floor: 25% below position value, following every new high",
                ]
            ),
            "",
            info("Messages arrive at every phase. /triggers shows the live floor."),
        ]
    )
