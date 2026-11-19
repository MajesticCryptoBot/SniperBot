import base64
import os

import pytest

from sniper.crypto import EnvelopeCipher, decode_master_key
from sniper.errors import ConfigError
from sniper.redact import redact, set_secrets


def test_roundtrip_and_reboot():
    master = os.urandom(32)
    first = EnvelopeCipher(master)
    secret = os.urandom(64)
    payload = first.encrypt(secret)
    assert payload["v"] == 1
    assert secret not in str(payload).encode()
    # New object, same master key: survives process restart.
    second = EnvelopeCipher(master)
    assert second.decrypt(payload) == secret


def test_wrong_master_key_fails():
    payload = EnvelopeCipher(os.urandom(32)).encrypt(b"wallet-secret")
    with pytest.raises(ValueError):
        EnvelopeCipher(os.urandom(32)).decrypt(payload)


def test_two_encryptions_differ():
    cipher = EnvelopeCipher(os.urandom(32))
    left = cipher.encrypt(b"same-secret")
    right = cipher.encrypt(b"same-secret")
    assert left["ciphertext"] != right["ciphertext"]
    assert left["wrapped_dek"] != right["wrapped_dek"]


def test_master_key_parser():
    raw = os.urandom(32)
    encoded = base64.urlsafe_b64encode(raw).decode()
    assert decode_master_key(encoded) == raw
    with pytest.raises(ConfigError):
        decode_master_key(base64.urlsafe_b64encode(b"short").decode())
    with pytest.raises(ConfigError):
        decode_master_key("")


def test_redact_known_secret():
    set_secrets(["super-secret-token-value"])
    assert redact("token super-secret-token-value leaked") == "token [redacted] leaked"
    set_secrets([])
