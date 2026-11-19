import base64
import os

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from sniper.crypto import EnvelopeCipher
from sniper.db import Database
from sniper.jupiter import ExecuteResult, SwapOrder
from sniper.trade_queue import TradeQueue
from sniper.timeutil import utcnow


class _Cfg:
    is_mainnet = True
    network = "mainnet-beta"
    absolute_max_sol = 100
    max_trades_per_hour = 20
    trade_queue_max = 10
    max_concurrent_swaps = 1

class FakeJupiter:
    def __init__(self, transaction: str):
        self.transaction = transaction
        self.executed = 0

    async def order(self, **kwargs):
        return SwapOrder(
            request_id="req",
            transaction=self.transaction,
            in_amount=int(kwargs["amount"]),
            out_amount=4242,
            other_amount_threshold=4000,
            price_impact_pct=1.5,
            slippage_bps=int(kwargs["slippage_bps"]),
            in_usd=10.0,
            out_usd=9.9,
            route="metis",
        )

    async def execute(self, signed_b64, request_id):
        self.executed += 1
        assert request_id == "req"
        assert signed_b64
        return ExecuteResult(True, "sig-from-jupiter", None, int(1e8), 4242)


class FakeRpc:
    def __init__(self):
        self.sent = []

    async def confirm(self, signature, timeout_s=45):
        return True, None

    async def transaction_fee(self, signature):
        return 5000

    async def get_balance_lamports(self, pubkey):
        return 2_000_000_000

    async def latest_blockhash(self):
        return str(Hash.default())

    async def send_raw(self, raw: bytes):
        self.sent.append(raw)
        return "sig-withdraw"


def _unsigned_tx(keypair: Keypair) -> str:
    dest = Keypair().pubkey()
    ix = transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=dest, lamports=10))
    message = MessageV0.try_compile(keypair.pubkey(), [ix], [], Hash.default())
    blank = VersionedTransaction.populate(message, [Signature.default()])
    return base64.b64encode(bytes(blank)).decode()


@pytest.mark.asyncio
async def test_buy_is_signed_recorded_and_idempotent():
    keypair = Keypair()
    master = os.urandom(32)
    cipher = EnvelopeCipher(master)
    db = Database.connect_sqlite()
    db.apply_schema()
    db.create_user_sync(
        telegram_id="7",
        username="bob",
        chat_id="7",
        wallet_address=str(keypair.pubkey()),
        enc_payload=cipher.encrypt_json(bytes(keypair)),
        defaults={
            "slippage_bps": 100,
            "priority_fee_lamports": 1000,
            "max_sol_per_trade": 1,
            "daily_spend_limit_sol": 2,
            "autosnipe_amount_sol": 0.05,
        },
    )
    jupiter = FakeJupiter(_unsigned_tx(keypair))
    rpc = FakeRpc()
    queue = TradeQueue(_Cfg(), db, cipher, rpc, jupiter, None, {})
    now = utcnow()
    trade_id = "trade-1"
    inserted = await db.insert_trade(
        {
            "id": trade_id,
            "telegram_id": "7",
            "chat_id": "7",
            "side": "buy",
            "token_mint": "Mint111111111111111111111111111111111111111",
            "destination": None,
            "amount_in_raw": None,
            "amount_out_raw": None,
            "amount_sol": 0.1,
            "slippage_bps": None,
            "price_impact_pct": None,
            "signature": None,
            "status": "pending",
            "error": None,
            "source": "manual",
            "idempotency_key": "preview-1",
            "fee_lamports": None,
            "sol_usd": None,
            "realized_pnl_sol": None,
            "request_id": None,
            "claim_token": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    assert inserted
    await queue.process_trade(trade_id)
    assert jupiter.executed == 1
    stored = await db.get_trade(trade_id)
    assert stored["status"] == "confirmed"
    assert stored["signature"] == "sig-from-jupiter"
    assert stored["position_applied"] == 1
    pos = await db.fetchone("SELECT amount_raw, cost_basis_sol FROM positions WHERE telegram_id='7'")
    assert int(pos["amount_raw"]) == 4242
    assert pos["cost_basis_sol"] == pytest.approx(0.1)
    # Second run must not swap again.
    await queue.process_trade(trade_id)
    assert jupiter.executed == 1
    db.close()
