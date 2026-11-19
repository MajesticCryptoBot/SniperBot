"""Tests for the rich buy/sell panels, trigger flows, and /triggers panel."""

from __future__ import annotations

import sniper.handlers.trade_panel as trade_panel
from sniper.handlers import flows

MINT = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
MINT2 = "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh"


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.sent: list[str] = []
        self.markups: list = []
        self._next_id = 100

    async def reply_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.sent.append(text)
        self.markups.append(reply_markup)
        self._next_id += 1
        return type("Sent", (), {"message_id": self._next_id, "chat_id": 7})()


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answered: list = []
        self.edited: list = []

    async def answer(self, text=None):
        self.answered.append(text)

    async def edit_message_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.edited.append((text, reply_markup))

    async def edit_message_reply_markup(self, markup):
        self.edited.append(("", markup))


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


SETTINGS = {
    "slippage_bps": 1500,
    "priority_fee_lamports": 100000,
    "max_sol_per_trade": 0.1,
    "daily_spend_limit_sol": 0.5,
    "autosnipe_enabled": 1,
    "autosnipe_amount_sol": 0.05,
    "autosnipe_max_price_impact_pct": 30,
    "max_price_impact_pct": 50,
}


class FakeRpc:
    def __init__(self, accounts=None):
        self.accounts = accounts or []
        self.balance = 500_000_000

    async def get_balance_lamports(self, address):
        return self.balance

    async def get_token_accounts(self, owner):
        return self.accounts

    async def get_mint_info(self, mint):
        return {"decimals": 6}


class FakePrices:
    async def usd_prices(self, mints):
        return {m: 0.5 for m in mints}

    async def token_profile(self, mint):
        return {"name": "Dogwifhat", "symbol": "WIF", "price_usd": 0.5, "liquidity_usd": 90000.0,
                "market_cap_usd": 1_000_000.0, "volume_24h_usd": 999.0, "change_24h_pct": 1.0, "dex": "raydium"}

    async def token_profiles(self, mints):
        return {m: await self.token_profile(m) for m in mints}


class FakeDb:
    def __init__(self):
        self.triggers: list[dict] = []
        self.trigger_rows: list[dict] = []
        self.cancelled: list[int] = []
        self.distances: list[tuple[int, float]] = []

    async def get_user(self, telegram_id):
        return {"wallet_address": "W", "trading_paused": 0}

    async def get_settings(self, telegram_id):
        return dict(SETTINGS)

    async def count_triggers_for(self, telegram_id):
        return len(self.triggers)

    async def add_trade_trigger(self, **kwargs):
        self.triggers.append(kwargs)
        return 42

    async def triggers_for(self, telegram_id):
        return self.trigger_rows

    async def cancel_trigger(self, trigger_id, telegram_id):
        self.cancelled.append(trigger_id)
        return True

    async def update_trigger_distance(self, trigger_id, telegram_id, pct):
        self.distances.append((trigger_id, pct))
        return True


class FakeCfg:
    commands_per_30s = 12
    max_watch_per_user = 10
    high_value_sol = 1.0
    is_mainnet = True
    absolute_max_sol = 100.0


class FakeSvc:
    def __init__(self, accounts=None):
        self.limiter = type("L", (), {"allow": staticmethod(lambda *a, **k: True)})()
        self.gate = type("G", (), {"check": staticmethod(_gate_check)})()
        self.db = FakeDb()
        self.rpc = FakeRpc(accounts)
        self.prices = FakePrices()
        self.cfg = FakeCfg()


async def _gate_check(bot, user_id, *, force=False):
    return type("R", (), {"ok": True, "user_message": ""})()


class FakeApp:
    def __init__(self, svc):
        self.bot_data = {"svc": svc}


class FakeBot:
    def __init__(self):
        self.edited: list[dict] = []

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.edited.append({"text": text, "chat_id": chat_id, "message_id": message_id, "markup": reply_markup})


class FakeContext:
    def __init__(self, svc=None):
        self.user_data = {}
        self.application = FakeApp(svc or FakeSvc())
        self.bot = FakeBot()
        self.args = []


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


async def test_buy_panel_opens_with_presets_and_market_data():
    message = FakeMessage()
    context = FakeContext()
    context.args = [MINT]  # /buy <mint>
    await trade_panel.buy_entry(make_update(message=message), context)
    assert len(message.sent) == 1
    text = message.sent[0]
    for marker in ("🛒", "Dogwifhat (WIF)", MINT, "0.500000 SOL", "Slippage: 15.00% (account default)"):
        assert marker in text, marker
    labels = [b.text for b in buttons(message.markups[0])]
    for preset in ("0.01 SOL", "0.05 SOL", "0.1 SOL", "0.5 SOL", "1 SOL"):
        assert preset in labels, preset
    # no flow was opened — the panel waits for taps
    assert flows.active_flow(context) is None


