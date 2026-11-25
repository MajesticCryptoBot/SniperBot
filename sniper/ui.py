"""Shared UI style for the Telegram interface.

Sober desk layout, Telegram HTML only:
- one status mark on a title when it carries meaning
- a short italic line for purpose (lead(), no mark)
- a blockquote for the data card
- an italic footer via info() — the only place the ℹ️ mark appears
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
    """Italic guidance line for the bottom of a panel. Starts with the mark."""
    return f"ℹ️ <i>{esc(text)}</i>"


def lead(text: str) -> str:
    """Italic purpose line under a title. No mark; ℹ️ is footer-only."""
    return f"<i>{esc(text)}</i>"


def sol(amount, *, signed: bool = False) -> str:
    """A SOL amount for panels. Trailing zeros are dropped. Zero is 0.0."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return f"💰 {esc(amount)} SOL"
    if abs(value) < 5e-7:
        text = "0.0"
    else:
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        if "." not in text:
            text += ".0"
    if signed and value > 0:
        text = f"+{text}"
    return f"💰 {text} SOL"


def onoff(enabled) -> str:
    return "ON" if enabled else "OFF"


def dot(enabled) -> str:
    """Green when an automation switch is on, red when it is off."""
    return "🟢" if enabled else "🔴"


def yesno(flag) -> str:
    return "YES" if flag else "NO"


def card(lines: list[str]) -> str:
    """Quoted data card. Lines may already contain HTML."""
    body = "\n".join("" if line is None else str(line) for line in lines).strip("\n")
    return f"<blockquote>{body}</blockquote>"


def _usd_compact(value) -> str | None:
    """Compact USD text: $12.3K / $1.23M / $4,405. None when unusable."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0:  # NaN or negative — no useful meaning here
        return None
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.0f}"


def _price_compact(value) -> str | None:
    """Price text that survives memecoin precision ($0.00000123)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0:
        return None
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.10f}".rstrip("0")


def token_metrics(profile: dict | None) -> list[str]:
    """Compact token summary for trade notifications.

    The TOKEN DETECTED panel fields folded into three lines: name, then
    price and liquidity, then market cap and 24h activity. Empty when there
    is no usable profile, so callers just skip the block.
    """
    if not profile:
        return []
    name = profile.get("name") or "Unknown"
    symbol = profile.get("symbol")
    lines = [f"{esc(name)} ({esc(symbol) if symbol else 'n/a'})"]
    facts = []
    price = _price_compact(profile.get("price_usd"))
    if price:
        facts.append(f"Price: {price}")
    liquidity = _usd_compact(profile.get("liquidity_usd"))
    if liquidity:
        dex = f" on {esc(profile['dex'])}" if profile.get("dex") else ""
        facts.append(f"Liq {liquidity}{dex}")
    if facts:
        lines.append(" · ".join(facts))
    stats = []
    cap = _usd_compact(profile.get("market_cap_usd"))
    if cap:
        stats.append(f"MCap: {cap}")
    volume = _usd_compact(profile.get("volume_24h_usd"))
    change = profile.get("change_24h_pct")
    change_text = None
    if change is not None and change == change:
        sign = "+" if float(change) >= 0 else ""
        change_text = f"{sign}{float(change):.1f}%"
    if volume:
        stats.append(f"24h: {volume}" + (f" ({change_text})" if change_text else ""))
    elif change_text:
        stats.append(f"24h: {change_text}")
    if stats:
        lines.append(" · ".join(stats))
    return lines


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
