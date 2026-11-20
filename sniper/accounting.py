"""Position and portfolio math. Balances shown to users come from RPC; this is cost basis."""

from __future__ import annotations


def apply_buy(amount_raw: int, cost_sol: float, buy_raw: int, buy_sol: float) -> tuple[int, float]:
    if buy_raw < 0 or buy_sol < 0:
        raise ValueError("buy amounts must be non-negative")
    return amount_raw + buy_raw, cost_sol + buy_sol


def apply_sell(
    amount_raw: int,
    cost_sol: float,
    sell_raw: int,
    sol_received: float,
) -> tuple[int, float, float]:
    """Return (new_amount_raw, new_cost_sol, realized_pnl_sol)."""
    if sell_raw < 0 or sol_received < 0:
        raise ValueError("sell amounts must be non-negative")
    if amount_raw <= 0 or sell_raw >= amount_raw:
        removed = cost_sol if amount_raw > 0 else 0.0
        pnl = sol_received - removed
        return 0, 0.0, pnl
    removed = cost_sol * (sell_raw / amount_raw)
    pnl = sol_received - removed
    return amount_raw - sell_raw, max(0.0, cost_sol - removed), pnl


def portfolio_totals(sol_balance: float, sol_usd: float, token_holdings: list[tuple[float, float]]) -> dict:
    """token_holdings is (ui_amount, usd_price). SOL and USD are never added together.

    The old draft did `total = sol_balance + (token_amount * 0.01)` and labeled it dollars.
    """
    sol_value = sol_balance * sol_usd
    tokens_usd = 0.0
    for ui_amount, usd_price in token_holdings:
        tokens_usd += ui_amount * usd_price
    return {
        "sol": sol_balance,
        "sol_usd_price": sol_usd,
        "sol_value_usd": sol_value,
        "tokens_usd": tokens_usd,
        "total_usd": sol_value + tokens_usd,
    }


def spend_decision(
    *,
    amount_sol: float,
    max_per_trade: float,
    spent_today: float,
    daily_limit: float,
    trades_last_hour: int,
    max_trades_per_hour: int,
    user_paused: bool,
    global_paused: bool,
    absolute_max_sol: float,
) -> tuple[bool, str]:
    if global_paused:
        return False, "Trading is paused. Try again later."
    if user_paused:
        return False, "Trading is paused for this account. Use /resume before new requests. Review /autosnipe before you rely on automation."
    if amount_sol <= 0:
        return False, "Amount must be greater than zero."
    if amount_sol > absolute_max_sol:
        return False, f"Amount exceeds the absolute maximum of {absolute_max_sol:g} SOL."
    if amount_sol > max_per_trade + 1e-12:
        return False, f"Requested amount ({amount_sol:.6f} SOL) exceeds the per-trade limit ({max_per_trade:.6f} SOL). Change max per trade in /config, or reduce the amount."
    if spent_today + amount_sol > daily_limit + 1e-9:
        return False, (
            f"This would spend {spent_today + amount_sol:.6f} SOL today, "
            f"above the daily limit of {daily_limit:.6f} SOL (already used: {spent_today:.6f})."
        )
    if trades_last_hour >= max_trades_per_hour:
        return False, f"Hourly trade limit reached ({max_trades_per_hour} per account). Wait before the next trade."
    return True, ""
