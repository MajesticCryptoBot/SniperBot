from __future__ import annotations

import logging

from sniper.errors import UserFacingError
from sniper.handlers import flows
from sniper.handlers.common import command, reply, services
from sniper.solana_service import WSOL
from sniper.ui import card, esc, info, mono, sol

logger = logging.getLogger(__name__)

UNKNOWN_COMMAND = "Unknown command. Send /help."


def _require_admin(update, context) -> None:
    svc = services(context)
    user_id = update.effective_user.id
    if user_id not in svc.cfg.admin_ids:
        # Administrative commands are internal: non-admins get the same neutral
        # response as any unrecognized command.
        raise UserFacingError(UNKNOWN_COMMAND)


@command("admin_stats")
async def admin_stats(update, context) -> None:
    _require_admin(update, context)
    svc = services(context)
    users = await svc.db.count_users()
    counts = await svc.db.count_trades_by_status()
    paused = await svc.db.get_state("global_pause")
    watch_active = await svc.db.count_watch_entries("active")
    lines = [
        "<b>ADMIN · SYSTEM</b>",
        "",
        card(
            [
                f"Network: {esc(svc.cfg.network)}",
                f"Users: {users}",
                f"Execution: {'PAUSED' if paused == '1' else 'ACTIVE'}",
                f"Queue: {svc.queue.queue.qsize()} / {svc.cfg.trade_queue_max}",
                f"Workers: {svc.cfg.max_concurrent_swaps}",
                f"Watchlist active: {watch_active}",
                f"Watchlist monitor: {esc(svc.monitor.status_line())}",
                f"Trigger monitor: {esc(svc.trigger_monitor.status_line())}",
                f"TP/SL monitor: {esc(svc.positions_monitor.status_line())}",
                f"Subscription cache: {svc.gate.cache_size()}",
                "Trades: " + (", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"),
                "Counters: " + (", ".join(f"{key}={value}" for key, value in sorted(svc.metrics.items())) or "none"),
            ]
        ),
    ]
    await reply(update, "\n".join(lines))


@command("admin_pause")
async def admin_pause(update, context) -> None:
    _require_admin(update, context)
    svc = services(context)
    await svc.db.set_state("global_pause", "1")
    await reply(update, "<b>GLOBAL EXECUTION PAUSED</b>\nNo new swaps or withdrawals will be submitted.")


@command("admin_resume")
async def admin_resume(update, context) -> None:
    _require_admin(update, context)
    svc = services(context)
    await svc.db.set_state("global_pause", "0")
    await reply(update, "<b>GLOBAL EXECUTION RESUMED</b>\nAccount and risk controls still apply.")


@command("admin_user")
async def admin_user(update, context) -> None:
    _require_admin(update, context)
    args = list(context.args or [])
    prefill = {"telegram_id": args[0]} if args else None
    await flows.begin(update, context, "admin_user", prefill=prefill)


@command("admin_gate")
async def admin_gate(update, context) -> None:
    _require_admin(update, context)
    svc = services(context)
    lines = ["<b>ADMIN · GATE</b>", info("Live check. Failures are not cached."), ""]
    for channel in svc.cfg.channels():
        try:
            chat = await context.bot.get_chat(channel)
            title = getattr(chat, "title", None) or getattr(chat, "username", None) or "ok"
            lines.append(f"OK · {esc(channel)} · {esc(title)}")
        except Exception as exc:
            lines.append(f"FAIL · {esc(channel)} · {esc(exc)}")
    result = await svc.gate.check(context.bot, update.effective_user.id, force=True)
    if result.admin_error:
        lines.append("Membership check error:")
        lines.append(esc(result.error_detail or result.user_message))
    elif result.ok:
        lines.append("You are in both channels.")
    else:
        lines.append("Missing at least one channel:")
        lines.append(esc(result.error_detail or result.user_message))
    await reply(update, "\n".join(lines))


