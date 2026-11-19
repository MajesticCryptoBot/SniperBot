from __future__ import annotations

import asyncio
import logging
import warnings

import httpx
from telegram import Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters
from telegram.warnings import PTBUserWarning

from sniper.config import Config
from sniper.crypto import EnvelopeCipher
from sniper.db import Database
from sniper.handlers import flows
from sniper.handlers.admin import admin_fees, admin_gate, admin_pause, admin_resume, admin_stats, admin_user
from sniper.handlers.common import looks_like_secret, reply, services
from sniper.handlers.portfolio import orders, summary
from sniper.handlers.settings import autosnipe, config_command, on_config_callback, on_snipe_callback, resume, stop
from sniper.handlers.start import help_command, on_check_sub, on_help_callback, start
from sniper.handlers.token_panel import detect_from_text, on_token_callback
from sniper.handlers.trade_panel import (
    buy_entry,
    on_trade_panel_callback,
    on_trigger_callback,
    sell_entry,
    triggers_command,
)
from sniper.handlers.trading import on_trade_callback, scan, withdraw
from sniper.handlers.wallet import (
    WAIT_ACK,
    WAIT_CONFIRM,
    WAIT_KEY,
    import_ack,
    import_cancel,
    import_confirm,
    import_key,
    import_start,
    on_wallet_callback,
    wallet,
)
from sniper.jupiter import JupiterClient
from sniper.logging_setup import setup_logging
from sniper.prices import PriceService
from sniper.rate_limit import RateLimiter
from sniper.redact import set_secrets
from sniper.safety import SafetyService
from sniper.settings_logic import SETTING_NAMES
from sniper.solana_service import SolanaService
from sniper.subscription import SubscriptionGate
from sniper.trade_queue import TradeQueue
from sniper.positions_monitor import PositionMonitor
from sniper.trigger_monitor import TriggerMonitor
from sniper.watchlist_monitor import WatchlistMonitor

logger = logging.getLogger(__name__)

# Dropped long-poll sockets and brief api.telegram.org blips. PTB's polling
# loop already retries these; they are not handler bugs.
_TRANSIENT_TELEGRAM = (NetworkError, TimedOut, RetryAfter)

# The wallet-import ConversationHandler tracks state in user_data, not per
# message, so the per_message=False + CallbackQueryHandler combination is
# intentional. PTB prints a FAQ note about it on every start; silence it.
warnings.filterwarnings("ignore", category=PTBUserWarning, message=".*per_message.*")


class Services:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.cipher = EnvelopeCipher(cfg.master_key)
        self.http: httpx.AsyncClient | None = None
        self.rpc = SolanaService(cfg.rpc_urls)
        self.jupiter: JupiterClient | None = None
        self.prices: PriceService | None = None
        self.safety: SafetyService | None = None
        self.metrics: dict = {}
        self.queue = TradeQueue(cfg, db, self.cipher, self.rpc, None, None, self.metrics)
        self.monitor: WatchlistMonitor | None = None
        self.trigger_monitor: TriggerMonitor | None = None
        self.positions_monitor: PositionMonitor | None = None
        self.gate = SubscriptionGate(cfg.channels(), ttl_seconds=cfg.subscription_ttl)
        self.limiter = RateLimiter()

    async def start(self) -> None:
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        self.rpc.http = self.http
        self.rpc._owns_http = False
        await self.rpc.start()
        self.jupiter = JupiterClient(self.cfg.jupiter_base, self.cfg.jupiter_api_key, self.http)
        self.prices = PriceService(self.http, self.cfg.jupiter_price_url, self.cfg.jupiter_api_key, self.cfg.birdeye_api_key)
        self.safety = SafetyService(
            self.http,
            self.rpc,
            self.prices,
            self.cfg.rugcheck_base,
            self.cfg.autosnipe_require_rugcheck,
            self.cfg.autosnipe_require_mint_revoked,
        )
        self.queue.jupiter = self.jupiter
        self.queue.prices = self.prices
        self.monitor = WatchlistMonitor(self.cfg, self.db, self.safety, self.queue, self.prices, self.metrics)
        self.trigger_monitor = TriggerMonitor(self.cfg, self.db, self.queue, self.prices, self.metrics)
        self.positions_monitor = PositionMonitor(self.cfg, self.db, self.queue, self.prices, self.rpc, self.metrics)

    async def aclose(self) -> None:
        await self.rpc.aclose()
        if self.http is not None:
            await self.http.aclose()
            self.http = None