async def test_buy_without_mint_asks_for_the_token():
    message = FakeMessage()
    context = FakeContext()
    await trade_panel.buy_entry(make_update(message=message), context)
    assert len(message.sent) == 1
    text = message.sent[0]
    assert "Send the token mint" in text
    assert "BUY TERMINAL" not in text  # no panel until a CA arrives
    assert message.markups[0] is None  # plain prompt, no buttons
    assert flows.active_flow(context) == "panel_buy_mint"  # the next reply is captured
    assert trade_panel._panel(context) is None


async def test_buy_reply_with_ca_opens_the_panel_below():
    message = FakeMessage()
    context = FakeContext()
    await trade_panel.buy_entry(make_update(message=message), context)
    await flows.handle(make_update(message=message), context, MINT)
    assert context.bot.edited == []  # nothing edited — the panel is a new message
    panel = message.sent[-1]
    for marker in ("🛒", MINT, "Dogwifhat (WIF)", "0.500000 SOL"):
        assert marker in panel, marker
    labels = [b.text for b in buttons(message.markups[-1])]
    assert "0.01 SOL" in labels and any("Confirm buy" in label for label in labels)
    assert trade_panel._panel(context)["mint"] == MINT
    assert context.user_data.get(flows.STATE_KEY) is None  # flow finished


async def test_buy_reply_with_garbage_reprompts():
    message = FakeMessage()
    context = FakeContext()
    await trade_panel.buy_entry(make_update(message=message), context)
    await flows.handle(make_update(message=message), context, "hello there")
    assert "Not a valid Solana token mint" in message.sent[-1]
    assert flows.active_flow(context) == "panel_buy_mint"


async def test_buy_reply_with_non_mint_address_reprompts(monkeypatch):
    from sniper.errors import UserFacingError

    async def not_a_mint(mint):
        raise UserFacingError("not a mint")

    message = FakeMessage()
    context = FakeContext()
    await trade_panel.buy_entry(make_update(message=message), context)
    monkeypatch.setattr(context.application.bot_data["svc"].rpc, "get_mint_info", not_a_mint)
    await flows.handle(make_update(message=message), context, MINT2)
    assert "not a token mint" in message.sent[-1]
    assert flows.active_flow(context) == "panel_buy_mint"  # stays armed for another try


async def test_buy_reply_with_wsol_is_rejected():
    from sniper.solana_service import WSOL

    message = FakeMessage()
    context = FakeContext()
    await trade_panel.buy_entry(make_update(message=message), context)
    await flows.handle(make_update(message=message), context, WSOL)
    assert "wrapped SOL" in message.sent[-1]
    assert flows.active_flow(context) == "panel_buy_mint"


async def test_close_cancels_the_paste_flow():
    message = FakeMessage()
    context = FakeContext()
    await trade_panel.buy_entry(make_update(message=message), context)  # arms the flow
    context.user_data[trade_panel.PANEL_KEY] = {"side": "buy", "mint": MINT, "mode": "main"}
    await trade_panel.on_trade_panel_callback(make_update(message=message, query=FakeQuery("tp:close")), context)
    assert flows.active_flow(context) is None
    assert trade_panel._panel(context) is None


async def test_amount_tap_edits_panel_in_place():
    query = FakeQuery(f"tp:amt:0.05:{MINT}")
    context = FakeContext()
    context.user_data[trade_panel.PANEL_KEY] = {"side": "buy", "mint": MINT, "mode": "main"}
    await trade_panel.on_trade_panel_callback(make_update(query=query), context)
    assert "Spend: 0.050000 SOL" in query.edited[0][0]
    assert trade_panel._panel(context)["amount"] == 0.05


async def test_slippage_override_flow():
    context = FakeContext()
    context.user_data[trade_panel.PANEL_KEY] = {"side": "buy", "mint": MINT, "mode": "main"}
    query = FakeQuery(f"tp:slipmenu:{MINT}")
    await trade_panel.on_trade_panel_callback(make_update(query=query), context)
    assert "Back" in [b.text for b in buttons(query.edited[0][1])]
    query2 = FakeQuery(f"tp:slip:300:{MINT}")
    await trade_panel.on_trade_panel_callback(make_update(query=query2), context)
    text = query2.edited[0][0]
    assert "Slippage: 3.00% (this order)" in text
    assert trade_panel._panel(context)["slip_bps"] == 300


