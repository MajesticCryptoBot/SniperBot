"""Tests for the wallet panel buttons and the enriched token-detection panel."""

from __future__ import annotations

from telegram.error import BadRequest

import sniper.handlers.trading  # noqa: F401 — registers the withdraw flow
import sniper.handlers.wallet as wallet_mod
from sniper.handlers import flows
from sniper.handlers.token_panel import (
    _fmt_price,
    _fmt_usd,
    detect_pasted_token,
    on_token_callback,
)
from sniper.handlers.wallet import on_wallet_callback, wallet
from sniper.keyboards import token_options_keyboard, wallet_keyboard
from sniper.prices import PriceService, _best_pair, parse_dexscreener_pairs
from sniper.solana_service import WSOL
from sniper.subscription import GateResult


class FakeMessage:
    def __init__(self):
        self.sent: list[str] = []
        self.markups: list = []

    async def reply_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.sent.append(text)
        self.markups.append(reply_markup)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answered: list = []
        self.edited: list = []

    async def answer(self, text=None):
        self.answered.append(text)

    async def edit_message_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.edited.append((text, reply_markup))


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


class FakeLimiter:
    def allow(self, *args, **kwargs):
        return True


class FakeGate:
    async def check(self, bot, user_id, *, force=False):
        return GateResult(True, "", False, [])


class FakePrices:
    def __init__(self, profile=None):
        self.profile = profile

    async def usd_prices(self, mints):
        return {mint: 0.5 for mint in mints}

    async def token_profile(self, mint):
        return self.profile


class FakeSvc:
    def __init__(self, profile=None):
        self.limiter = FakeLimiter()
        self.gate = FakeGate()
        self.cfg = type("Cfg", (), {"commands_per_30s": 12})()
        self.db = type("Db", (), {})()
        self.db.get_user = self._user
        self.db.get_settings = self._settings
        self.rpc = type("Rpc", (), {})()
        self.rpc.get_balance_lamports = self._balance
        self.rpc.get_mint_info = self._mint_info
        self.prices = FakePrices(profile)

    async def _user(self, telegram_id):
        return {"wallet_address": "W", "trading_paused": 0, "chat_id": "7"}

    async def _settings(self, telegram_id):
        # auto-buy is off in these tests — detection shows the panel
        return {"auto_buy_enabled": 0, "auto_buy_amount_sol": 0.05}

    async def _balance(self, address):
        return 50_000_000

    async def _mint_info(self, address):
        return {"freeze_authority": None, "mint_authority": None}


class FakeApp:
    def __init__(self, svc):
        self.bot_data = {"svc": svc}


class FakeContext:
    def __init__(self, svc):
        self.user_data = {}
        self.application = FakeApp(svc)
        self.bot = None
        self.args = []


def fake_profile():
    return {
        "name": "Dogwifhat",
        "symbol": "WIF",
        "price_usd": "0.00000123",
        "liquidity_usd": 12345.0,
        "market_cap_usd": 1234567.0,
        "volume_24h_usd": 98765.0,
        "change_24h_pct": -4.2,
        "dex": "raydium",
    }


def buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_wallet_panel_has_action_buttons():
    message = FakeMessage()
    await wallet(make_update(message=message), FakeContext(FakeSvc()))
    assert "WALLET" in message.sent[0] and "Balance" in message.sent[0]
    assert set(buttons(message.markups[0])) == {"wal:withdraw", "wal:summary", "wal:refresh", "wal:config"}


async def test_wallet_withdraw_button_starts_withdraw_flow():
    query = FakeQuery("wal:withdraw")
    message = FakeMessage()
    context = FakeContext(FakeSvc())
    await on_wallet_callback(make_update(message=message, query=query), context)
    assert flows.active_flow(context) == "withdraw"
    assert message.sent  # the destination prompt went out


async def test_wallet_summary_button_reruns_summary(monkeypatch):
    calls = []

    async def fake_summary(update, context):
        calls.append(1)

    monkeypatch.setattr(wallet_mod, "summary", fake_summary)
    await on_wallet_callback(make_update(query=FakeQuery("wal:summary")), FakeContext(FakeSvc()))
    assert calls == [1]


