"""Smoke tests for the conversational flow engine."""

from __future__ import annotations

import pytest

import sniper.handlers.admin  # noqa: F401 — registers admin_user flow
import sniper.handlers.settings  # noqa: F401 — registers config/autosnipe flows
import sniper.handlers.trading  # noqa: F401 — registers buy/sell/withdraw/scan flows
from sniper.errors import UserFacingError
from sniper.handlers import flows
from sniper.subscription import GateResult


class FakeMessage:
    def __init__(self):
        self.sent: list[str] = []
        self.text = ""

    async def reply_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.sent.append(text)


class FakeUpdate:
    def __init__(self, user_id=7):
        self.effective_user = type("U", (), {"id": user_id})()
        self.effective_message = FakeMessage()


class FakeLimiter:
    def allow(self, *args, **kwargs):
        return True


class FakeGate:
    async def check(self, bot, user_id, *, force=False):
        return GateResult(True, "", False, [])


class FakeSafety:
    async def evaluate(self, mint, **kwargs):
        return type("R", (), {"summary": lambda self: "ok"})()


class FakeDb:
    async def get_user(self, telegram_id):
        return {"wallet_address": "W", "trading_paused": 0}


class FakeCfg:
    commands_per_30s = 12
    is_mainnet = False  # executors stop at the mainnet gate with a clean error
    network = "devnet"


class FakeSvc:
    def __init__(self):
        self.limiter = FakeLimiter()
        self.gate = FakeGate()
        self.safety = FakeSafety()
        self.cfg = FakeCfg()
        self.db = FakeDb()


class FakeApp:
    def __init__(self, svc):
        self.bot_data = {"svc": svc}


class FakeContext:
    def __init__(self, svc):
        self.user_data = {}
        self.application = FakeApp(svc)
        self.bot = None  # FakeGate ignores it; ensure_subscribed only passes it through


@pytest.fixture(autouse=True)
def _restore_flows():
    snapshot = dict(flows.FLOWS)
    yield
    flows.FLOWS.clear()
    flows.FLOWS.update(snapshot)


def test_mint_parser_normalizes_and_reprompts():
    mint = "So11111111111111111111111111111111111111112"
    assert flows.parse_mint(f"  {mint}\n") == mint
    with pytest.raises(flows.StepError):
        flows.parse_mint("not an address")


def test_sol_amount_parser_accepts_sol_suffix():
    assert flows.parse_sol_amount("0.5") == 0.5
    assert flows.parse_sol_amount("1.5 SOL") == 1.5
    with pytest.raises(flows.StepError):
        flows.parse_sol_amount("-1")
    with pytest.raises(flows.StepError):
        flows.parse_sol_amount("abc")


def test_token_or_percent_parser():
    assert flows.parse_token_or_percent("50 %") == "50%"
    assert flows.parse_token_or_percent("1.5") == "1.5"
    with pytest.raises(flows.StepError):
        flows.parse_token_or_percent("150%")
    with pytest.raises(flows.StepError):
        flows.parse_token_or_percent("x")


def test_onoff_and_telegram_id_parsers():
    assert flows.parse_onoff("ON") is True
    assert flows.parse_onoff("disable") is False
    with pytest.raises(flows.StepError):
        flows.parse_onoff("maybe")
    assert flows.parse_telegram_id("123") == "123"
    with pytest.raises(flows.StepError):
        flows.parse_telegram_id("12a3")


@pytest.mark.asyncio
async def test_registered_trading_flows_exist():
    assert {"buy", "sell", "withdraw", "scan"} <= set(flows.FLOWS)
    assert {"config", "autosnipe_add"} <= set(flows.FLOWS)
    assert "admin_user" in flows.FLOWS


@pytest.mark.asyncio
async def test_scan_flow_two_steps_then_execute():
    svc = FakeSvc()
    context = FakeContext(svc)
    update = FakeUpdate()

    await flows.begin(update, context, "scan")
    assert flows.active_flow(context) == "scan"
    assert "assess" in update.effective_message.sent[-1].lower()
    mint = "So11111111111111111111111111111111111111112"
    consumed = await flows.handle(update, context, mint)
    assert consumed
    assert flows.active_flow(context) is None  # single-step flow executed
    assert mint in update.effective_message.sent[-1]


@pytest.mark.asyncio
async def test_scan_with_prefill_executes_immediately():
    svc = FakeSvc()
    context = FakeContext(svc)
    update = FakeUpdate()
    mint = "So11111111111111111111111111111111111111112"
    await flows.begin(update, context, "scan", prefill={"mint": mint})
    assert flows.active_flow(context) is None
    assert mint in update.effective_message.sent[-1]


