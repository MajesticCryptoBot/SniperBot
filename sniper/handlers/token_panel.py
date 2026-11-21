"""Paste a token address in chat → the bot offers the options.

The user brings the address themselves (from an official announcement of
the project, for example) — as a bare CA, inside a message, or inside a
link; sniper/ca_detect.py extracts every mint-shaped address from the
text and resolves pool/pair links to their base token. Nothing is scanned
from the chain. The panel only appears after an on-chain check confirms
the address really is an SPL token mint, so a pasted wallet address or
garbage gets no panel.
"""

from __future__ import annotations

import logging

from telegram.error import BadRequest

from sniper.ca_detect import extract_mints, resolve_pair_to_mint
from sniper.errors import UserFacingError
from sniper.handlers import flows
from sniper.handlers.common import (
    callback_guard,
    ensure_private,
    ensure_subscribed,
    reply,
    services,
    tg_call,
)
from sniper.handlers.trading import run_scan
from sniper.handlers.trade_panel import open_buy_panel, open_sell_token_panel
from sniper.keyboards import token_options_keyboard
from sniper.solana_service import WSOL
from sniper.ui import card, esc
from sniper.ui import info as ui_info

logger = logging.getLogger(__name__)

NOT_A_TOKEN = (
    "<b>NOT A TOKEN MINT</b>\n"
    "Valid Solana address, but not a token mint. "
    "Send a token mint, or a supported token or pair link."
)
NONE_A_TOKEN = (
    "<b>NOT A TOKEN MINT</b>\n"
    "None of the {n} addresses is a Solana token mint. "
    "Send a token mint, or a supported token or pair link."
)


def _fmt_usd(value) -> str | None:
    """Compact USD formatting: $12.3K / $1.23M. None when unusable."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0:  # NaN or negative — no useful meaning here
        return None
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.0f}"


def _fmt_price(value) -> str | None:
    """Price formatting that survives memecoin precision ($0.00000123)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0:
        return None
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.10f}".rstrip("0")


def _panel_lines(address: str, info: dict, live: bool, profile: dict | None) -> list[str]:
    """Render the detection panel. Shared by the first paste and by Refresh,
    so both always show the same fields from the freshest data."""
    lines = [
        "<b>TOKEN DETECTED</b>",
        ui_info("Submitted by you. No order has been created."),
        "",
    ]
    body = [f"Mint: {esc(address)}"]
    if profile and (profile.get("name") or profile.get("symbol")):
        name = profile.get("name") or "Unknown"
        symbol = profile.get("symbol") or "?"
        body.append(f"Asset: {esc(name)} ({esc(symbol)})")
    if profile and profile.get("price_usd"):
        price_text = _fmt_price(profile["price_usd"])
        if price_text:
            body.append(f"Price: {price_text}")
    if profile and profile.get("liquidity_usd"):
        liquidity_text = _fmt_usd(profile["liquidity_usd"])
        if liquidity_text:
            dex = f" on {esc(profile['dex'])}" if profile.get("dex") else ""
            body.append(f"Liquidity: {liquidity_text}{dex}")
    if profile and profile.get("market_cap_usd"):
        cap_text = _fmt_usd(profile["market_cap_usd"])
        if cap_text:
            body.append(f"Market cap: {cap_text}")
    if profile and profile.get("volume_24h_usd"):
        volume_text = _fmt_usd(profile["volume_24h_usd"])
        if volume_text:
            body.append(f"24h volume: {volume_text}")
    change = profile.get("change_24h_pct") if profile else None
    if change is not None and change == change:
        sign = "+" if change >= 0 else ""
        body.append(f"24h change: {sign}{change:.1f}%")
    body.append("🟢 Market: LIVE" if live else "Market: NOT LIVE")
    if profile is None:
        body.append("Market data is not available yet. The token may be new, or not indexed yet.")
    lines.append(card(body))
    if info.get("freeze_authority"):
        lines.append("CRITICAL: freeze authority is active. The token can be frozen.")
    elif info.get("mint_authority"):
        lines.append("Mint authority is active. Use /scan for the full report.")
    lines.append("")
    lines.append(ui_info("Choose an action below. Refresh loads the latest data."))
    return lines


