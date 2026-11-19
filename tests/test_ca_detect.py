"""CA detection in messages and links.

The bot must detect a token wherever the user shares it: a bare CA, a CA
inside a sentence, a CA inside a link (pump.fun, DexScreener, Solscan,
Birdeye, Photon…), or a pool/pair link that has to be resolved to the
base token first. Everything here is text-level; the on-chain mint check
still gates every panel."""

from __future__ import annotations

import pytest

from sniper.ca_detect import extract_mints, resolve_pair_to_mint
from sniper.errors import UserFacingError
from sniper.handlers import flows
from sniper.handlers.token_panel import detect_from_text

MINT = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
MINT2 = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6dxjnGY4eB6d4WmP"
WALLET = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
PAIR = "5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9"  # a real DexScreener pool address
WSOL = "So11111111111111111111111111111111111111112"


# ---------------------------------------------------------------------------
# extract_mints — bare CA, CA in a message, CA in a link
# ---------------------------------------------------------------------------


def test_extract_bare_mint():
    assert extract_mints(f"  {MINT}\n") == [MINT]


def test_extract_mint_inside_a_sentence():
    assert extract_mints(f"check this one 🚀🚀 {MINT}") == [MINT]


def test_extract_keeps_reading_order_and_dedupes():
    assert extract_mints(f"{MINT} vs {MINT2}") == [MINT, MINT2]
    assert extract_mints(f"{MINT} and again {MINT}") == [MINT]


def test_extract_mint_from_links():
    assert extract_mints(f"https://pump.fun/{MINT}") == [MINT]
    assert extract_mints(f"https://pump.fun/coin/{MINT}") == [MINT]
    assert extract_mints(f"https://solscan.io/token/{MINT}") == [MINT]
    assert extract_mints(f"https://birdeye.so/token/{MINT}?chain=solana") == [MINT]
    assert extract_mints(f"https://jup.ag/tokens/{MINT}") == [MINT]
    assert extract_mints(f"https://rugcheck.xyz/tokens/{MINT}") == [MINT]
    assert extract_mints(f"https://example.com/?address={MINT}&ref=1") == [MINT]


def test_extract_pair_addresses_from_links():
    assert extract_mints(f"https://dexscreener.com/solana/{PAIR}") == [PAIR]
    assert extract_mints(f"https://photon-sol.tinyastro.io/en/lp/{PAIR}?r=x") == [PAIR]
    # GeckoTerminal hides the pair behind a network_prefix: "solana_<pair>"
    assert extract_mints(f"https://www.geckoterminal.com/solana/pools/solana_{PAIR}") == [PAIR]


def test_extract_rejects_non_mints():
    assert extract_mints("hello") == []
    assert extract_mints("") == []
    assert extract_mints("a" * 64) == []  # signature shape
    assert extract_mints("1" * 45) == []  # too long
    assert extract_mints("https://t.me/somechannel/12345") == []
    # A base58 suffix extends the run past 44 chars — no match. A
    # non-base58 suffix (like "0") leaves the address run intact.
    assert extract_mints(f"{MINT}a") == []
    assert extract_mints(f"{MINT}0") == [MINT]


def test_extract_mint_between_url_noise():
    # A message with a link AND extra text still yields the mint once.
    text = f"🚀 moonshot: https://pump.fun/{MINT} lfg"
    assert extract_mints(text) == [MINT]


# ---------------------------------------------------------------------------
# resolve_pair_to_mint — pool/pair links map back to the base token
# ---------------------------------------------------------------------------


class FakeHttp:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.urls: list[str] = []

    async def get(self, url, **kwargs):
        self.urls.append(url)
        payload, status = self.payload, self.status_code
        return type("R", (), {"status_code": status, "json": lambda self: payload})()


class FakePrices:
    def __init__(self, http=None):
        self.http = http or FakeHttp({"pairs": []})

    async def usd_prices(self, mints):
        return {mint: 0.5 for mint in mints}

    async def token_profile(self, mint):
        return {"name": "Dogwifhat", "symbol": "WIF"}