@pytest.mark.asyncio
async def test_invalid_prefill_falls_back_to_prompt():
    svc = FakeSvc()
    context = FakeContext(svc)
    update = FakeUpdate()
    await flows.begin(update, context, "scan", prefill={"mint": "garbage"})
    assert flows.active_flow(context) == "scan"
    assert "Not a valid Solana token mint" in update.effective_message.sent[-1]


@pytest.mark.asyncio
async def test_invalid_input_reprompts_and_cancel_clears():
    svc = FakeSvc()
    context = FakeContext(svc)
    update = FakeUpdate()
    await flows.begin(update, context, "scan")
    await flows.handle(update, context, "nope")
    assert flows.active_flow(context) == "scan"  # still waiting
    assert "Not a valid Solana token mint" in update.effective_message.sent[-1]
    mint = "So11111111111111111111111111111111111111112"
    await flows.handle(update, context, mint)
    assert flows.active_flow(context) is None

    await flows.begin(update, context, "scan")
    assert flows.cancel(context) == "scan"
    assert flows.cancel(context) is None


@pytest.mark.asyncio
async def test_two_step_buy_flow_prompt_order():
    svc = FakeSvc()
    context = FakeContext(svc)
    update = FakeUpdate()
    await flows.begin(update, context, "buy")
    prompts = " ".join(update.effective_message.sent).lower()
    assert "token mint" in prompts
    mint = "So11111111111111111111111111111111111111112"
    await flows.handle(update, context, mint)
    assert "buy amount" in update.effective_message.sent[-1].lower()
    assert flows.active_flow(context) == "buy"
    # executor stops at the mainnet gate; the engine surfaces the clean error and ends the flow
    await flows.handle(update, context, "0.1")
    assert flows.active_flow(context) is None
    assert "unavailable on the current network configuration" in update.effective_message.sent[-1].lower()


@pytest.mark.asyncio
async def test_expired_flow_times_out():
    svc = FakeSvc()
    context = FakeContext(svc)
    update = FakeUpdate()
    await flows.begin(update, context, "scan")
    context.user_data[flows.STATE_KEY]["ts"] -= flows.FLOW_TTL + 1
    consumed = await flows.handle(update, context, "anything")
    assert consumed
    assert flows.active_flow(context) is None
    assert "expired" in update.effective_message.sent[-1].lower()


@pytest.mark.asyncio
async def test_handle_without_flow_returns_false():
    svc = FakeSvc()
    context = FakeContext(svc)
    update = FakeUpdate()
    assert await flows.handle(update, context, "hello") is False


# ---------------------------------------------------------------------------
# Autosnipe gate: a live-token refusal closes the dialog
# ---------------------------------------------------------------------------

WSOL = "So11111111111111111111111111111111111111112"
LIVE_MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"


class _FixedPrices:
    """WSOL probe and the watched mint are priced independently."""

    def __init__(self, mint_price: float, wsol_price: float = 1.0):
        self.mint_price = mint_price
        self.wsol_price = wsol_price

    async def usd_prices(self, mints):
        return {m: (self.wsol_price if m == WSOL else self.mint_price) for m in mints}


@pytest.mark.asyncio
async def test_live_token_refusal_closes_the_autosnipe_dialog():
    svc = FakeSvc()
    svc.prices = _FixedPrices(0.5)  # mint priced → live
    context = FakeContext(svc)
    update = FakeUpdate()
    await flows.begin(update, context, "autosnipe_add")
    assert flows.active_flow(context) == "autosnipe_add"
    consumed = await flows.handle(update, context, LIVE_MINT)
    assert consumed
    assert "already live" in update.effective_message.sent[-1]
    assert flows.active_flow(context) is None  # dialog closed, not left pending
    # the user's next paste is NOT swallowed by the old dialog
    assert await flows.handle(update, context, LIVE_MINT) is False


@pytest.mark.asyncio
async def test_market_check_refusal_keeps_the_dialog_open():
    svc = FakeSvc()
    svc.prices = _FixedPrices(0.0, wsol_price=0.0)  # probe unpriced → service looks down
    context = FakeContext(svc)
    update = FakeUpdate()
    await flows.begin(update, context, "autosnipe_add")
    consumed = await flows.handle(update, context, LIVE_MINT)
    assert consumed
    assert "could not verify" in update.effective_message.sent[-1]
    assert flows.active_flow(context) == "autosnipe_add"  # transient — retry in place


@pytest.mark.asyncio
async def test_prefill_of_a_live_token_does_not_arm_the_flow():
    svc = FakeSvc()
    svc.prices = _FixedPrices(0.5)
    context = FakeContext(svc)
    update = FakeUpdate()
    await flows.begin(update, context, "autosnipe_add", prefill={"mint": LIVE_MINT})
    assert flows.active_flow(context) is None
    assert "already live" in update.effective_message.sent[-1]
