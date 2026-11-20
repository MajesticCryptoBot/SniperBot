"""Shared UI style for the Telegram interface.

Sober desk layout, Telegram HTML only:
- one status mark on a title when it carries meaning
- a short italic line for purpose
- a blockquote for the data card
- an italic footer via info()
Every dynamic value must pass through esc() before it is embedded.
"""

from __future__ import annotations

from html import escape


def esc(value) -> str:
    """Escape a dynamic value for Telegram HTML."""
    return escape(str(value), quote=False)


def mono(value) -> str:
    """Code-formatted value. Use for addresses and signatures."""
    return f"<code>{esc(value)}</code>"


def info(text: str) -> str:
    """Italic footer. Kept short; the text is escaped."""
    return f"<i>{esc(text)}</i>"


def onoff(enabled) -> str:
    return "ON" if enabled else "OFF"


def yesno(flag) -> str:
    return "YES" if flag else "NO"


def card(lines: list[str]) -> str:
    """Quoted data card. Lines may already contain HTML."""
    body = "\n".join("" if line is None else str(line) for line in lines).strip("\n")
    return f"<blockquote>{body}</blockquote>"


def desk(title: str, rows: list[str] | None = None, *, lead: str | None = None, footer: str | None = None) -> str:
    """Standard panel: title, optional purpose line, optional card, optional footer."""
    parts = [title]
    if lead:
        parts.append(f"<i>{esc(lead)}</i>")
    if rows:
        parts += ["", card(rows)]
    if footer:
        parts += ["", info(footer)]
    return "\n".join(parts)