async def test_confirm_calls_buy_preview_with_overrides(monkeypatch):
    recorded = {}

    async def fake_preview(update, context, svc, mint, amount, *, slippage_bps=None, priority_fee_lamports=None):
        recorded.update(mint=mint, amount=amount, slip=slippage_bps, fee=priority_fee_lamports)

    monkeypatch.setattr(trade_panel, "build_buy_preview", fake_preview)
    context = FakeContext()
    context.user_data[trade_panel.PANEL_KEY] = {
        "side": "buy", "mint": MINT, "mode": "main", "amount": 0.05, "slip_bps": 300, "fee_lamports": 50000,
    }
    message = FakeMessage()
    query = FakeQuery(f"tp:go:{MINT}")
    await trade_panel.on_trade_panel_callback(make_update(message=message, query=query), context)
    assert recorded == {"mint": MINT, "amount": 0.05, "slip": 300, "fee": 50000}
    assert trade_panel._panel(context) is None  # panel closed after handing off
    assert "preview sent below" in query.edited[0][0]


async def test_confirm_sell_uses_percent_spec(monkeypatch):
    recorded = {}

    async def fake_sell_preview(update, context, svc, mint, spec, *, slippage_bps=None, priority_fee_lamports=None):
        recorded.update(mint=mint, spec=spec, slip=slippage_bps)

    monkeypatch.setattr(trade_panel, "build_sell_preview", fake_sell_preview)
    svc = FakeSvc(accounts=[{"mint": MINT, "amount_raw": 1_000_000, "decimals": 6}])
    context = FakeContext(svc)
    query = FakeQuery(f"tp:sell:{MINT}")
    await trade_panel.on_trade_panel_callback(make_update(query=query), context)
    assert "1.00 tokens" in query.edited[0][0]  # the panel opens edited in place
    await trade_panel.on_trade_panel_callback(make_update(query=FakeQuery(f"tp:pct:50:{MINT}")), context)
    assert trade_panel._panel(context)["pct"] == 50
    query = FakeQuery(f"tp:go:{MINT}")
    await trade_panel.on_trade_panel_callback(make_update(query=query), context)
    assert recorded["spec"] == "50%" and recorded["mint"] == MINT


async def test_sell_holdings_panel_lists_tokens():
    svc = FakeSvc(accounts=[
        {"mint": MINT, "amount_raw": 1_000_000, "decimals": 6},
        {"mint": MINT2, "amount_raw": 2_000_000_000, "decimals": 9},
    ])
    message = FakeMessage()
    await trade_panel.sell_entry(make_update(message=message), FakeContext(svc))
    text = message.sent[0]
    assert "SELL · WALLET HOLDINGS" in text
    assert "$0.5000" in text  # live price per line
    data = [b.callback_data for b in buttons(message.markups[0])]
    assert f"tp:sell:{MINT}" in data and f"tp:sell:{MINT2}" in data
    assert "tp:refreshsell" in data and "tp:paste" in data


async def test_empty_wallet_sell_shows_friendly_message():
    message = FakeMessage()
    context = FakeContext(FakeSvc(accounts=[]))
    await trade_panel.sell_entry(make_update(message=message), context)
    text = message.sent[0]
    assert "No SPL token balances" in text
    assert "send a token mint" in text
    assert message.markups[0] is None  # plain prompt, no buttons
    assert flows.active_flow(context) == "panel_sell_mint"
    assert trade_panel._panel(context) is None


async def test_sell_reply_with_held_token_opens_panel_below():
    svc = FakeSvc(accounts=[{"mint": MINT, "amount_raw": 1_000_000, "decimals": 6}])
    message = FakeMessage()
    context = FakeContext(svc)
    await trade_panel.sell_entry(make_update(message=message), context)
    await trade_panel.on_trade_panel_callback(make_update(message=message, query=FakeQuery("tp:paste")), context)
    await flows.handle(make_update(message=message), context, MINT)
    assert context.bot.edited == []  # nothing edited — the panel is a new message
    panel = message.sent[-1]
    assert "SELL TERMINAL" in panel
    assert "1.00 tokens" in panel
    assert trade_panel._panel(context)["mint"] == MINT
    assert context.user_data.get(flows.STATE_KEY) is None


async def test_sell_reply_with_unheld_token_is_rejected():
    message = FakeMessage()
    context = FakeContext(FakeSvc(accounts=[]))
    await trade_panel.sell_entry(make_update(message=message), context)
    await flows.handle(make_update(message=message), context, MINT2)
    assert "No balance for this token" in message.sent[-1]
    assert flows.active_flow(context) == "panel_sell_mint"  # stays armed for another try


async def test_paste_button_on_holdings_starts_sell_mint_flow():
    svc = FakeSvc(accounts=[{"mint": MINT, "amount_raw": 1_000_000, "decimals": 6}])
    message = FakeMessage()
    context = FakeContext(svc)
    await trade_panel.sell_entry(make_update(message=message), context)
    await trade_panel.on_trade_panel_callback(make_update(message=message, query=FakeQuery("tp:paste")), context)
    assert flows.active_flow(context) == "panel_sell_mint"
    assert "Send the token mint to sell" in message.sent[-1]