def build_application(cfg: Config, db: Database) -> Application:
    services = Services(cfg, db)
    application = (
        Application.builder()
        .token(cfg.telegram_token)
        # Defaults are 5s read/write and 1s pool. A getUpdates long-poll adds
        # its own timeout on top of read_timeout, but a slow connect or a
        # saturated pool still fails at those defaults and shows up as
        # httpx.ReadError / TimedOut. Give both the send client and the
        # polling client room to ride out a blip.
        .connect_timeout(15.0)
        .read_timeout(20.0)
        .write_timeout(20.0)
        .pool_timeout(5.0)
        .get_updates_connect_timeout(15.0)
        .get_updates_read_timeout(20.0)
        .get_updates_write_timeout(20.0)
        .get_updates_pool_timeout(5.0)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["svc"] = services
    _register(application)
    return application


def _register(application: Application) -> None:
    private = filters.ChatType.PRIVATE
    application.add_error_handler(_on_error)
    application.add_handler(CommandHandler("start", start, filters=private))
    application.add_handler(CommandHandler("help", help_command, filters=private))
    application.add_handler(CommandHandler("wallet", wallet, filters=private))
    application.add_handler(CommandHandler("wallets", wallet, filters=private))
    application.add_handler(CommandHandler("summary", summary, filters=private))
    application.add_handler(CommandHandler("orders", orders, filters=private))
    application.add_handler(CommandHandler("buy", buy_entry, filters=private))
    application.add_handler(CommandHandler("sell", sell_entry, filters=private))
    application.add_handler(CommandHandler("withdraw", withdraw, filters=private))
    application.add_handler(CommandHandler("scan", scan, filters=private))
    application.add_handler(CommandHandler("triggers", triggers_command, filters=private))
    application.add_handler(CommandHandler("config", config_command, filters=private))
    application.add_handler(CommandHandler("autosnipe", autosnipe, filters=private))
    application.add_handler(CommandHandler("stop", stop, filters=private))
    application.add_handler(CommandHandler("resume", resume, filters=private))
    application.add_handler(CommandHandler("admin_stats", admin_stats, filters=private))
    application.add_handler(CommandHandler("admin_pause", admin_pause, filters=private))
    application.add_handler(CommandHandler("admin_resume", admin_resume, filters=private))
    application.add_handler(CommandHandler("admin_user", admin_user, filters=private))
    application.add_handler(CommandHandler("admin_gate", admin_gate, filters=private))
    application.add_handler(CommandHandler("admin_fees", admin_fees, filters=private))
    application.add_handler(CallbackQueryHandler(on_check_sub, pattern=r"^check_sub$"))
    application.add_handler(CallbackQueryHandler(on_config_callback, pattern=r"^cfg:"))
    application.add_handler(CallbackQueryHandler(on_help_callback, pattern=r"^help:"))
    application.add_handler(CallbackQueryHandler(on_token_callback, pattern=r"^tok:"))
    application.add_handler(CallbackQueryHandler(on_wallet_callback, pattern=r"^wal:"))
    application.add_handler(CallbackQueryHandler(on_snipe_callback, pattern=r"^snipe:"))
    application.add_handler(CallbackQueryHandler(on_trade_panel_callback, pattern=r"^tp:"))
    application.add_handler(CallbackQueryHandler(on_trigger_callback, pattern=r"^tt:"))
    application.add_handler(CallbackQueryHandler(on_trade_callback, pattern=r"^(ok|no):"))
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("import", import_start, filters=private)],
            states={
                WAIT_ACK: [CallbackQueryHandler(import_ack, pattern=r"^import_go$")],
                WAIT_KEY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, import_key),
                    CommandHandler("cancel", import_cancel),
                ],
                WAIT_CONFIRM: [CallbackQueryHandler(import_confirm, pattern=r"^import_(yes|no)$")],
            },
            fallbacks=[CommandHandler("cancel", import_cancel)],
            per_chat=True,
            per_user=True,
            per_message=False,
            name="import_wallet",
        )
    )
    application.add_handler(CommandHandler("cancel", _cancel, filters=private))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, _handle_text))
    # Photo/document captions go through the same routing: an active flow
    # step or a token address inside the caption is handled like text.
    application.add_handler(MessageHandler(filters.CAPTION & ~filters.COMMAND & private, _handle_caption))


async def _on_error(update: object, context) -> None:
    """Last-resort safety net so an unhandled exception never disappears.

    Handlers already translate UserFacingError into a chat reply; this only
    catches anything that slips through. The user gets a neutral message,
    the detail goes to the log.

    NetworkError raised by getUpdates is delivered here too, on every dropped
    long-poll. The polling loop retries it; logging a traceback (and trying
    to tell the chat) just spams the log while the bot is still healthy.
    """
    err = context.error
    if isinstance(err, _TRANSIENT_TELEGRAM):
        logger.warning("telegram network blip (retried automatically): %s", err)
        return
    logger.error("unhandled update error", exc_info=err)
    message = None
    if isinstance(update, Update):
        message = update.effective_message
    if message is not None:
        try:
            await message.reply_text("⚠️ Unexpected error. Please try again shortly. Check /orders before retrying a trade.")
        except Exception:
            logger.warning("could not deliver the error notice to the chat")


