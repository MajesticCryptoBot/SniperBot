from __future__ import annotations

import asyncio
import logging
import re
from functools import wraps

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from sniper.errors import UserFacingError
from sniper.keyboards import join_keyboard

logger = logging.getLogger(__name__)

UNEXPECTED = "<b>UNEXPECTED ERROR</b>\nTry again shortly. Check /orders before retrying a trade."

# Telegram API calls occasionally cannot open a connection at all
# (httpx.ConnectTimeout → telegram.error.TimedOut) while the long-poll
# connection keeps working, so the bot looks online but every fresh API
# call dies. A short retry turns a network blip into a recoverable hiccup.
TG_ATTEMPTS = 3
TG_RETRY_DELAY = 1.0


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (BadRequest, RetryAfter)):
        # Deterministic rejections and flood control never benefit from retrying.
        return False
    return isinstance(exc, (TimedOut, NetworkError))


async def tg_call(call, *args, **kwargs):
    """Run a Telegram API call, retrying network-level failures."""
    for attempt in range(TG_ATTEMPTS):
        try:
            return await call(*args, **kwargs)
        except Exception as exc:
            if attempt + 1 >= TG_ATTEMPTS or not _is_retryable(exc):
                raise
            delay = TG_RETRY_DELAY * (attempt + 1)
            logger.warning("telegram call failed (%s) — retrying in %.0fs", exc, delay)
            await asyncio.sleep(delay)


async def best_effort_answer(query, text: str) -> None:
    """A callback spinner toast is cosmetic — a network blip on it must not
    kill the action the button stands for."""
    try:
        await tg_call(query.answer, text)
    except Exception as exc:
        logger.warning("could not answer a callback: %s", exc)

# A Solana public key (mint or wallet) is 43-44 base58 characters.
# Transaction signatures are 64+, private keys 87+, so they never match.
# Token detection in free text lives in sniper/ca_detect.py.


def services(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["svc"]


async def reply(update, text: str, markup=None) -> None:
    message = update.effective_message
    if message is None or not text:
        return
    await tg_call(message.reply_text, text[:3900], reply_markup=markup, disable_web_page_preview=True, parse_mode="HTML")


async def _safe_reply(update, text: str) -> None:
    """Fallback notices run inside an except branch — they must never raise."""
    try:
        await reply(update, text)
    except Exception:
        logger.warning("could not deliver fallback notice to the chat")


async def ensure_private(update) -> bool:
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        if update.effective_message:
            await reply(update, "<b>PRIVATE CHAT</b>\nOpen this bot in a direct message.")
        return False
    return True


async def ensure_subscribed(update, context, *, force: bool = False) -> bool:
    user = update.effective_user
    if user is None:
        return False
    svc = services(context)
    result = await tg_call(svc.gate.check, context.bot, user.id, force=force)
    if result.ok:
        return True
    await reply(update, result.user_message, join_keyboard(svc.cfg))
    return False


def command(name: str, *, gated: bool = True, needs_wallet: bool = False):
    def decorator(func):
        @wraps(func)
        async def wrapper(update, context: ContextTypes.DEFAULT_TYPE):
            try:
                if not await ensure_private(update):
                    return
                user = update.effective_user
                svc = services(context)
                if user and not svc.limiter.allow(f"cmd:{user.id}", svc.cfg.commands_per_30s, 30):
                    await reply(update, "<b>PLEASE WAIT</b>\nToo many requests. Try again in a moment.")
                    return
                if gated and not await ensure_subscribed(update, context):
                    return
                if needs_wallet:
                    row = await svc.db.get_user(str(user.id))
                    if not row or not row.get("wallet_address"):
                        await reply(update, "<b>NO WALLET</b>\nComplete /start to create one.")
                        return
                await func(update, context)
            except UserFacingError as exc:
                await _safe_reply(update, str(exc))
            except Exception:
                logger.exception("command %s failed", name)
                await _safe_reply(update, UNEXPECTED)

        return wrapper

    return decorator


def callback_guard(func):
    """Catch handler errors for inline-button callbacks the way `command` does.

    Without this, a UserFacingError raised by a button handler (for example
    "Your trading is stopped. Use /resume…") reaches the framework, is logged
    as an unhandled exception, and the user gets no reply.
    """

    @wraps(func)
    async def wrapper(update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await func(update, context)
        except UserFacingError as exc:
            await _safe_reply(update, str(exc))
        except Exception:
            logger.exception("callback handler %s failed", func.__name__)
            await _safe_reply(update, UNEXPECTED)

    return wrapper


def looks_like_secret(text: str) -> bool:
    stripped = (text or "").strip()
    if stripped.startswith("[") and stripped.endswith("]") and len(stripped) > 80:
        return True
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{60,}", stripped))