class FakeRpc:
    def __init__(self, known):
        self.known = set(known)

    async def get_mint_info(self, address):
        if address in self.known:
            return {"freeze_authority": None, "mint_authority": None}
        raise UserFacingError(f"{address} is not a token")


def _ok_gate():
    from sniper.subscription import GateResult

    return GateResult(True, "", False, [])


class FakeGate:
    async def check(self, *args, **kwargs):
        return _ok_gate()


class FakeDb:
    """settings + user rows; auto-buy is off unless a test switches it on."""

    def __init__(self, settings=None):
        self.settings = settings if settings is not None else {"auto_buy_enabled": 0, "auto_buy_amount_sol": 0.05}
        self.user = {"wallet_address": "W", "trading_paused": 0, "chat_id": "7"}

    async def get_settings(self, telegram_id):
        return self.settings

    async def get_user(self, telegram_id):
        return self.user


class FakeSvc:
    def __init__(self, known=(MINT,), search_payload=None, settings=None):
        self.rpc = FakeRpc(known)
        self.prices = FakePrices(FakeHttp(search_payload or {"pairs": []}))
        self.limiter = type("L", (), {"allow": lambda self, *a, **k: True})()
        self.cfg = type("Cfg", (), {"commands_per_30s": 12})()
        self.gate = FakeGate()
        self.db = FakeDb(settings)
        self.queue = None  # auto-buy tests inject a fake queue here


SEARCH_PAYLOAD = {"pairs": [{"pairAddress": PAIR, "baseToken": {"address": MINT}, "liquidity": {"usd": 5000}}]}


async def test_pair_address_resolves_to_base_mint():
    svc = FakeSvc(search_payload=SEARCH_PAYLOAD)
    mint = await resolve_pair_to_mint(svc, PAIR)
    assert mint == MINT
    assert svc.prices.http.urls == [f"https://api.dexscreener.com/latest/dex/search?q={PAIR}"]


async def test_unknown_address_does_not_resolve():
    assert await resolve_pair_to_mint(FakeSvc(search_payload=SEARCH_PAYLOAD), WALLET) is None
    # pairAddress mismatch is never trusted
    payload = {"pairs": [{"pairAddress": "other", "baseToken": {"address": MINT}}]}
    assert await resolve_pair_to_mint(FakeSvc(search_payload=payload), PAIR) is None


async def test_resolution_survives_service_failures():
    svc = FakeSvc(search_payload={})
    svc.prices.http.status_code = 503
    assert await resolve_pair_to_mint(svc, PAIR) is None
    svc.prices.http = FakeHttp({"pairs": []})
    assert await resolve_pair_to_mint(svc, PAIR) is None


# ---------------------------------------------------------------------------
# detect_from_text — the message-level entry point
# ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, text=None, caption=None):
        self.text = text
        self.caption = caption
        self.sent: list[str] = []
        self.markups: list = []

    async def reply_text(self, text, reply_markup=None, disable_web_page_preview=True, parse_mode=None):
        self.sent.append(text)
        self.markups.append(reply_markup)


def make_update(message):
    return type(
        "Upd",
        (),
        {
            "effective_message": message,
            "message": message,
            "effective_user": type("U", (), {"id": 7})(),
            "effective_chat": type("C", (), {"type": "private"})(),
            "callback_query": None,
        },
    )()


async def test_bare_mint_still_gets_the_panel():
    message = FakeMessage(text=MINT)
    handled = await detect_from_text(make_update(message), None, FakeSvc(), MINT)
    assert handled
    assert "TOKEN DETECTED" in message.sent[0] and MINT in message.sent[0]


async def test_mint_inside_a_message_gets_the_panel():
    message = FakeMessage(text=f"check this one 🚀 {MINT}")
    handled = await detect_from_text(make_update(message), None, FakeSvc(), message.text)
    assert handled
    assert MINT in message.sent[0]


async def test_link_with_mint_gets_the_panel():
    message = FakeMessage(text=f"https://pump.fun/{MINT}")
    handled = await detect_from_text(make_update(message), None, FakeSvc(), message.text)
    assert handled
    assert MINT in message.sent[0]


