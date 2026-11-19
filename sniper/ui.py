"""Shared UI style for the Telegram interface.

Maestro-inspired presentation: emoji-led headers, compact "Label: value"
fields, no decorative rule lines, no em dashes. Every user-facing message
is sent as Telegram HTML, so all dynamic values must pass through esc()
before they are embedded in a string.
"""

from __future__ import annotations

from html import escape


def esc(value) -> str:
    """Escape a dynamic value for Telegram HTML."""
    return escape(str(value), quote=False)


def info(text: str) -> str:
    """Italic footer line, used sparingly at the end of a panel."""
    return f"ℹ️ <i>{esc(text)}</i>"


def onoff(enabled) -> str:
    return "🟢 ON" if enabled else "🔴 OFF"


def yesno(flag) -> str:
    return "✅ YES" if flag else "❌ NO"