async def _scan_for_auto_buy(svc, mint: str):
    """Run the shared safety scan before an auto-buy.

    Returns (report, None) when the token may be bought, or (report_or_none, reason)
    when the buy must not be submitted.
    """
    safety = getattr(svc, "safety", None)
    if safety is None:
        return None, "Safety scan is not available."
    try:
        report = await safety.evaluate(mint, min_liquidity_sol=0)
    except Exception:
        logger.warning("auto-buy safety scan failed")
        return None, "Safety scan did not complete."
    if not report.ok_for_autosnipe:
        return report, "; ".join(report.blockers) or "safety checks failed"
    return report, None


async def _maybe_auto_buy(update, context, svc, mint: str, info: dict) -> bool:
    """Fire the user's auto-buy switch when a LIVE token is detected.

    With auto-buy ON, a detected token that already has a live market is
    scanned first. A flagged token is not bought. A clear scan queues the
    preset amount with no confirmation and no panel. Returns True when a
    buy was queued; False falls through to the regular detection panel.
    """
    user = update.effective_user
    if user is None:
        return False
    settings = await svc.db.get_settings(str(user.id))
    if not settings or not int(settings.get("auto_buy_enabled") or 0):
        return False
    amount = float(settings.get("auto_buy_amount_sol") or 0)
    if amount <= 0:
        return False
    if info.get("freeze_authority"):
        # The worst scam class. Auto-buy never touches these — the panel
        # that follows carries the freeze warning instead.
        return False
    report, blocked = await _scan_for_auto_buy(svc, mint)
    if blocked:
        flagged = report is not None and not report.ok_for_autosnipe
        title = "<b>AUTO-BUY BLOCKED</b>" if flagged else "<b>AUTO-BUY HELD</b>"
        lead = (
            "The safety scan flagged this token. No buy was submitted."
            if flagged
            else "The safety scan did not clear this token. No buy was submitted."
        )
        await reply(
            update,
            "\n".join(
                [
                    title,
                    ui_info(lead),
                    "",
                    card(
                        [
                            f"Mint: {esc(mint)}",
                            f"Reason: {esc(blocked)}",
                        ]
                    ),
                    "",
                    ui_info("The token panel opens for review. A manual buy still asks for confirmation."),
                ]
            ),
        )
        return False
    trade_id, reason = await svc.queue.enqueue_auto_buy(str(user.id), mint, amount)
    if trade_id is None:
        await reply(update, f"<b>AUTO-BUY NOT QUEUED</b>\n\nReason: {esc(reason)}\n\n{ui_info('The token panel opens so you can act manually.')}")
        return False  # the panel still opens so the user can act manually
    notes = bool(report and report.warnings)
    await reply(
        update,
        "\n".join(
            [
                "⚡️ <b>AUTO-BUY REQUEST QUEUED</b>",
                "",
                card(
                    [
                        f"Spend: {amount:.4f} SOL",
                        f"Mint: {esc(mint)}",
                        f"Scan: {'passed with notes' if notes else 'clear'}",
                        f"Reference: {trade_id}",
                    ]
                ),
                "",
                ui_info("Confirmation is skipped. A flagged scan is not bought. Switches are in /autosnipe."),
            ]
        ),
    )
    return True


async def _detect_one(update, context, svc, address: str) -> str:
    """One detection attempt.

    Returns "panel" when the options panel was shown, "auto" when the
    user's auto-buy switch fired instead, "error" when the user got a
    definitive answer (network problem, dead address), or "skip" when the
    address is not a mint — a shared pool/pair link is resolved to its
    base token first, and the caller may try other candidates from the
    same message before replying.
    """
    mint = address
    try:
        info = await svc.rpc.get_mint_info(mint)
    except UserFacingError as exc:
        if str(exc).startswith("The network"):
            await reply(update, esc(str(exc)))
            return "error"
        resolved = await resolve_pair_to_mint(svc, address)
        if not resolved:
            return "skip"
        mint = resolved
        try:
            info = await svc.rpc.get_mint_info(mint)
        except UserFacingError as exc:
            await reply(update, esc(str(exc)))
            return "error"
        except Exception:
            logger.warning("mint check failed for a resolved pair address")
            await reply(update, "<b>NETWORK</b>\nThe network did not respond. Try again shortly.")
            return "error"
    except Exception:
        logger.warning("mint check failed for a pasted address")
        await reply(update, "<b>NETWORK</b>\nThe network did not respond. Please try again shortly.")
        return "error"
    live = False
    try:
        prices = await svc.prices.usd_prices([mint])
        live = float(prices.get(mint, 0)) > 0
    except Exception:
        live = False
    if live and await _maybe_auto_buy(update, context, svc, mint, info):
        return "auto"
    profile = None
    try:
        profile = await svc.prices.token_profile(mint)
    except Exception:
        logger.warning("token profile lookup failed for a pasted address")
    await reply(update, "\n".join(_panel_lines(mint, info, live, profile)), token_options_keyboard(mint))
    return "panel"


