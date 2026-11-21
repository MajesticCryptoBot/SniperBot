"""Tests for the /orders trade-card rendering (cosmetic pass)."""

from sniper.handlers.portfolio import _render_orders, format_trade_row


def _row(**over):
    row = {
        "id": "t1",
        "side": "buy",
        "token_mint": "3xCW9mrUuEUGwAfGYemc83L4Efao5ecKPngKxWoZpump",
        "destination": None,
        "amount_sol": 0.05,
        "signature": None,
        "status": "failed",
        "error": "Quote unavailable: Insufficient funds",
        "source": "autosnipe",
        "realized_pnl_sol": None,
        "created_at": "2026-11-14T17:06:54Z",
    }
    row.update(over)
    return row


def test_failed_auto_snipe_card_is_scannable():
    text = format_trade_row(1, _row())
    lines = text.splitlines()
    assert lines[0] == "01  BUY  |  FAILED  |  💰 0.05 SOL"
    assert "AUTOSNIPE" in lines[1] and "14 Nov 2026 · 17:06 UTC" in lines[1]
    assert lines[2] == "     Token: 3xCW9mrUuEUGwAfGYemc83L4Efao5ecKPngKxWoZpump"
    assert lines[3] == "     Details: Quote unavailable: Insufficient funds"
    assert len(lines) == 4


def test_confirmed_manual_card_shows_signature_and_pnl():
    text = format_trade_row(
        2,
        _row(
            status="confirmed",
            source="manual",
            error=None,
            signature="5x" * 40,
            realized_pnl_sol=0.01,
            amount_sol=0.1,
        ),
    )
    assert "02  BUY  |  CONFIRMED  |  💰 0.1 SOL" in text
    assert "AUTOSNIPE" not in text
    assert "Signature:" in text
    assert "📈 P&L: 💰 +0.01 SOL" in text
    assert "WARNINGS:" not in text


def test_withdraw_card_shows_destination_not_wsol_mint():
    row = _row(
        side="withdraw",
        token_mint="So11111111111111111111111111111111111111112",
        destination="3BnBLuGaZrVwXWr34Yaa2wsyFh1E9SW2YpZQMDhKo5dN",
        source="manual",
    )
    text = format_trade_row(3, row)
    assert "Destination: 3BnBLuGaZrVwXWr34Yaa2wsyFh1E9SW2YpZQMDhKo5dN" in text
    assert "So111" not in text


def test_bad_timestamp_falls_back_to_raw():
    text = format_trade_row(4, _row(created_at="garbage"))
    assert "garbage" in text


def test_render_orders_headers_and_cards():
    text = _render_orders([_row(), _row(), _row()])
    assert text.startswith("🧾") and "TRADE HISTORY" in text and "LATEST 3" in text
    assert "01  " in text and "03  " in text
    assert "Solana explorer" in text


def test_render_orders_hides_overflow_within_telegram_budget():
    rows = [_row(error="x" * 500) for _ in range(10)]
    text = _render_orders(rows)
    assert "older records omitted" in text
    assert len(text) <= 3900
