"""Tests for Telegram network-error resilience (the 2026-11-15 dead-button incident).

The incident: a fresh HTTPS connection to api.telegram.org timed out on the
"I subscribed" button click, killing the whole check-sub flow with zero
feedback. The bot must retry network-level failures and never let a cosmetic
call (the spinner toast) abort the actual membership check.
"""

from __future__ import annotations

import pytest

from sniper.handlers import common
from sniper.handlers.common import reply
from sniper.handlers.start import on_check_sub
from sniper.subscription import GateResult
from telegram.error import BadRequest, NetworkError, TimedOut


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(common, "TG_RETRY_DELAY", 0)


class FlakyMessage:
    def __init__(self, failures=0, error=TimedOut):
        self.failures = failures
        self.error = error
        self.sent: list[str] = []

    async def reply_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.sent.append(text)
        if self.failures > 0:
            self.failures -= 1
            raise self.error("network down")


class FlakyCallbackQuery:
    def __init__(self, failures=99):
        self.failures = failures
        self.data = "check_sub"
        self.answered: list[str | None] = []

    async def answer(self, text=None):
        self.answered.append(text)
        if self.failures > 0:
            self.failures -= 1
            raise TimedOut("network down")


def make_update(message=None, query=None, user_id=99):
    user = type("U", (), {"id": user_id, "username": "mom"})()
    chat = type("C", (), {"id": user_id, "type": "private"})()
    update = type(
        "Upd",
        (),
        {
            "effective_user": user,
            "effective_chat": chat,
            "effective_message": message or FlakyMessage(),
            "callback_query": query,
        },
    )()
    return update


class FakeLimiter:
    def __init__(self, allow=True):
        self.allow_result = allow

    def allow(self, *args, **kwargs):
        return self.allow_result


class FakeGate:
    async def check(self, bot, user_id, *, force=False):
        return GateResult(True, "", False, [])


class FakeDb:
    async def get_user(self, telegram_id):
        return {"wallet_address": "W", "trading_paused": 0}

    async def touch_user(self, telegram_id, username, chat_id):
        pass


class FakeSvc:
    def __init__(self, allow=True):
        self.limiter = FakeLimiter(allow)
        self.gate = FakeGate()
        self.db = FakeDb()
        self.cfg = type("Cfg", (), {"commands_per_30s": 12})()


class FakeApp:
    def __init__(self, svc):
        self.bot_data = {"svc": svc}


class FakeContext:
    def __init__(self, svc):
        self.user_data = {}
        self.application = FakeApp(svc)
        self.bot = None


async def test_reply_retries_on_timed_out():
    message = FlakyMessage(failures=1)
    await reply(make_update(message), "hello")
    assert message.sent.count("hello") == 2  # first attempt failed, retry delivered


async def test_reply_gives_up_after_three_attempts():
    message = FlakyMessage(failures=99)
    with pytest.raises(TimedOut):
        await reply(make_update(message), "hello")
    assert len(message.sent) == common.TG_ATTEMPTS


async def test_reply_does_not_retry_bad_request():
    message = FlakyMessage(failures=99, error=BadRequest)
    with pytest.raises(BadRequest):
        await reply(make_update(message), "hello")
    assert len(message.sent) == 1  # deterministic error — no wasted retries


async def test_check_sub_survives_callback_answer_failure():
    """The exact incident: query.answer times out, the check must still run."""
    query = FlakyCallbackQuery(failures=99)
    message = FlakyMessage()
    update = make_update(message=message, query=query)
    await on_check_sub(update, FakeContext(FakeSvc()))
    assert len(query.answered) == common.TG_ATTEMPTS  # toast retried, then gave up
    assert any("ACCESS VERIFIED" in text for text in message.sent)


async def test_polling_read_error_is_not_logged_as_a_crash(caplog):
    """getUpdates ReadError reaches the error handler; it must not look fatal."""
    import logging

    from sniper.app import _on_error

    class Ctx:
        error = NetworkError("httpx.ReadError: ")

    with caplog.at_level(logging.WARNING, logger="sniper.app"):
        await _on_error(None, Ctx())
    assert any("telegram network blip" in record.message for record in caplog.records)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


async def test_real_handler_error_is_still_logged(caplog):
    import logging

    from sniper.app import _on_error

    class Ctx:
        error = RuntimeError("boom")

    class Message:
        async def reply_text(self, text):
            return None

    update = type("Upd", (), {"effective_message": Message()})()
    with caplog.at_level(logging.ERROR, logger="sniper.app"):
        await _on_error(update, Ctx())
    assert any(record.levelno >= logging.ERROR and "unhandled update error" in record.message for record in caplog.records)


async def test_check_sub_rate_limited_answers_best_effort():
    query = FlakyCallbackQuery(failures=1)  # blip on the rate-limit toast too
    update = make_update(query=query)
    await on_check_sub(update, FakeContext(FakeSvc(allow=False)))
    # first attempt failed, the tg_call retry delivered the toast
    assert query.answered == ["⏳ Please wait a moment before checking again."] * 2
