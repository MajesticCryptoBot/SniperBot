"""Envelope encryption.

Each wallet secret is encrypted with a random data key (AES-GCM). That data
key is wrapped with the master key from MASTER_ENCRYPTION_KEY. The master key
is not generated at startup, so rebooting the PC does not lock wallets.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sniper.errors import ConfigError

ENVELOPE_VERSION = 1


def decode_master_key(value: str) -> bytes:
    text = (value or "").strip()
    if not text:
        raise ConfigError("MASTER_ENCRYPTION_KEY is empty")
    pad = "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(text + pad)
    except Exception as exc:
        raise ConfigError("MASTER_ENCRYPTION_KEY is not valid base64") from exc
    if len(raw) != 32:
        raise ConfigError(
            "MASTER_ENCRYPTION_KEY must decode to exactly 32 bytes. "
            "Generate one with: python tools/generate_master_key.py"
        )
    return raw


class EnvelopeCipher:
    def __init__(self, master_key: bytes):
        if len(master_key) != 32:
            raise ConfigError("master key must be 32 bytes")
        self._master = AESGCM(master_key)

    def encrypt(self, plaintext: bytes) -> dict:
        dek = AESGCM.generate_key(bit_length=256)
        data_nonce = os.urandom(12)
        ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext, None)
        dek_nonce = os.urandom(12)
        wrapped = self._master.encrypt(dek_nonce, dek, None)
        return {
            "v": ENVELOPE_VERSION,
            "dek_nonce": base64.b64encode(dek_nonce).decode(),
            "wrapped_dek": base64.b64encode(wrapped).decode(),
            "data_nonce": base64.b64encode(data_nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }

    def decrypt(self, payload: dict) -> bytes:
        if not isinstance(payload, dict) or payload.get("v") != ENVELOPE_VERSION:
            raise ValueError("unsupported wallet envelope")
        try:
            dek_nonce = base64.b64decode(payload["dek_nonce"])
            wrapped = base64.b64decode(payload["wrapped_dek"])
            data_nonce = base64.b64decode(payload["data_nonce"])
            ciphertext = base64.b64decode(payload["ciphertext"])
            dek = self._master.decrypt(dek_nonce, wrapped, None)
            return AESGCM(dek).decrypt(data_nonce, ciphertext, None)
        except InvalidTag as exc:
            raise ValueError("wallet decryption failed (wrong master key or corrupt payload)") from exc
        except Exception as exc:
            raise ValueError("wallet decryption failed") from exc

    def encrypt_json(self, plaintext: bytes) -> str:
        return json.dumps(self.encrypt(plaintext), separators=(",", ":"))

    def decrypt_json(self, encoded: str) -> bytes:
        return self.decrypt(json.loads(encoded))
