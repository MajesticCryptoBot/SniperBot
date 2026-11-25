"""Partial-sign Jupiter versioned transactions.

Swap API v2 can return a transaction that still needs a market-maker signature
during /execute. We sign only our key and leave the other slots untouched.
"""

from __future__ import annotations

import base64
import json

import base58
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from sniper.errors import UserFacingError


def parse_keypair(raw: str) -> Keypair:
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("Invalid Solana private key")
    try:
        if text.startswith("[") and text.endswith("]"):
            arr = json.loads(text)
            if not isinstance(arr, list) or not arr or not all(isinstance(item, int) and 0 <= item <= 255 for item in arr):
                raise ValueError("invalid")
            if len(arr) == 64:
                return Keypair.from_bytes(bytes(arr))
            if len(arr) == 32:
                return Keypair.from_seed(bytes(arr))
            raise ValueError("invalid")
        try:
            return Keypair.from_base58_string(text)
        except Exception:
            decoded = base58.b58decode(text)
            if len(decoded) == 64:
                return Keypair.from_bytes(decoded)
            if len(decoded) == 32:
                return Keypair.from_seed(decoded)
            raise ValueError("invalid")
    except ValueError:
        raise ValueError("Invalid Solana private key") from None
    except Exception:
        raise ValueError("Invalid Solana private key") from None


def partial_sign(tx_base64: str, secret_key: bytes) -> str:
    try:
        keypair = Keypair.from_bytes(secret_key)
        raw = base64.b64decode(tx_base64)
        tx = VersionedTransaction.from_bytes(raw)
        message = tx.message
        signature = keypair.sign_message(to_bytes_versioned(message))
        keys = list(message.account_keys)
        try:
            index = keys.index(keypair.pubkey())
        except ValueError as exc:
            raise UserFacingError("The swap transaction does not include the connected wallet as a required signer. No signature was applied.") from exc
        required = int(message.header.num_required_signatures)
        if index >= required:
            raise UserFacingError("The connected wallet is not authorised to sign this swap transaction.")
        signatures = list(tx.signatures)
        if len(signatures) < required:
            signatures.extend([Signature.default()] * (required - len(signatures)))
        signatures = signatures[:required]
        signatures[index] = signature
        signed = VersionedTransaction.populate(message, signatures)
        return base64.b64encode(bytes(signed)).decode()
    except UserFacingError:
        raise
    except Exception as exc:
        raise UserFacingError("The swap could not be signed. No transaction was submitted; request a fresh quote and try again.") from exc
