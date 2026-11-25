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
from sniper.db import Database, split_sql
from sniper.jupiter import parse_execute, parse_order
from sniper.paths import SCHEMA_PATH
from sniper.prices import parse_jupiter_prices
from sniper.signing import parse_keypair, partial_sign
from sniper.timeutil import utcnow


def test_schema_and_wallet_survives_new_cipher():
    statements = split_sql(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert any("CREATE TABLE IF NOT EXISTS trades" in item for item in statements)
    db = Database.connect_sqlite()
    try:
        assert db.apply_schema() == len(statements)
        master = os.urandom(32)
        secret = os.urandom(64)
        db.create_user_sync(
            telegram_id="42",
            username="ada",
            chat_id="42",
            wallet_address="wallet",
            enc_payload=EnvelopeCipher(master).encrypt_json(secret),
            defaults={
                "slippage_bps": 500,
                "priority_fee_lamports": 1000,
                "max_sol_per_trade": 0.2,
                "daily_spend_limit_sol": 1,
                "autosnipe_amount_sol": 0.05,
            },
        )
        payload = db.get_secret_payload_sync("42")
        assert EnvelopeCipher(master).decrypt_json(payload) == secret
        assert "enc_payload" not in db.get_user_sync("42")
    finally:
        db.close()


def _trade(now: str, idem: str, trade_id: str = "t1") -> dict:
    return {
        "id": trade_id,
        "telegram_id": "42",
        "chat_id": "42",
        "side": "buy",
        "token_mint": "Mint111111111111111111111111111111111111111",
        "destination": None,
        "amount_in_raw": "100000000",
        "amount_out_raw": "1000",
        "amount_sol": 0.1,
        "slippage_bps": 500,
        "price_impact_pct": 1.2,
        "signature": None,
        "status": "pending",
        "error": None,
        "source": "manual",
        "idempotency_key": idem,
        "fee_lamports": None,
        "sol_usd": 100.0,
        "realized_pnl_sol": None,
        "request_id": None,
        "claim_token": None,
        "created_at": now,
        "updated_at": now,
    }


def test_orders_idempotency_and_positions():
    db = Database.connect_sqlite()
    db.apply_schema()
    cipher = EnvelopeCipher(os.urandom(32))
    db.create_user_sync(
        telegram_id="42",
        username=None,
        chat_id="42",
        wallet_address="wallet",
        enc_payload=cipher.encrypt_json(b"x" * 64),
        defaults={
            "slippage_bps": 500,
            "priority_fee_lamports": 1000,
            "max_sol_per_trade": 1,
            "daily_spend_limit_sol": 2,
            "autosnipe_amount_sol": 0.05,
        },
    )
    now = utcnow()
    assert _insert(db, _trade(now, "idem-1", "t1")) is True
    assert _insert(db, _trade(now, "idem-1", "t2")) is False
    assert db.claim_trade_sync("t1", "claim-a") is True
    assert db.claim_trade_sync("t1", "claim-b") is False
    assert db.finalize_sync("t1", success=True, error=None, fee_lamports=5000) == "confirmed"
    pos = db.fetchone_sync("SELECT * FROM positions WHERE telegram_id='42'")
    assert int(pos["amount_raw"]) == 1000
    assert pos["cost_basis_sol"] == pytest.approx(0.1)
    assert db.finalize_sync("t1", success=True, error=None, fee_lamports=5000) == "confirmed"
    pos = db.fetchone_sync("SELECT * FROM positions WHERE telegram_id='42'")
    assert int(pos["amount_raw"]) == 1000

    sell = _trade(now, "idem-sell", "t3")
    sell["side"] = "sell"
    sell["amount_in_raw"] = "400"
    sell["amount_out_raw"] = "50000000"
    sell["amount_sol"] = 0.05
    sell["status"] = "submitted"
    assert _insert(db, sell) is True
    assert db.finalize_sync("t3", success=True, error=None, fee_lamports=5000) == "confirmed"
    pos = db.fetchone_sync("SELECT * FROM positions WHERE telegram_id='42'")
    assert int(pos["amount_raw"]) == 600
    assert pos["cost_basis_sol"] == pytest.approx(0.06)
    assert pos["realized_pnl_sol"] == pytest.approx(0.01)
    db.close()


def test_finalize_rolls_back_when_position_update_fails():
    db = Database.connect_sqlite()
    db.apply_schema()
    cipher = EnvelopeCipher(os.urandom(32))
    db.create_user_sync(
        telegram_id="42",
        username=None,
        chat_id="42",
        wallet_address="wallet",
        enc_payload=cipher.encrypt_json(b"x" * 64),
        defaults={
            "slippage_bps": 500,
            "priority_fee_lamports": 1000,
            "max_sol_per_trade": 1,
            "daily_spend_limit_sol": 2,
            "autosnipe_amount_sol": 0.05,
        },
    )
    now = utcnow()
    assert _insert(db, _trade(now, "idem-rollback", "t-rb")) is True
    assert db.claim_trade_sync("t-rb", "claim") is True

    def boom(*_args, **_kwargs):
        raise RuntimeError("position write failed")

    db._add_buy_position = boom
    with pytest.raises(RuntimeError):
        db.finalize_sync("t-rb", success=True, error=None, fee_lamports=1)
    assert db.fetchone_sync("SELECT trade_id FROM position_ledger WHERE trade_id='t-rb'") is None
    assert db.fetchone_sync("SELECT token_mint FROM positions") is None
    trade = db.get_trade_sync("t-rb")
    assert trade["status"] == "processing"
    assert int(trade["position_applied"]) == 0
    db.close()


def _insert(db: Database, row: dict) -> bool:
    existing = db.fetchone_sync("SELECT id FROM trades WHERE idempotency_key=?", (row["idempotency_key"],))
    if existing:
        return False
    try:
        db.execute_sync(
            """
            INSERT INTO trades (
                id, telegram_id, chat_id, side, token_mint, destination,
                amount_in_raw, amount_out_raw, amount_sol, slippage_bps,
                price_impact_pct, signature, status, error, source,
                idempotency_key, fee_lamports, sol_usd, realized_pnl_sol,
                request_id, claim_token, position_applied, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, 0, ?, ?
            )
            """,
            (
                row["id"], row["telegram_id"], row["chat_id"], row["side"], row["token_mint"], row["destination"],
                row["amount_in_raw"], row["amount_out_raw"], row["amount_sol"], row["slippage_bps"],
                row["price_impact_pct"], row["signature"], row["status"], row["error"], row["source"],
                row["idempotency_key"], row["fee_lamports"], row["sol_usd"], row["realized_pnl_sol"],
                row["request_id"], row["claim_token"], row["created_at"], row["updated_at"],
            ),
        )
        return True
    except Exception as exc:
        if "unique" in str(exc).lower():
            return False
        raise


def test_prune_trades_keeps_newest_and_grace():
    from datetime import datetime, timedelta, timezone

    db = Database.connect_sqlite()
    db.apply_schema()
    cipher = EnvelopeCipher(os.urandom(32))
    defaults = {
        "slippage_bps": 500,
        "priority_fee_lamports": 1000,
        "max_sol_per_trade": 1,
        "daily_spend_limit_sol": 2,
        "autosnipe_amount_sol": 0.05,
    }
    for user in ("42", "43"):
        db.create_user_sync(
            telegram_id=user,
            username=None,
            chat_id=user,
            wallet_address=f"wallet-{user}",
            enc_payload=cipher.encrypt_json(b"x" * 64),
            defaults=defaults,
        )
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    old = (now - timedelta(hours=72)).strftime(fmt)
    mid = (now - timedelta(hours=10)).strftime(fmt)
    recent = (now - timedelta(hours=1)).strftime(fmt)
    # User 42: 15 trades beyond the grace window, 2 inside it, and 10 very
    # recent ones that form the newest 10.
    for i in range(15):
        assert _insert(db, _trade(old, f"old-{i}", f"old-{i}")) is True
    for i in range(2):
        assert _insert(db, _trade(mid, f"mid-{i}", f"mid-{i}")) is True
    for i in range(10):
        assert _insert(db, _trade(recent, f"rec-{i}", f"rec-{i}")) is True
    # User 43: one old trade — it is still their newest, so it must survive.
    other = _trade(old, "u43-1", "u43-1")
    other["telegram_id"] = "43"
    other["chat_id"] = "43"
    assert _insert(db, other) is True

    removed = db.prune_trades_sync(keep=10, grace_seconds=48 * 3600)
    assert removed == 15
    assert db.fetchone_sync("SELECT COUNT(*) AS n FROM trades WHERE telegram_id='42'")["n"] == 12
    assert db.fetchone_sync("SELECT COUNT(*) AS n FROM trades WHERE id LIKE 'old-%'")["n"] == 0
    assert db.fetchone_sync("SELECT COUNT(*) AS n FROM trades WHERE telegram_id='43'")["n"] == 1
    # Idempotent: a second pass finds nothing left to prune.
    assert db.prune_trades_sync(keep=10, grace_seconds=48 * 3600) == 0
    db.close()


def test_jupiter_and_price_parsers():
    order = parse_order(
        {
            "requestId": "req-1",
            "transaction": "AAAA",
            "inAmount": "1000",
            "outAmount": "5000",
            "otherAmountThreshold": "4900",
            "priceImpactPct": "0.012",
            "slippageBps": 50,
            "router": "metis",
            "routePlan": [{"swapInfo": {"label": "Raydium"}}],
            "inUsdValue": 0.15,
        }
    )
    assert order.in_amount == 1000
    assert order.price_impact_pct == pytest.approx(1.2)
    assert "Raydium" in order.route
    failed = parse_execute({"status": "Failed", "error": "slippage", "code": -1000})
    assert not failed.ok
    assert "slippage" in failed.error
    ok = parse_execute({"status": "Success", "signature": "sig", "outputAmountResult": "9"})
    assert ok.ok and ok.output_amount == 9
    prices = parse_jupiter_prices({"So111": {"usdPrice": 150.5}, "timeTaken": 0.01})
    assert prices["So111"] == 150.5
    prices = parse_jupiter_prices({"data": {"Mint": {"price": "0.25"}}})
    assert prices["Mint"] == 0.25


def test_partial_sign_and_keypair_parse():
    kp = Keypair()
    dest = Keypair().pubkey()
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=dest, lamports=10))
    message = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    signed = VersionedTransaction(message, [kp])
    blank = VersionedTransaction.populate(message, [Signature.default()])
    restored = partial_sign(base64.b64encode(bytes(blank)).decode(), bytes(kp))
    parsed = VersionedTransaction.from_bytes(base64.b64decode(restored))
    assert parsed.signatures[0] == signed.signatures[0]

    phantom = __import__("base58").b58encode(bytes(kp)).decode()
    assert parse_keypair(phantom).pubkey() == kp.pubkey()
    seed = bytes(kp)[:32]
    assert parse_keypair(__import__("base58").b58encode(seed).decode()).pubkey()
    with pytest.raises(ValueError):
        parse_keypair("not-a-key")
