"""Tests for the merged /config panel and its tap-to-change buttons."""

from __future__ import annotations

import time

from sniper.handlers import flows
from sniper.handlers.settings import config_command, on_config_callback
from sniper.keyboards import config_keyboard
from sniper.settings_logic import SETTING_NAMES
from sniper.subscription import GateResult


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.sent: list[str] = []
        self.markups: list = []

    async def reply_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.sent.append(text)
        self.markups.append(reply_markup)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answered: list = []

    async def answer(self, text=None):
        self.answered.append(text)


def make_update(message=None, query=None):
    user = type("U", (), {"id": 7, "username": "tester"})()
    chat = type("C", (), {"id": 7, "type": "private"})()
    return type(
        "Upd",
        (),
        {
            "effective_user": user,
            "effective_chat": chat,
            "effective_message": message or FakeMessage(),
            "message": message,
            "callback_query": query,
        },
    )()


def fake_settings():
    return {
        "slippage_bps": 1500,
        "priority_fee_lamports": 100000,
        "max_sol_per_trade": 0.1,
        "daily_spend_limit_sol": 0.5,
        "max_price_impact_pct": 50,
        "autosnipe_enabled": 1,
        "autosnipe_amount_sol": 0.05,
        "autosnipe_max_price_impact_pct": 30,
    }


class FakeDb:
    async def get_user(self, telegram_id):
        return {"wallet_address": "W", "trading_paused": 0}

    async def get_settings(self, telegram_id):
        return fake_settings()


class FakeLimiter:
    def allow(self, *args, **kwargs):
        return True


class FakeGate:
    async def check(self, bot, user_id, *, force=False):
        return GateResult(True, "", False, [])


class FakeSvc:
    def __init__(self):
        self.limiter = FakeLimiter()
        self.gate = FakeGate()
        self.db = FakeDb()
        self.cfg = type("Cfg", (), {"commands_per_30s": 12})()


class FakeApp:
    def __init__(self, svc):
        self.bot_data = {"svc": svc}


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.application = FakeApp(FakeSvc())
        self.bot = None
        self.args = []  # PTB command contexts carry the command arguments


def panel_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


async def test_config_panel_is_one_message_with_buttons():
    message = FakeMessage()
    context = FakeContext()
    await config_command(make_update(message=message), context)
    assert len(message.sent) == 1  # merged panel — no second "which setting" message
    assert "TRADING CONFIGURATION" in message.sent[0]
    assert "Tap a setting below to change it." in message.sent[0]
    assert flows.active_flow(context) is None  # flow starts only on a tap
    expected = {f"cfg:{name}" for name in SETTING_NAMES} | {"cfg:cancel"}
    assert {b.callback_data for b in panel_buttons(message.markups[0])} == expected


async def test_config_button_jumps_to_value_prompt():
    query = FakeQuery("cfg:slippage")
    message = FakeMessage()
    context = FakeContext()
    await on_config_callback(make_update(message=message, query=query), context)
    assert len(query.answered) == 1
    assert "Send the new value for" in message.sent[0] and "slippage" in message.sent[0]
    assert flows.active_flow(context) == "config"
    assert context.user_data[flows.STATE_KEY]["data"]["name"] == "slippage"


async def test_config_button_cancel_drops_flow():
    message = FakeMessage()
    context = FakeContext()
    context.user_data[flows.STATE_KEY] = {"flow": "config", "data": {"name": "max_sol"}, "ts": time.monotonic()}
    await on_config_callback(make_update(message=message, query=FakeQuery("cfg:cancel")), context)
    assert flows.active_flow(context) is None
    assert "cancelled" in message.sent[0]


async def test_config_button_ignores_unknown_name():
    query = FakeQuery("cfg:bogus")
    message = FakeMessage()
    context = FakeContext()
    await on_config_callback(make_update(message=message, query=query), context)
    assert message.sent == []
    assert flows.active_flow(context) is None


async def test_typed_setting_name_still_starts_flow():
    from sniper.app import _handle_text

    message = FakeMessage(text="impact")
    context = FakeContext()
    await _handle_text(make_update(message=message), context)
    assert flows.active_flow(context) == "config"
    assert any("Send the new value for" in text and "impact" in text for text in message.sent)


async def test_config_keyboard_covers_all_settings():
    data = [b.callback_data for b in panel_buttons(config_keyboard())]
    assert set(data) == {f"cfg:{name}" for name in SETTING_NAMES} | {"cfg:cancel"}
    assert len(data) == len(set(data))  # no duplicate callbacks