async def _cancel(update: Update, context) -> None:
    from sniper.handlers.common import ensure_private

    if not await ensure_private(update):
        return
    flow = flows.cancel(context)
    if flow:
        await reply(update, "✅ Input flow cancelled.")
        return
    await reply(update, "No active input flow.")


async def _handle_text(update: Update, context) -> None:
    """Route free-text messages: secret protection, active flow, detected token."""
    text = update.message.text or ""
    if looks_like_secret(text):
        try:
            await update.message.delete()
        except Exception:
            await reply(update, "🚨 Telegram refused deletion. Remove that message immediately. The bot did not store it.")
            return
        await reply(update, "🛡️ Key-like message deleted and not stored. Use /import to replace the wallet intentionally.")
        return
    if await flows.handle(update, context, text):
        return
    if await detect_from_text(update, context, services(context), text):
        return
    if text.strip().lower() in SETTING_NAMES:
        # A bare setting name typed under the /config panel starts that flow.
        await flows.begin(update, context, "config", prefill={"name": text.strip().lower()})
        return
    await reply(update, "❓ <b>MESSAGE NOT RECOGNISED</b>\n\nSend /help for commands, or paste a token mint / supported link to open its panel.")


async def _handle_caption(update: Update, context) -> None:
    """Captions of photos and documents get the same treatment as text: an
    active flow step or a token address inside the caption is handled the
    same way. A caption with nothing tradable stays silent — no nagging."""
    text = update.message.caption or ""
    if looks_like_secret(text):
        await reply(update, "🛡️ Key-like caption not processed. Never share private keys in chat.")
        return
    if await flows.handle(update, context, text):
        return
    await detect_from_text(update, context, services(context), text)


async def _post_init(application: Application) -> None:
    svc: Services = application.bot_data["svc"]
    await svc.start()
    svc.queue.bot = application.bot
    tasks = [
        asyncio.create_task(svc.queue.worker(index), name=f"trade-worker-{index}")
        for index in range(svc.cfg.max_concurrent_swaps)
    ]
    tasks.append(asyncio.create_task(svc.queue.reconcile_loop(), name="reconcile"))
    tasks.append(asyncio.create_task(svc.monitor.run(), name="watchlist-monitor"))
    tasks.append(asyncio.create_task(svc.trigger_monitor.run(), name="trigger-monitor"))
    tasks.append(asyncio.create_task(svc.positions_monitor.run(), name="positions-monitor"))
    application.bot_data["tasks"] = tasks
    # Trades confirmed before a restart (crash, deploy, offline reboot) but
    # never executed are safe to resume: guards are re-checked on execution.
    resumed = await svc.queue.resume_pending()
    if resumed:
        logger.info("resumed %s trade(s) queued before the previous shutdown", resumed)
    me = await application.bot.get_me()
    logger.info("bot online username=%s id=%s network=%s", me.username, me.id, svc.cfg.network)
    if "yourchannel" in (svc.cfg.channel_1 + svc.cfg.channel_2).lower():
        logger.warning("REQUIRED_CHANNEL placeholders are still set. Membership checks will fail until .env is edited.")
    if not svc.cfg.jupiter_api_key:
        logger.warning("JUPITER_API_KEY is empty. Keyless Jupiter calls are heavily rate-limited.")
    if not svc.cfg.is_mainnet:
        logger.warning("SOLANA_NETWORK is %s. Jupiter swaps and watchlist autosnipe execution are disabled.", svc.cfg.network)


async def _post_shutdown(application: Application) -> None:
    tasks = application.bot_data.get("tasks") or []
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    svc = application.bot_data.get("svc")
    if svc:
        await svc.aclose()
    logger.info("shutdown complete")


def main() -> None:
    try:
        cfg = Config.from_env("bot")
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    set_secrets(cfg.secret_values())
    setup_logging(cfg.log_level)
    logger.info(
        "starting network=%s channels=%s,%s admins=%s concurrent_swaps=%s",  # noqa: operator-side log
        cfg.network,
        cfg.channel_1,
        cfg.channel_2,
        len(cfg.admin_ids),
        cfg.max_concurrent_swaps,
    )
    db = Database.connect(cfg)
    try:
        applied = db.apply_schema()
        logger.info("schema ensured statements=%s", applied)
        application = build_application(cfg, db)
        # Long polling. No public URL, webhook, tunnel, or container.
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False, close_loop=False)
    finally:
        db.close()
