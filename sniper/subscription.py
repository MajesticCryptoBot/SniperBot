"""Mandatory membership in two Telegram channels, cached briefly."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sniper.ui import esc

logger = logging.getLogger(__name__)

MEMBER_STATUSES = {"member", "administrator", "creator", "owner"}


def is_joined(status, is_member: bool | None) -> bool:
    norm = str(status).split(".")[-1].lower()
    if norm in MEMBER_STATUSES:
        return True
    if norm == "restricted" and bool(is_member):
        return True
    return False


async def telegram_membership(bot, channel: str, user_id: int) -> tuple[bool, str | None]:
    """Return (joined, error_detail). error_detail is operator-side detail for the log, never shown to users."""
    from telegram.error import BadRequest, Forbidden, TelegramError

    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
    except BadRequest as exc:
        low = str(exc).lower()
        if "user not found" in low or "member not found" in low or "participant" in low:
            return False, None
        return False, f"{channel}: {exc}"
    except Forbidden as exc:
        return False, f"{channel}: {exc}"
    except TelegramError as exc:
        return False, f"{channel}: Telegram API error while checking membership: {exc}"
    status = getattr(member, "status", "")
    return is_joined(status, getattr(member, "is_member", None)), None


@dataclass
class GateResult:
    ok: bool
    user_message: str
    admin_error: bool
    missing: list[str]
    error_detail: str = ""


class SubscriptionGate:
    def __init__(self, channels: list[str], ttl_seconds: int = 60, fetch=None, clock=None):
        if len(channels) != 2 or not all(channels):
            raise ValueError("exactly two required channels must be configured")
        self.channels = channels
        self.ttl = ttl_seconds
        self._fetch = fetch or telegram_membership
        self._clock = clock or time.monotonic
        self._cache: dict[int, tuple[float, GateResult]] = {}

    def cache_size(self) -> int:
        return len(self._cache)

    async def check(self, bot, user_id: int, *, force: bool = False) -> GateResult:
        now = self._clock()
        if not force:
            cached = self._cache.get(user_id)
            if cached and now - cached[0] < self.ttl:
                return cached[1]
        result = await self._check_live(bot, user_id)
        if result.admin_error and result.error_detail:
            logger.warning("membership check failed for user=%s: %s", user_id, result.error_detail)
        if not result.admin_error:
            self._cache[user_id] = (now, result)
        return result

    async def _check_live(self, bot, user_id: int) -> GateResult:
        missing: list[str] = []
        errors: list[str] = []
        for channel in self.channels:
            joined, error = await self._fetch(bot, channel, user_id)
            if error:
                errors.append(error)
            elif not joined:
                missing.append(channel)
        if errors:
            return GateResult(
                False,
                "Membership could not be verified right now. Tap “✅ I've joined · Verify access” in a moment.",
                True,
                missing,
                error_detail="\n".join(errors),
            )
        if missing:
            lines = [
                "<b>ACCESS REQUIRED</b>",
                "",
                "Join both channels, then tap “✅ I've joined · Verify access”.",
                "",
            ]
            for channel in self.channels:
                state = "Not joined" if channel in missing else "Joined"
                lines.append(f"{state} · {esc(channel)}")
            return GateResult(False, "\n".join(lines), False, missing)
        return GateResult(True, "", False, [])
