"""Regression test: a message carrying a token address must route to
token detection. In production a missing `services` import in app.py
raised NameError on this exact path before the token panel could appear
(the global error handler turned it into a neutral reply instead)."""

from __future__ import annotations

import pytest

import sniper.app as app_module

# BONK mint — a 44-character base58 key that is not secret-shaped.
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6dxjnGY4eB6d4WmP"


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.sent: list[str] = []

    async def reply_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.sent.append(text)


class FakeUpdate:
    def __init__(self, text):
        self.message = FakeMessage(text)
        self.effective_message = self.message


class FakeApplication:
    def __init__(self):
        self.bot_data = {"svc": object()}


class FakeContext:
    def __init__(self):
        self.application = FakeApplication()
        self.user_data = {}


@pytest.mark.asyncio
async def test_bare_mint_is_routed_to_detect(monkeypatch):
    seen: dict = {}

    async def fake_detect(update, context, svc, text):
        seen["svc"] = svc
        seen["text"] = text
        return True

    monkeypatch.setattr(app_module, "detect_from_text", fake_detect)
    update = FakeUpdate(MINT)
    context = FakeContext()
    await app_module._handle_text(update, context)
    assert seen["text"] == MINT
    assert seen["svc"] is context.application.bot_data["svc"]
    assert update.effective_message.sent == []


@pytest.mark.asyncio
async def test_mint_inside_a_message_is_routed_to_detect(monkeypatch):
    seen: dict = {}

    async def fake_detect(update, context, svc, text):
        seen["text"] = text
        return True

    monkeypatch.setattr(app_module, "detect_from_text", fake_detect)
    update = FakeUpdate(f"check this one 🚀 {MINT}")
    context = FakeContext()
    await app_module._handle_text(update, context)
    assert MINT in seen["text"]
    assert update.effective_message.sent == []


@pytest.mark.asyncio
async def test_plain_text_gets_fallback_reply(monkeypatch):
    calls: list[str] = []

    async def fake_detect(update, context, svc, text):
        # The router always hands text to detection; detection itself
        # ignores messages without a mint-shaped address (see
        # test_ca_detect.test_no_mint_shape_returns_false).
        calls.append(text)
        return False

    monkeypatch.setattr(app_module, "detect_from_text", fake_detect)
    update = FakeUpdate("hello there, when moon?")
    context = FakeContext()
    await app_module._handle_text(update, context)
    assert calls == ["hello there, when moon?"]
    assert len(update.effective_message.sent) == 1
