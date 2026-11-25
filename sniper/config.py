from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from sniper.crypto import decode_master_key
from sniper.errors import ConfigError
from sniper.paths import ENV_PATH, LOG_DIR


def _bool(value: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Base58 alphabet (bitcoin/Solana), used to sanity-check the fee referral account.
_BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _validate_fee_settings(account: str, bps: int) -> None:
    """JUP_REFERRAL_ACCOUNT + BOT_FEE_BPS consistency, checked once per boot.

    An empty account (or bps 0) simply means the bot fee is disabled."""
    if not account or bps <= 0:
        return
    # Jupiter /order accepts referral fees of 50-255 bps only.
    if not 50 <= bps <= 255:
        raise ConfigError("BOT_FEE_BPS must be 50-255 (Jupiter referral fee range) or 0 to disable the bot fee.")
    if len(account) < 32 or len(account) > 44 or not all(char in _BASE58 for char in account):
        raise ConfigError("JUP_REFERRAL_ACCOUNT does not look like a Solana address.")


def _int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value < minimum or value > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass
class Config:
    telegram_token: str = ""
    turso_url: str = ""
    turso_token: str = ""
    turso_db_name: str = ""
    network: str = "mainnet-beta"
    rpc_urls: list[str] = field(default_factory=list)
    jupiter_base: str = "https://api.jup.ag/swap/v2"
    jupiter_api_key: str = ""
    jupiter_referral_account: str = ""
    bot_fee_bps: int = 100
    jupiter_price_url: str = "https://lite-api.jup.ag/price/v3"
    birdeye_api_key: str = ""
    rugcheck_base: str = "https://api.rugcheck.xyz"
    helius_api_key: str = ""
    master_key: bytes = b""
    channel_1: str = ""
    channel_2: str = ""
    channel_1_link: str = ""
    channel_2_link: str = ""
    admin_ids: set[int] = field(default_factory=set)
    subscription_ttl: int = 60
    max_concurrent_swaps: int = 3
    trade_queue_max: int = 200
    max_trades_per_hour: int = 20
    trades_keep_per_user: int = 100
    commands_per_30s: int = 12
    high_value_sol: float = 1.0
    watchlist_poll_seconds: float = 3.0
    trigger_poll_seconds: float = 15.0
    automate_poll_seconds: float = 12.0
    max_watch_per_user: int = 10
    log_level: str = "INFO"
    autosnipe_require_rugcheck: bool = True
    autosnipe_require_mint_revoked: bool = False
    absolute_max_sol: float = 100.0
    log_dir: Path = LOG_DIR
    default_slippage_bps: int = 500
    default_priority_fee: int = 100_000
    default_max_sol: float = 0.1
    default_daily_sol: float = 0.5
    default_snipe_sol: float = 0.05

    @property
    def is_mainnet(self) -> bool:
        return self.network == "mainnet-beta"

    @property
    def fee_active(self) -> bool:
        """True when the sell-side bot fee is configured and switched on."""
        return bool(self.jupiter_referral_account) and self.bot_fee_bps > 0

    def channels(self) -> list[str]:
        return [self.channel_1, self.channel_2]

    def channel_link(self, channel: str) -> str:
        if channel == self.channel_1 and self.channel_1_link:
            return self.channel_1_link
        if channel == self.channel_2 and self.channel_2_link:
            return self.channel_2_link
        text = channel.strip()
        if text.startswith("https://") or text.startswith("http://"):
            return text
        if text.startswith("@"):
            return f"https://t.me/{text[1:]}"
        if text.startswith("-100") and text[4:].isdigit():
            return f"https://t.me/c/{text[4:]}"
        return f"https://t.me/{text}"

    def secret_values(self) -> list[str]:
        values = [
            self.telegram_token,
            self.turso_token,
            self.jupiter_api_key,
            self.birdeye_api_key,
            self.helius_api_key,
        ]
        if self.master_key:
            import base64

            values.append(base64.urlsafe_b64encode(self.master_key).decode().rstrip("="))
        return [item for item in values if item]

    @classmethod
    def from_env(cls, profile: str = "bot") -> "Config":
        load_dotenv(ENV_PATH, override=False)
        missing: list[str] = []

        def need(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                missing.append(name)
            return value

        cfg = cls()
        cfg.log_level = os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO"
        cfg.turso_db_name = os.environ.get("TURSO_DB_NAME", "").strip()
        cfg.network = os.environ.get("SOLANA_NETWORK", "mainnet-beta").strip() or "mainnet-beta"
        if cfg.network not in {"mainnet-beta", "devnet", "testnet"}:
            raise ConfigError("SOLANA_NETWORK must be mainnet-beta, devnet, or testnet")

        if profile in {"bot", "migrate", "decrypt", "export"}:
            cfg.turso_url = need("TURSO_DATABASE_URL")
            cfg.turso_token = need("TURSO_AUTH_TOKEN")
        if profile in {"bot", "decrypt"}:
            raw_master = need("MASTER_ENCRYPTION_KEY")
            if raw_master:
                cfg.master_key = decode_master_key(raw_master)
        if profile == "bot":
            cfg.telegram_token = need("TELEGRAM_BOT_TOKEN")
            cfg.channel_1 = need("REQUIRED_CHANNEL_1")
            cfg.channel_2 = need("REQUIRED_CHANNEL_2")
            cfg.channel_1_link = os.environ.get("REQUIRED_CHANNEL_1_LINK", "").strip()
            cfg.channel_2_link = os.environ.get("REQUIRED_CHANNEL_2_LINK", "").strip()
            helius = os.environ.get("HELIUS_API_KEY", "").strip()
            cfg.helius_api_key = helius
            raw_urls = need("SOLANA_RPC_URLS")
            urls = []
            for part in raw_urls.split(","):
                url = part.strip().replace("{HELIUS_API_KEY}", helius)
                if url:
                    urls.append(url)
            if not urls:
                missing.append("SOLANA_RPC_URLS")
            cfg.rpc_urls = urls
            cfg.jupiter_base = os.environ.get("JUPITER_API_BASE", cfg.jupiter_base).strip() or cfg.jupiter_base
            cfg.jupiter_api_key = os.environ.get("JUPITER_API_KEY", "").strip()
            cfg.jupiter_price_url = os.environ.get("JUPITER_PRICE_URL", cfg.jupiter_price_url).strip() or cfg.jupiter_price_url
            cfg.jupiter_referral_account = os.environ.get("JUP_REFERRAL_ACCOUNT", "").strip()
            raw_fee_bps = os.environ.get("BOT_FEE_BPS", "").strip()
            if raw_fee_bps:
                try:
                    cfg.bot_fee_bps = int(raw_fee_bps)
                except ValueError as exc:
                    raise ConfigError("BOT_FEE_BPS must be an integer in basis points (100 = 1%).") from exc
            _validate_fee_settings(cfg.jupiter_referral_account, cfg.bot_fee_bps)
            cfg.birdeye_api_key = os.environ.get("BIRDEYE_API_KEY", "").strip()
            cfg.rugcheck_base = os.environ.get("RUGCHECK_API_BASE", cfg.rugcheck_base).strip() or cfg.rugcheck_base
            admins = os.environ.get("ADMIN_TELEGRAM_IDS", "")
            parsed: set[int] = set()
            for piece in admins.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                try:
                    parsed.add(int(piece))
                except ValueError as exc:
                    raise ConfigError(f"ADMIN_TELEGRAM_IDS contains a non-integer: {piece}") from exc
            cfg.admin_ids = parsed
            cfg.subscription_ttl = _int("SUBSCRIPTION_CACHE_SECONDS", 60, minimum=5, maximum=3600)
            cfg.max_concurrent_swaps = _int("MAX_CONCURRENT_SWAPS", 3, minimum=1, maximum=16)
            cfg.trade_queue_max = _int("TRADE_QUEUE_MAX", 200, minimum=10, maximum=5000)
            cfg.max_trades_per_hour = _int("MAX_TRADES_PER_USER_PER_HOUR", 20, minimum=1, maximum=500)
            cfg.trades_keep_per_user = _int("TRADES_KEEP_PER_USER", 100, minimum=10, maximum=10000)
            cfg.commands_per_30s = _int("COMMANDS_PER_30S", 12, minimum=1, maximum=100)
            cfg.high_value_sol = _float("HIGH_VALUE_SOL", 1.0, minimum=0.01, maximum=100000)
            cfg.watchlist_poll_seconds = _float("WATCHLIST_POLL_SECONDS", 3.0, minimum=1, maximum=300)
            cfg.trigger_poll_seconds = _float("TRIGGER_POLL_SECONDS", 15.0, minimum=5, maximum=300)
            cfg.automate_poll_seconds = _float("AUTOMATE_POLL_SECONDS", 12.0, minimum=5, maximum=120)
            cfg.max_watch_per_user = _int("MAX_WATCH_PER_USER", 10, minimum=1, maximum=100)
            cfg.absolute_max_sol = _float("ABSOLUTE_MAX_SOL", 100.0, minimum=0.01, maximum=100000)
            cfg.autosnipe_require_rugcheck = _bool(os.environ.get("AUTOSNIPE_REQUIRE_RUGCHECK", "true"), True)
            cfg.autosnipe_require_mint_revoked = _bool(os.environ.get("AUTOSNIPE_REQUIRE_MINT_REVOKED", "false"), False)
            cfg.default_slippage_bps = _int("DEFAULT_SLIPPAGE_BPS", 500, minimum=10, maximum=5000)
            cfg.default_priority_fee = _int("DEFAULT_PRIORITY_FEE_LAMPORTS", 100_000, minimum=0, maximum=20_000_000)
            cfg.default_max_sol = _float("DEFAULT_MAX_SOL_PER_TRADE", 0.1, minimum=0.000001, maximum=cfg.absolute_max_sol)
            cfg.default_daily_sol = _float("DEFAULT_DAILY_SPEND_LIMIT_SOL", 0.5, minimum=0.000001, maximum=cfg.absolute_max_sol)
            cfg.default_snipe_sol = _float("DEFAULT_AUTOSNIPE_AMOUNT_SOL", 0.05, minimum=0.000001, maximum=cfg.absolute_max_sol)

        if missing:
            unique = []
            for item in missing:
                if item not in unique:
                    unique.append(item)
            raise ConfigError("Missing required environment variables: " + ", ".join(unique))
        return cfg