async def test_wallet_refresh_button_reruns_wallet(monkeypatch):
    calls = []

    async def fake_wallet(update, context):
        calls.append(1)

    monkeypatch.setattr(wallet_mod, "wallet", fake_wallet)
    await on_wallet_callback(make_update(query=FakeQuery("wal:refresh")), FakeContext(FakeSvc()))
    assert calls == [1]


async def test_wallet_config_button_opens_settings(monkeypatch):
    calls = []

    async def fake_config(update, context):
        calls.append(1)

    monkeypatch.setattr(wallet_mod, "config_command", fake_config)
    await on_wallet_callback(make_update(query=FakeQuery("wal:config")), FakeContext(FakeSvc()))
    assert calls == [1]


async def test_wallet_keyboard_shape():
    assert set(buttons(wallet_keyboard())) == {"wal:withdraw", "wal:summary", "wal:refresh", "wal:config"}


async def test_token_panel_is_rich_when_market_data_exists():
    message = FakeMessage()
    mint = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
    handled = await detect_pasted_token(make_update(message=message), FakeContext(FakeSvc(fake_profile())), FakeSvc(fake_profile()), mint)
    assert handled
    text = message.sent[0]
    for marker in ("Asset: Dogwifhat (WIF)", "Price: $0.00000123", "Liquidity: $12.3K on raydium",
                   "Market cap: $1.23M", "24h volume: $98.8K", "24h change: -4.2%",
                   "🟢 Market: LIVE", mint):
        assert marker in text, marker


async def test_token_panel_degrades_without_market_data():
    message = FakeMessage()
    mint = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
    handled = await detect_pasted_token(make_update(message=message), FakeContext(FakeSvc()), FakeSvc(), mint)
    assert handled
    text = message.sent[0]
    assert "Market data is not available yet" in text
    assert mint in text  # the panel still works, just thinner


def test_usd_formatting():
    assert _fmt_usd(12345) == "$12.3K"
    assert _fmt_usd(1234567) == "$1.23M"
    assert _fmt_usd(999) == "$999"
    assert _fmt_usd(None) is None
    assert _fmt_price("0.00000123") == "$0.00000123"
    assert _fmt_price(0.5) == "$0.5000"
    assert _fmt_price(2) == "$2.00"
    assert _fmt_price(None) is None


def test_best_pair_is_the_deepest_liquidity():
    pairs = parse_dexscreener_pairs({"pairs": [
        {"liquidity": {"usd": 100}, "baseToken": {"symbol": "S"}},
        {"liquidity": {"usd": 90000}, "baseToken": {"symbol": "B"}},
    ]})
    assert _best_pair(pairs)["baseToken"]["symbol"] == "B"
    assert parse_dexscreener_pairs(None) == []


async def test_token_profile_maps_fields_defensively():
    payload = {"pairs": [{
        "baseToken": {"name": "Big", "symbol": "B"},
        "priceUsd": "3",
        "liquidity": {"usd": 90000},
        "volume": {"h24": "999"},
        "priceChange": {"h24": "-1.5"},
        "dexId": "raydium",
    }]}
    profile = await PriceService(FakeHttpLike(payload), "u").token_profile("MINT")
    assert profile["symbol"] == "B" and profile["price_usd"] == 3.0
    assert profile["liquidity_usd"] == 90000.0 and profile["volume_24h_usd"] == 999.0
    assert profile["change_24h_pct"] == -1.5 and profile["dex"] == "raydium"
    assert profile["market_cap_usd"] is None  # marketCap and fdv both missing


class FakeHttpLike:
    def __init__(self, payload):
        self._payload = payload

    async def get(self, url, **kwargs):
        payload = self._payload
        return type("R", (), {"status_code": 200, "json": lambda self: payload})()


async def test_token_profile_returns_none_for_unlisted_or_down():
    svc = PriceService.__new__(PriceService)
    svc.http = _FakeHttp({"pairs": []})
    assert await svc.token_profile("MINT") is None
    svc.http = _FakeHttp({}, status_code=404)
    assert await svc.token_profile("MINT") is None