@command("admin_fees")
async def admin_fees(update, context) -> None:
    """Bot fee status: configuration, audit totals and claimable balances."""
    _require_admin(update, context)
    svc = services(context)
    cfg = svc.cfg
    if not cfg.jupiter_referral_account:
        await reply(
            update,
            "\n".join(
                [
                    "<b>BOT FEE · OFF</b>",
                    "JUP_REFERRAL_ACCOUNT is not set.",
                    "",
                    "To charge the fee on sell volume:",
                    "1. Enroll the fee wallet at https://referral.jup.ag/",
                    "2. Create a referral token account for WSOL.",
                    "3. Set JUP_REFERRAL_ACCOUNT and BOT_FEE_BPS=100 (range 50–255).",
                    "4. Restart the bot.",
                ]
            ),
        )
        return
    lines = [
        "<b>BOT FEE</b>",
        "",
        card(
            [
                f"Status: {'ACTIVE' if cfg.fee_active else 'OFF (BOT_FEE_BPS=0)'}",
                f"Rate: {cfg.bot_fee_bps / 100:.2f}% of sell volume",
                f"Referral: {esc(cfg.jupiter_referral_account)}",
            ]
        ),
    ]
    totals = await svc.db.fee_ledger_totals()
    if totals:
        lines.append("")
        lines.append("<b>CHARGED</b>")
        for row in totals:
            value = f" · ≈{sol(row['value_sol'])}" if row["fee_mint"] == WSOL else ""
            lines.append(f"  {row['fee_mint']}: {row['trades']} trade(s){value}")
    try:
        claimable = await svc.rpc.get_token_accounts(cfg.jupiter_referral_account)
    except Exception as exc:
        logger.warning("fee claimable lookup failed: %s", exc.__class__.__name__)
        claimable = []
    if claimable:
        lines.append("")
        lines.append("<b>CLAIMABLE</b>")
        for item in claimable:
            label = " (SOL)" if item["mint"] == WSOL else ""
            lines.append(f"  {item['mint']}{label}: {item['ui_amount']:.6f}")
    elif cfg.fee_active:
        lines.append("")
        lines.append("Claimable: none.")
    lines.append("")
    lines.append(info("Claim at https://referral.jup.ag/ · audit rows are in fee_ledger."))
    await reply(update, "\n".join(lines))


async def admin_inspect(update, context, svc, telegram_id: str) -> None:
    _require_admin(update, context)
    user = await svc.db.get_user(telegram_id)
    if not user:
        raise UserFacingError("No account for that Telegram user ID.")
    settings = await svc.db.get_settings(telegram_id)
    trades = await svc.db.recent_trades(telegram_id, limit=5)
    watch = await svc.db.watchlist_for(telegram_id)
    waiting = sum(1 for item in watch if item["status"] == "active")
    lines = [
        f"<b>ADMIN · USER {esc(telegram_id)}</b>",
        "",
        card(
            [
                f"Username: {esc(user.get('username'))}",
                f"Wallet: {mono(user.get('wallet_address'))}",
                f"Paused: {'YES' if user['trading_paused'] else 'NO'}",
                f"🎯 Autosnipe: {('🟢 ON' if settings and settings['autosnipe_enabled'] else '🔴 OFF')} · {waiting} waiting",
                f"Max SOL: {settings['max_sol_per_trade'] if settings else 'n/a'}",
                "Recent: " + ("; ".join(f"{row['side']} {row['status']}" for row in trades) or "none"),
            ]
        ),
    ]
    await reply(update, "\n".join(lines))


def _register_flows() -> None:
    flows.register(
        "admin_user",
        [flows.Step("telegram_id", "<b>INSPECT USER</b>\n\nSend the Telegram user ID.", flows.parse_telegram_id)],
        lambda update, context, data: admin_inspect(update, context, services(context), data["telegram_id"]),
    )


_register_flows()