async def test_custom_buy_amount_survives_the_panel_refresh():
    message = FakeMessage()
    context = FakeContext()
    context.user_data[trade_panel.PANEL_KEY] = {"side": "buy", "mint": MINT, "mode": "main"}
    await trade_panel.on_trade_panel_callback(make_update(message=message, query=FakeQuery(f"tp:custom:{MINT}")), context)
    assert flows.active_flow(context) == "panel_buy_amount"
    await flows.handle(make_update(message=message), context, "0.25")
    assert trade_panel._panel(context)["amount"] == 0.25
    assert "Spend: 0.250000 SOL" in message.sent[-1]


async def test_limit_buy_flow_arms_trigger():
    context = FakeContext()
    context.user_data[trade_panel.PANEL_KEY] = {"side": "buy", "mint": MINT, "mode": "main"}
    message = FakeMessage()
    await trade_panel.on_trade_panel_callback(make_update(message=message, query=FakeQuery(f"tp:limit:{MINT}")), context)
    assert flows.active_flow(context) == "limit_buy"
    await flows.handle(make_update(message=message), context, "$0.42")
    await flows.handle(make_update(message=message), context, "0.05")
    assert context.user_data.get(flows.STATE_KEY) is None  # flow finished
    db = context.application.bot_data["svc"].db
    assert db.triggers == [{
        "telegram_id": "7", "kind": "limit_buy", "mint": MINT,
        "target_price_usd": 0.42, "amount_sol": 0.05,
    }]
    assert "LIMIT BUY ACTIVATED" in message.sent[-1]


async def test_trailing_stop_flow_arms_trigger():
    context = FakeContext()
    context.user_data[trade_panel.PANEL_KEY] = {"side": "sell", "mint": MINT, "mode": "main"}
    message = FakeMessage()
    await trade_panel.on_trade_panel_callback(make_update(message=message, query=FakeQuery(f"tp:trail:{MINT}")), context)
    assert flows.active_flow(context) == "trailing_stop"
    await flows.handle(make_update(message=message), context, "20")
    await flows.handle(make_update(message=message), context, "100%")
    db = context.application.bot_data["svc"].db
    assert db.triggers == [{
        "telegram_id": "7", "kind": "trailing_stop", "mint": MINT,
        "trail_distance_pct": 20.0, "sell_percent": 100.0,
    }]


async def test_triggers_panel_and_callbacks():
    context = FakeContext()
    svc = context.application.bot_data["svc"]
    svc.db.trigger_rows = [
        {"id": 5, "kind": "trailing_stop", "token_mint": MINT, "target_price_usd": None,
         "trail_distance_pct": 20, "peak_price_usd": 1.5, "amount_sol": None, "sell_percent": 100,
         "status": "open", "trigger_error": None},
        {"id": 6, "kind": "limit_buy", "token_mint": MINT2, "target_price_usd": 0.4,
         "trail_distance_pct": None, "peak_price_usd": None, "amount_sol": 0.1, "sell_percent": None,
         "status": "open", "trigger_error": None},
    ]
    message = FakeMessage()
    await trade_panel.triggers_command(make_update(message=message), context)
    text = message.sent[0]
    assert "ORDER MANAGEMENT" in text and "TRAILING STOP" in text and "LIMIT BUY" in text
    data = [b.callback_data for b in buttons(message.markups[0])]
    assert "tt:cancel:5" in data and "tt:distmenu:5" in data and "tt:cancel:6" in data
    query = FakeQuery("tt:cancel:5")
    await trade_panel.on_trigger_callback(make_update(query=query), context)
    assert svc.db.cancelled == [5]
    query2 = FakeQuery("tt:dist:5:30")
    await trade_panel.on_trigger_callback(make_update(query=query2), context)
    assert svc.db.distances == [(5, 30.0)]


async def test_all_panel_callbacks_fit_64_bytes():
    long_mint = "4kjruqGHDsSqkMdFfCjV7Vb62HPFhtXyWc2t6JpQzDnz"  # 44 chars
    settings = dict(SETTINGS)
    for state in (
        {"side": "buy", "mint": long_mint, "mode": "main"},
        {"side": "buy", "mint": long_mint, "mode": "slip"},
        {"side": "buy", "mint": long_mint, "mode": "fee"},
        {"side": "sell", "mint": long_mint, "mode": "main"},
        {"side": "sell", "mint": long_mint, "mode": "slip"},
    ):
        markup = trade_panel._buy_keyboard(state, settings) if state["side"] == "buy" else trade_panel._sell_keyboard(state, settings)
        for button in buttons(markup):
            assert len(button.callback_data) <= 64, button.callback_data
