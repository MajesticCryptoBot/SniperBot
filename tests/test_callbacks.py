"""Regression test: inline-button handlers must turn UserFacingError into a
chat reply instead of an unhandled framework exception (seen in production:
tapping the autosnipe toggle while trading was paused logged a crash and the
user got no message)."""

from __future__ import annotations

import pytest

from sniper.errors import UserFacingError
from sniper.handlers.common import callback_guard


class FakeMessage:
    def __init__(self):
        self.sent: list[str] = []

    async def reply_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.sent.append(text)


class FakeUpdate:
    def __init__(self):
        self.effective_message = FakeMessage()


class FakeContext:
    pass


@pytest.mark.asyncio
async def test_callback_guard_replies_user_facing_error():
    @callback_guard
    async def handler(update, context):
        raise UserFacingError("Trading is paused for this account. Use /resume before submitting new requests.")

    update = FakeUpdate()
    await handler(update, FakeContext())
    assert update.effective_message.sent == ["Trading is paused for this account. Use /resume before submitting new requests."]


@pytest.mark.asyncio
async def test_callback_guard_replies_neutrally_on_unexpected_error():
    @callback_guard
    async def handler(update, context):
        raise RuntimeError("boom")

    update = FakeUpdate()
    await handler(update, FakeContext())
    assert update.effective_message.sent == ["⚠️ Unexpected error. Please try again shortly. Check /orders before retrying a trade."]


@pytest.mark.asyncio
async def test_callback_guard_passes_through_success():
    marker: list = []

    @callback_guard
    async def handler(update, context):
        marker.append("ran")

    update = FakeUpdate()
    await handler(update, FakeContext())
    assert marker == ["ran"]
    assert update.effective_message.sent == []