async def detect_pasted_token(update, context, svc, address: str) -> bool:
    """Verify the address is a mint, then show the options panel.

    Returns True when the message was handled as a token, False when the
    caller should fall through to the regular unknown-text reply.
    """
    if address == WSOL:
        return False
    await _detect_one(update, context, svc, address)
    return True


async def detect_from_text(update, context, svc, text: str) -> bool:
    """Token detection for any message: a bare CA, a CA inside a sentence,
    or a CA inside a link (pump.fun, DexScreener, Solscan, Photon…).

    Every mint-shaped address in the message is tried in reading order, so
    a wallet address pasted next to the mint does not block detection.
    Returns True when the message was handled, False when it contained no
    mint-shaped address at all.
    """
    candidates = [c for c in extract_mints(text or "") if c != WSOL]
    if not candidates:
        return False
    for candidate in candidates:
        status = await _detect_one(update, context, svc, candidate)
        if status != "skip":
            return True
    if len(candidates) > 1:
        await reply(update, NONE_A_TOKEN.format(n=len(candidates)))
    else:
        await reply(update, NOT_A_TOKEN)
    return True


async def _refresh_token_panel(update, query, svc, mint: str) -> None:
    """Re-fetch market data and edit the panel in place, keeping it live."""
    try:
        info = await svc.rpc.get_mint_info(mint)
    except UserFacingError as exc:
        await reply(update, esc(str(exc)))
        return
    except Exception:
        logger.warning("mint check failed on token panel refresh")
        await reply(update, "<b>NETWORK</b>\nThe network did not respond. Refresh in a moment.")
        return
    live = False
    try:
        prices = await svc.prices.usd_prices([mint])
        live = float(prices.get(mint, 0)) > 0
    except Exception:
        live = False
    profile = None
    try:
        profile = await svc.prices.token_profile(mint)
    except Exception:
        logger.warning("token profile lookup failed on refresh")
    text = "\n".join(_panel_lines(mint, info, live, profile))
    try:
        await tg_call(
            query.edit_message_text,
            text,
            reply_markup=token_options_keyboard(mint),
            disable_web_page_preview=True,
            parse_mode="HTML",
        )
    except BadRequest as exc:
        # Rapid double-tap with unchanged data — Telegram rejects the edit.
        if "not modified" not in str(exc).lower():
            raise


@callback_guard
async def on_token_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if not await ensure_private(update):
        return
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "tok":
        return
    action, mint = parts[1], parts[2]
    svc = services(context)
    user = update.effective_user
    if user and not svc.limiter.allow(f"cmd:{user.id}", svc.cfg.commands_per_30s, 30):
        await reply(update, "<b>PLEASE WAIT</b>\nToo many requests. Try again in a moment.")
        return
    if action == "close":
        await query.edit_message_reply_markup(None)
        return
    if not await ensure_subscribed(update, context):
        return
    if action == "refresh":
        await _refresh_token_panel(update, query, svc, mint)
        return
    if action in {"buy", "sell", "auto"}:
        row = await svc.db.get_user(str(user.id))
        if not row or not row.get("wallet_address"):
            await reply(update, "<b>NO WALLET</b>\nComplete /start first.")
            return
    if action == "auto":
        if await svc.db.get_state("global_pause") == "1":
            await reply(update, "<b>TRADING PAUSED</b>\nTry again later.")
            return
        await flows.begin(update, context, "autosnipe_add", prefill={"mint": mint})
        return
    if action == "buy":
        await open_buy_panel(update, context, svc, mint)
        return
    if action == "sell":
        row = await svc.db.get_user(str(user.id))
        accounts = await svc.rpc.get_token_accounts(row["wallet_address"])
        if not any(item["mint"] == mint for item in accounts):
            await reply(update, "<b>NO BALANCE</b>\nThis wallet does not hold the token. Buy it first.")
            return
        await open_sell_token_panel(update, context, svc, mint)
        return
    if action == "scan":
        await run_scan(update, svc, mint)
        return
