from __future__ import annotations

import logging
import time

from telegram.ext import ContextTypes, ConversationHandler

from sniper.amounts import lamports_to_sol
from sniper.handlers import flows
from sniper.handlers.common import best_effort_answer, callback_guard, command, ensure_private, ensure_subscribed, reply, services
from sniper.handlers.portfolio import summary
from sniper.handlers.settings import config_command
from sniper.keyboards import import_confirm_keyboard, import_go_keyboard, wallet_keyboard
from sniper.signing import parse_keypair
from sniper.texts import IMPORT_WARNING
from sniper.ui import esc, info

logger = logging.getLogger(__name__)

WAIT_ACK, WAIT_KEY, WAIT_CONFIRM = range(3)
IMPORT_TTL = 180


@command("wallet", needs_wallet=True)
async def wallet(update, context) -> None:
    svc = services(context)
    user_id = str(update.effective_user.id)
    row = await svc.db.get_user(user_id)
    try:
        lamports = await svc.rpc.get_balance_lamports(row["wallet_address"])
        balance = f"{lamports_to_sol(lamports):.6f} SOL"
    except Exception as exc:
        from sniper.errors import UserFacingError

        if isinstance(exc, UserFacingError):
            balance = str(exc)
        else:
            balance = "Temporarily unavailable"
    paused = "🟢 ACTIVE" if not row["trading_paused"] else "🔴 PAUSED"
    await reply(
        update,
        "\n".join(
            [
                "💼 <b>WALLET</b>",
                "",
                "Address",
                esc(row["wallet_address"]),
                "",
                f"💰 Balance: {esc(balance)}",
                f"Trading: {paused}",
            ]
        ),
        wallet_keyboard(),
    )


@callback_guard
async def on_wallet_callback(update, context) -> None:
    query = update.callback_query
    await best_effort_answer(query, "")
    if not await ensure_private(update):
        return
    action = (query.data or "").removeprefix("wal:")
    svc = services(context)
    user = update.effective_user
    if user and not svc.limiter.allow(f"cmd:{user.id}", svc.cfg.commands_per_30s, 30):
        await reply(update, "⏳ Rate limit reached. Please wait a moment.")
        return
    if not await ensure_subscribed(update, context):
        return
    if action == "withdraw":
        await flows.begin(update, context, "withdraw")
        return
    if action == "summary":
        await summary(update, context)
        return
    if action == "refresh":
        await wallet(update, context)
        return
    if action == "config":
        await config_command(update, context)
        return


async def import_start(update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_subscribed(update, context):
        return ConversationHandler.END
    row = await services(context).db.get_user(str(update.effective_user.id))
    if not row:
        await reply(update, "💼 No wallet connected yet. Complete /start first.")
        return ConversationHandler.END
    context.user_data["import_started"] = time.monotonic()
    await reply(update, IMPORT_WARNING, import_go_keyboard())
    return WAIT_ACK


async def import_ack(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await ensure_subscribed(update, context):
        return ConversationHandler.END
    await reply(
        update,
        "🔐 <b>WALLET IMPORT</b>\n\nSend the private key in your next message.\n\n"
        + info("The bot deletes it immediately · if Telegram refuses, delete it yourself at once · /cancel aborts."),
    )
    return WAIT_KEY


async def import_key(update, context: ContextTypes.DEFAULT_TYPE):
    started = context.user_data.get("import_started") or 0
    raw = update.message.text or ""
    try:
        await update.message.delete()
    except Exception:
        await reply(update, "🚨 Telegram refused deletion. Remove that message from the chat immediately.")
    if time.monotonic() - started > IMPORT_TTL:
        _clear_import(context)
        await reply(update, "⌛ Import session expired. No changes were made.")
        return ConversationHandler.END
    try:
        keypair = parse_keypair(raw)
    except ValueError:
        _clear_import(context)
        await reply(update, "❌ Not a valid private key. No changes were made.")
        return ConversationHandler.END
    finally:
        raw = ""
    context.user_data["import_secret"] = bytes(keypair)
    context.user_data["import_pub"] = str(keypair.pubkey())
    del keypair
    await reply(
        update,
        "\n".join(
            [
                "🔐 <b>CONFIRM REPLACEMENT</b>",
                "",
                "New address",
                esc(context.user_data["import_pub"]),
                "",
                info("Confirming replaces the currently connected wallet."),
            ]
        ),
        import_confirm_keyboard(),
    )
    return WAIT_CONFIRM


async def import_confirm(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    secret = context.user_data.get("import_secret")
    pubkey = context.user_data.get("import_pub")
    if data == "import_no" or not secret or not pubkey:
        _clear_import(context)
        await reply(update, "❌ Import cancelled. Existing wallet unchanged.")
        return ConversationHandler.END
    if not await ensure_subscribed(update, context):
        _clear_import(context)
        return ConversationHandler.END
    svc = services(context)
    payload = svc.cipher.encrypt_json(secret)
    await svc.db.set_wallet(str(update.effective_user.id), pubkey, payload)
    _clear_import(context)
    logger.info("wallet imported user=%s", update.effective_user.id)
    await reply(
        update,
        f"✅ <b>WALLET REPLACED</b>\n\nConnected: {esc(pubkey)}",
    )
    return ConversationHandler.END


async def import_cancel(update, context: ContextTypes.DEFAULT_TYPE):
    _clear_import(context)
    await reply(update, "❌ Import cancelled. Existing wallet unchanged.")
    return ConversationHandler.END


def _clear_import(context) -> None:
    context.user_data.pop("import_secret", None)
    context.user_data.pop("import_pub", None)
    context.user_data.pop("import_started", None)
