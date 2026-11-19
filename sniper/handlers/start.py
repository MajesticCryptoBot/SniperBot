from __future__ import annotations

import logging

from solders.keypair import Keypair

from sniper.handlers.common import (
    best_effort_answer,
    callback_guard,
    command,
    ensure_private,
    ensure_subscribed,
    reply,
    services,
)
from sniper.keyboards import help_menu_keyboard, help_topic_keyboard, join_keyboard
from sniper.texts import HELP, HELP_MENU, HELP_SECTIONS, START_INTRO
from sniper.ui import esc, info

logger = logging.getLogger(__name__)


async def _onboard(update, context, *, already_member: bool) -> None:
    svc = services(context)
    user = update.effective_user
    chat = update.effective_chat
    telegram_id = str(user.id)
    existing = await svc.db.get_user(telegram_id)
    if existing:
        await svc.db.touch_user(telegram_id, user.username, str(chat.id))
        wallet = existing["wallet_address"]
        created = False
    else:
        keypair = Keypair()
        payload = svc.cipher.encrypt_json(bytes(keypair))
        wallet = str(keypair.pubkey())
        del keypair
        await svc.db.create_user(
            telegram_id=telegram_id,
            username=user.username,
            chat_id=str(chat.id),
            wallet_address=wallet,
            enc_payload=payload,
            defaults={
                "slippage_bps": svc.cfg.default_slippage_bps,
                "priority_fee_lamports": svc.cfg.default_priority_fee,
                "max_sol_per_trade": svc.cfg.default_max_sol,
                "daily_spend_limit_sol": svc.cfg.default_daily_sol,
                "autosnipe_amount_sol": svc.cfg.default_snipe_sol,
            },
        )
        created = True
        logger.info("wallet created for a new user")
    header = "🔓 <b>ACCESS VERIFIED</b>" if already_member else "✅ <b>ACCOUNT READY</b>"
    lines = [
        header,
        "",
        "💼 Wallet",
        esc(wallet),
        "",
        "Created." if created else "Loaded.",
        "",
        "<b>NEXT STEPS</b>",
        "1. 💰 Fund the wallet with SOL",
        "2. 🛒 /buy or 💸 /sell to trade",
        "3. 💼 /wallet for balance and controls",
        "4. 📋 /help for all commands",
    ]
    await reply(update, "\n".join(lines))


@command("start", gated=False)
async def start(update, context) -> None:
    svc = services(context)
    await reply(update, START_INTRO, join_keyboard(svc.cfg))
    if await ensure_subscribed(update, context, force=True):
        await _onboard(update, context, already_member=True)


@command("help")
async def help_command(update, context) -> None:
    await reply(update, HELP, help_menu_keyboard())


@callback_guard
async def on_help_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    data = (query.data or "").split(":")
    if len(data) != 2 or data[0] != "help":
        return
    topic = data[1]
    if topic == "main":
        text, markup = HELP, help_menu_keyboard()
    else:
        text = HELP_SECTIONS.get(topic)
        markup = help_topic_keyboard()
    if text is None:
        return
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass


@callback_guard
async def on_check_sub(update, context) -> None:
    if not await ensure_private(update):
        return
    query = update.callback_query
    svc = services(context)
    user = update.effective_user
    if user and not svc.limiter.allow(f"sub:{user.id}", 6, 60):
        await best_effort_answer(query, "⏳ Please wait a moment before checking again.")
        return
    await best_effort_answer(query, "🔎 Checking membership…")
    if await ensure_subscribed(update, context, force=True):
        await _onboard(update, context, already_member=True)
