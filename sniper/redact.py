"""Redact known secrets. Do not try to guess private keys vs signatures by length."""

from __future__ import annotations

_secrets: list[str] = []


def set_secrets(values: list[str]) -> None:
    _secrets.clear()
    for value in values:
        if value and len(value) >= 8:
            _secrets.append(value)


def redact(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, "[redacted]")
    return text