class _FakeHttp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status = status_code

    async def get(self, url, **kwargs):
        payload, status = self._payload, self._status
        return type("R", (), {"status_code": status, "json": lambda self: payload})()


async def test_token_keyboard_has_refresh():
    mint = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
    data = [b.callback_data for row in token_options_keyboard(mint).inline_keyboard for b in row]
    assert f"tok:refresh:{mint}" in data
    assert data.count("tok:close") == 1


async def test_token_refresh_edits_panel_in_place():
    mint = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
    query = FakeQuery(f"tok:refresh:{mint}")
    message = FakeMessage()
    await on_token_callback(make_update(message=message, query=query), FakeContext(FakeSvc(fake_profile())))
    assert message.sent == []  # no new message — the same panel was edited in place
    text, markup = query.edited[0]
    assert "TOKEN DETECTED" in text and "Dogwifhat (WIF)" in text and mint in text
    assert f"tok:refresh:{mint}" in buttons(markup)


async def test_token_refresh_shows_updated_numbers():
    mint = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
    svc = FakeSvc(fake_profile())
    context = FakeContext(svc)
    message = FakeMessage()
    await detect_pasted_token(make_update(message=message), context, svc, mint)
    assert "Price: $0.00000123" in message.sent[0]
    updated = fake_profile()
    updated["price_usd"] = "9.5"
    updated["liquidity_usd"] = 250000.0
    svc.prices.profile = updated
    query = FakeQuery(f"tok:refresh:{mint}")
    await on_token_callback(make_update(message=message, query=query), context)
    text = query.edited[0][0]
    assert "Price: $9.50" in text
    assert "Liquidity: $250.0K" in text


async def test_token_refresh_failure_keeps_panel_intact():
    mint = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"

    async def boom(address):
        raise RuntimeError("rpc down")

    svc = FakeSvc()
    svc.rpc.get_mint_info = boom
    query = FakeQuery(f"tok:refresh:{mint}")
    message = FakeMessage()
    await on_token_callback(make_update(message=message, query=query), FakeContext(svc))
    assert query.edited == []  # the original panel and its buttons survive
    assert "did not respond" in message.sent[0]


async def test_token_refresh_tolerates_not_modified():
    mint = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"

    async def not_modified(text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        raise BadRequest("Message is not modified")

    query = FakeQuery(f"tok:refresh:{mint}")
    query.edit_message_text = not_modified
    message = FakeMessage()
    await on_token_callback(make_update(message=message, query=query), FakeContext(FakeSvc(fake_profile())))
    assert message.sent == []  # data was already current — no error spam


# ---------------------------------------------------------------------------
# Watchlist button: already-live tokens are refused before the flow starts
# ---------------------------------------------------------------------------


class _ProbePrices:
    """Add-gate price fake: WSOL always prices the probe, the mint is controllable."""

    def __init__(self, mint_price: float):
        self.mint_price = mint_price

    async def usd_prices(self, mints):
        return {mint: (1.0 if mint == WSOL else self.mint_price) for mint in mints}

    async def token_profile(self, mint):
        return None


def _gate_svc(mint_price: float):
    svc = FakeSvc()

    async def _get_state(key):
        return "0"

    svc.db.get_state = _get_state
    svc.prices = _ProbePrices(mint_price)
    return svc


async def test_watchlist_button_refuses_a_live_token():
    mint = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
    svc = _gate_svc(0.5)
    context = FakeContext(svc)
    message = FakeMessage()
    await on_token_callback(make_update(message=message, query=FakeQuery(f"tok:auto:{mint}")), context)
    assert any("already live" in text for text in message.sent)
    assert flows.active_flow(context) is None  # refused — no flow, no amount prompt


async def test_watchlist_button_arms_the_flow_for_a_token_with_no_market():
    mint = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
    svc = _gate_svc(0.0)
    context = FakeContext(svc)
    message = FakeMessage()
    await on_token_callback(make_update(message=message, query=FakeQuery(f"tok:auto:{mint}")), context)
    assert flows.active_flow(context) == "autosnipe_add"
    assert any("ENTRY AMOUNT" in text for text in message.sent)
