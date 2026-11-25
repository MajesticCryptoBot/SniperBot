"""Sell-side bot fee: Jupiter referral fee params, audit ledger, config gating."""

import base64
import os

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from sniper.config import Config, _validate_fee_settings
from sniper.crypto import EnvelopeCipher
from sniper.db import Database
from sniper.errors import ConfigError
from sniper.jupiter import (
    ExecuteResult,
    JupiterClient,
    SwapOrder,
    actual_fee_amount,
    parse_execute,
    parse_order,
)
from sniper.solana_service import WSOL
from sniper.trade_queue import TradeQueue

TOKEN = "Mint111111111111111111111111111111111111111"
ACCOUNT = "ReferAcc1111111111111111111111111111111"

_ORDER_RESPONSE = {
    "requestId": "req-1",
    "transaction": "x",
    "inAmount": "1000000",
    "outAmount": "90000000",
    "otherAmountThreshold": "89000000",
    "feeMint": WSOL,
    "feeBps": 100,
    "platformFee": "900000",
}

_DEFAULTS = {
    "slippage_bps": 100,
    "priority_fee_lamports": 1000,
    "max_sol_per_trade": 1,
    "daily_spend_limit_sol": 2,
    "autosnipe_amount_sol": 0.05,
}


class _Cfg:
    is_mainnet = True
    network = "mainnet-beta"
    absolute_max_sol = 100
    max_trades_per_hour = 20
    trade_queue_max = 10
    max_concurrent_swaps = 1

    def __init__(self, fee_enabled: bool = True):
        self.jupiter_referral_account = ACCOUNT if fee_enabled else ""
        self.bot_fee_bps = 100 if fee_enabled else 0
        self.fee_active = fee_enabled


class FeeJupiter:
    """Quotes like Jupiter with a referral fee: sells pay 1% in SOL."""

    def __init__(self, transaction: str):
        self.transaction = transaction
        self.executed = 0
        self.order_kwargs = []

    async def order(self, **kwargs):
        self.order_kwargs.append(kwargs)
        fee_active = bool(kwargs.get("referral_account") and kwargs.get("referral_fee_bps"))
        if kwargs["input_mint"] == WSOL:  # buy — no fee
            return SwapOrder(
                request_id="req",
                transaction=self.transaction,
                in_amount=int(kwargs["amount"]),
                out_amount=4242,
                other_amount_threshold=4000,
                price_impact_pct=1.0,
                slippage_bps=int(kwargs["slippage_bps"]),
                in_usd=None,
                out_usd=None,
                route="metis",
            )
        return SwapOrder(
            request_id="req",
            transaction=self.transaction,
            in_amount=int(kwargs["amount"]),
            out_amount=90_000_000,  # total incl. fee
            other_amount_threshold=89_000_000,
            price_impact_pct=1.0,
            slippage_bps=int(kwargs["slippage_bps"]),
            in_usd=None,
            out_usd=9.9,
            route="metis",
            fee_mint=WSOL if fee_active else None,
            fee_bps=int(kwargs["referral_fee_bps"]) if fee_active else None,
            fee_amount=900_000 if fee_active else None,
        )

    async def execute(self, signed_b64, request_id):
        self.executed += 1
        # Sell executed: user nets 89_100_000 of the 90_000_000 total — fee 900_000.
        return ExecuteResult(True, "sig-fee", None, None, 89_100_000, None, 90_000_000)


class FakeRpc:
    async def confirm(self, signature, timeout_s=45):
        return True, None

    async def transaction_fee(self, signature):
        return 5000

    async def get_token_accounts(self, pubkey):
        return []

    async def get_balance_lamports(self, pubkey):
        return 2_000_000_000


class RecordingBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((int(chat_id), text))


def _unsigned_tx(keypair: Keypair) -> str:
    dest = Keypair().pubkey()
    ix = transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=dest, lamports=10))
    message = MessageV0.try_compile(keypair.pubkey(), [ix], [], Hash.default())
    blank = VersionedTransaction.populate(message, [Signature.default()])
    return base64.b64encode(bytes(blank)).decode()