async def test_dexscreener_pair_link_resolves_to_the_mint():
    message = FakeMessage(text=f"https://dexscreener.com/solana/{PAIR}")
    svc = FakeSvc(known=(MINT,), search_payload=SEARCH_PAYLOAD)
    handled = await detect_from_text(make_update(message), None, svc, message.text)
    assert handled
    assert f"Mint: {MINT}" in message.sent[0]  # the base token, not the pair


async def test_wallet_next_to_mint_does_not_block_detection():
    message = FakeMessage(text=f"my wallet {WALLET}, the token is {MINT}")
    handled = await detect_from_text(make_update(message), None, FakeSvc(), message.text)
    assert handled
    assert f"Mint: {MINT}" in message.sent[0]


async def test_single_unknown_address_gets_not_a_token():
    message = FakeMessage(text=WALLET)
    handled = await detect_from_text(make_update(message), None, FakeSvc(), WALLET)
    assert handled
    assert "not a token mint" in message.sent[0]


async def test_several_unknown_addresses_get_one_notice():
    message = FakeMessage(text=f"{WALLET} {PAIR}")
    handled = await detect_from_text(make_update(message), None, FakeSvc(), message.text)
    assert handled
    assert "None of the 2 addresses" in message.sent[0]


async def test_wsol_alone_is_not_a_detection():
    message = FakeMessage(text=WSOL)
    handled = await detect_from_text(make_update(message), None, FakeSvc(), WSOL)
    assert not handled
    assert message.sent == []


async def test_wsol_next_to_mint_is_skipped():
    message = FakeMessage(text=f"{WSOL} or better {MINT}")
    handled = await detect_from_text(make_update(message), None, FakeSvc(), message.text)
    assert handled
    assert f"Mint: {MINT}" in message.sent[0]


async def test_no_mint_shape_returns_false():
    message = FakeMessage(text="just chatting")
    assert await detect_from_text(make_update(message), None, FakeSvc(), "just chatting") is False
    assert message.sent == []


# ---------------------------------------------------------------------------
# Captions — a photo caption works like a message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caption_with_mint_gets_the_panel(monkeypatch):
    import sniper.app as app_module

    message = FakeMessage(caption=f"💎 {MINT}")
    await app_module._handle_caption(make_update(message), FakeContext())
    assert "TOKEN DETECTED" in message.sent[0] and MINT in message.sent[0]


@pytest.mark.asyncio
async def test_caption_without_a_mint_stays_silent():
    import sniper.app as app_module

    message = FakeMessage(caption="look at this chart")
    await app_module._handle_caption(make_update(message), FakeContext())
    assert message.sent == []


@pytest.mark.asyncio
async def test_caption_feeds_an_active_flow():
    import sniper.app as app_module

    message = FakeMessage(caption="0.25")
    context = FakeContext()
    flows.arm(context, "panel_buy_amount")
    await app_module._handle_caption(make_update(message), context)
    assert flows.active_flow(context) is None  # the caption was consumed as the amount
    assert message.sent  # the flow's follow-up reply went out


class FakeApp:
    def __init__(self):
        self.bot_data = {"svc": FakeSvc()}


class FakeContext:
    def __init__(self):
        self.application = FakeApp()
        self.user_data = {}
        self.bot = None  # the gate fake ignores it
        self.args = []


# ---------------------------------------------------------------------------
# flows.parse_mint — the /buy and /sell steps accept embedded CAs and links
# ---------------------------------------------------------------------------


def test_parse_mint_accepts_embedded_and_linked_cas():
    assert flows.parse_mint(f"ca: {MINT}") == MINT
    assert flows.parse_mint(f"https://pump.fun/{MINT}") == MINT
    assert flows.parse_mint(f"{MINT}") == MINT
    assert flows.parse_mint(MINT) == MINT


def test_parse_mint_still_rejects_plain_text():
    with pytest.raises(flows.StepError):
        flows.parse_mint("hello there")
    with pytest.raises(flows.StepError):
        flows.parse_mint("a" * 60)  # secret-shaped text is never treated as a mint