async def _insert_sell(db: Database, trade_id: str) -> None:
    inserted = await db.insert_trade(
        {
            "id": trade_id,
            "telegram_id": "7",
            "chat_id": "7",
            "side": "sell",
            "token_mint": TOKEN,
            "destination": None,
            "amount_in_raw": "1000000000",
            "amount_out_raw": None,
            "amount_sol": None,
            "slippage_bps": None,
            "price_impact_pct": None,
            "signature": None,
            "status": "pending",
            "error": None,
            "source": "manual",
            "idempotency_key": f"preview-{trade_id}",
            "fee_lamports": None,
            "sol_usd": None,
            "realized_pnl_sol": None,
            "request_id": None,
            "claim_token": None,
            "priority_fee_lamports": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    assert inserted


async def _insert_buy(db: Database, trade_id: str) -> None:
    inserted = await db.insert_trade(
        {
            "id": trade_id,
            "telegram_id": "7",
            "chat_id": "7",
            "side": "buy",
            "token_mint": TOKEN,
            "destination": None,
            "amount_in_raw": None,
            "amount_out_raw": None,
            "amount_sol": 0.05,
            "slippage_bps": None,
            "price_impact_pct": None,
            "signature": None,
            "status": "pending",
            "error": None,
            "source": "manual",
            "idempotency_key": f"preview-{trade_id}",
            "fee_lamports": None,
            "sol_usd": None,
            "realized_pnl_sol": None,
            "request_id": None,
            "claim_token": None,
            "priority_fee_lamports": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    assert inserted


async def _make_queue(db: Database, cipher, keypair, fee_enabled: bool = True):
    jupiter = FeeJupiter(_unsigned_tx(keypair))
    queue = TradeQueue(_Cfg(fee_enabled), db, cipher, FakeRpc(), jupiter, None, {})
    queue.bot = RecordingBot()
    return queue, jupiter


# --- Config validation -------------------------------------------------------


def test_fee_config_accepts_valid_range():
    _validate_fee_settings(ACCOUNT, 100)  # no error
    _validate_fee_settings(ACCOUNT, 50)
    _validate_fee_settings(ACCOUNT, 255)
    _validate_fee_settings("", 100)  # no account -> disabled, bps ignored
    _validate_fee_settings(ACCOUNT, 0)  # explicit off


@pytest.mark.parametrize("bps", [1, 49, 256, 300])
def test_fee_config_rejects_out_of_range_bps(bps):
    with pytest.raises(ConfigError):
        _validate_fee_settings(ACCOUNT, bps)


@pytest.mark.parametrize("account", ["short", "Z" * 45, "not-base58!!" + "1" * 26, ACCOUNT[:-1] + "0"])
def test_fee_config_rejects_bad_account(account):
    with pytest.raises(ConfigError):
        _validate_fee_settings(account, 100)


def test_fee_disabled_without_referral_account():
    cfg = Config()
    assert cfg.jupiter_referral_account == ""
    assert cfg.fee_active is False


# --- Jupiter response parsing and fee math -----------------------------------


def test_parse_order_extracts_fee_fields():
    order = parse_order(dict(_ORDER_RESPONSE))
    assert order.fee_mint == WSOL
    assert order.fee_bps == 100
    assert order.fee_amount == 900_000


def test_parse_order_fee_fields_optional():
    order = parse_order({"requestId": "r", "transaction": "t", "inAmount": "1", "outAmount": "2"})
    assert order.fee_mint is None
    assert order.fee_bps is None
    assert order.fee_amount is None


def test_parse_execute_extracts_totals():
    result = parse_execute({"status": "success", "signature": "sig", "totalOutputAmount": "90000000", "outputAmountResult": "89100000"})
    assert result.total_output_amount == 90_000_000
    assert result.output_amount == 89_100_000


def test_actual_fee_output_side():
    order = parse_order(dict(_ORDER_RESPONSE))
    result = ExecuteResult(True, "sig", None, None, 89_100_000, None, 90_000_000)
    assert actual_fee_amount(order, result, TOKEN, WSOL) == 900_000


def test_actual_fee_input_side():
    order = SwapOrder("r", "t", 500, 1, None, 1.0, None, None, None, "m", fee_mint=TOKEN, fee_bps=100)
    result = ExecuteResult(True, "sig", None, 450, 1, 500, 1)
    assert actual_fee_amount(order, result, TOKEN, WSOL) == 50


def test_actual_fee_falls_back_to_quote_estimate():
    order = SwapOrder("r", "t", 1, 1, None, 1.0, None, None, None, "m", fee_mint=WSOL, fee_bps=100, fee_amount=900_000)
    result = ExecuteResult(True, "sig", None, None, None)
    assert actual_fee_amount(order, result, TOKEN, WSOL) == 900_000


def test_actual_fee_none_without_fee_info():
    order = SwapOrder("r", "t", 1, 1, None, 1.0, None, None, None, "m")
    result = ExecuteResult(True, "sig", None, None, None)
    assert actual_fee_amount(order, result, TOKEN, WSOL) is None


@pytest.mark.asyncio
async def test_order_params_carry_referral_fee():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=dict(_ORDER_RESPONSE))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JupiterClient("https://api.jup.ag/swap/v2", "key", http)
        order = await client.order(
            input_mint=TOKEN,
            output_mint=WSOL,
            amount=1000000,
            taker="Taker111111111111111111111111111111111111",
            slippage_bps=100,
            referral_account=ACCOUNT,
            referral_fee_bps=100,
        )
    assert captured["referralAccount"] == ACCOUNT
    assert captured["referralFee"] == "100"
    assert order.fee_mint == WSOL


@pytest.mark.asyncio
async def test_order_params_omit_referral_when_absent():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=dict(_ORDER_RESPONSE))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JupiterClient("https://api.jup.ag/swap/v2", "key", http)
        await client.order(
            input_mint=TOKEN,
            output_mint=WSOL,
            amount=1000000,
            taker="Taker111111111111111111111111111111111111",
            slippage_bps=100,
        )
    assert "referralAccount" not in captured
    assert "referralFee" not in captured


# --- Audit ledger ------------------------------------------------------------


@pytest.mark.asyncio
async def test_fee_ledger_insert_and_totals():
    db = Database.connect_sqlite()
    db.apply_schema()
    await db.insert_fee_ledger(
        {"trade_id": "t1", "side": "sell", "token_mint": TOKEN, "fee_mint": WSOL, "fee_bps": 100, "fee_amount_raw": 900_000, "fee_value_sol": 0.0009, "volume_sol": 0.09}
    )
    await db.insert_fee_ledger(
        {"trade_id": "t2", "side": "sell", "token_mint": TOKEN, "fee_mint": WSOL, "fee_bps": 100, "fee_amount_raw": 500_000, "fee_value_sol": 0.0005, "volume_sol": 0.05}
    )
    totals = await db.fee_ledger_totals()
    assert len(totals) == 1
    assert totals[0]["fee_mint"] == WSOL
    assert totals[0]["trades"] == 2
    assert totals[0]["value_sol"] == pytest.approx(0.0014)
    recent = await db.fee_ledger_recent(limit=1)
    assert recent[0]["trade_id"] == "t2"
    db.close()


# --- Full trade flow ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_with_fee_records_ledger_and_sends_fee_params():
    keypair = Keypair()
    cipher = EnvelopeCipher(os.urandom(32))
    db = Database.connect_sqlite()
    db.apply_schema()
    db.create_user_sync(
        telegram_id="7",
        username="bob",
        chat_id="7",
        wallet_address=str(keypair.pubkey()),
        enc_payload=cipher.encrypt_json(bytes(keypair)),
        defaults=_DEFAULTS,
    )
    queue, jupiter = await _make_queue(db, cipher, keypair, fee_enabled=True)
    await _insert_sell(db, "trade-1")

    await queue.process_trade("trade-1")

    sent_kwargs = jupiter.order_kwargs[0]
    assert sent_kwargs["referral_account"] == ACCOUNT
    assert sent_kwargs["referral_fee_bps"] == 100

    stored = await db.get_trade("trade-1")
    assert stored["status"] == "confirmed"

    rows = await db.fee_ledger_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["trade_id"] == "trade-1"
    assert row["side"] == "sell"
    assert row["token_mint"] == TOKEN
    assert row["fee_mint"] == WSOL
    assert int(row["fee_bps"]) == 100
    assert int(row["fee_amount_raw"]) == 900_000
    assert row["fee_value_sol"] == pytest.approx(0.0009)
    assert row["volume_sol"] == pytest.approx(0.0891)
    db.close()


@pytest.mark.asyncio
async def test_buy_with_fee_enabled_charges_no_fee():
    keypair = Keypair()
    cipher = EnvelopeCipher(os.urandom(32))
    db = Database.connect_sqlite()
    db.apply_schema()
    db.create_user_sync(
        telegram_id="7",
        username="bob",
        chat_id="7",
        wallet_address=str(keypair.pubkey()),
        enc_payload=cipher.encrypt_json(bytes(keypair)),
        defaults=_DEFAULTS,
    )
    queue, jupiter = await _make_queue(db, cipher, keypair, fee_enabled=True)
    await _insert_buy(db, "trade-1")

    await queue.process_trade("trade-1")

    sent_kwargs = jupiter.order_kwargs[0]
    assert sent_kwargs["referral_account"] is None
    assert sent_kwargs["referral_fee_bps"] is None
    assert await db.fee_ledger_recent() == []
    db.close()


@pytest.mark.asyncio
async def test_sell_without_fee_configured_runs_fee_free():
    keypair = Keypair()
    cipher = EnvelopeCipher(os.urandom(32))
    db = Database.connect_sqlite()
    db.apply_schema()
    db.create_user_sync(
        telegram_id="7",
        username="bob",
        chat_id="7",
        wallet_address=str(keypair.pubkey()),
        enc_payload=cipher.encrypt_json(bytes(keypair)),
        defaults=_DEFAULTS,
    )
    queue, jupiter = await _make_queue(db, cipher, keypair, fee_enabled=False)
    await _insert_sell(db, "trade-1")

    await queue.process_trade("trade-1")

    sent_kwargs = jupiter.order_kwargs[0]
    assert sent_kwargs["referral_account"] is None
    assert sent_kwargs["referral_fee_bps"] is None
    assert await db.fee_ledger_recent() == []
    stored = await db.get_trade("trade-1")
    assert stored["status"] == "confirmed"
    db.close()
