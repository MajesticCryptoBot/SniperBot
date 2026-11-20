from __future__ import annotations

from sniper.amounts import AmountError, parse_positive_decimal

SETTING_NAMES = (
    "slippage",
    "priority_fee",
    "max_sol",
    "daily_limit",
    "snipe_amount",
    "snipe_impact",
    "impact",
    "auto_buy_amount",
    "take_profit",
    "stop_loss",
)

SETTING_HELP = (
    "<b>KEYS</b>\n"
    "slippage · percent\n"
    "priority_fee · lamports\n"
    "max_sol · SOL per trade\n"
    "daily_limit · SOL per UTC day\n"
    "snipe_amount · autosnipe SOL\n"
    "snipe_impact · autosnipe impact cap %\n"
    "impact · manual impact cap %\n"
    "auto_buy_amount · auto-buy SOL\n"
    "take_profit · exit gain %\n"
    "stop_loss · exit loss %"
)


def validate_setting(name: str, raw: str, *, absolute_max_sol: float) -> tuple[str, float | int]:
    key = name.strip().lower()
    text = raw.strip()
    try:
        if key == "slippage":
            percent = parse_positive_decimal(text)
            bps = int(percent * 100)
            if bps < 10 or bps > 5000:
                raise AmountError("Slippage tolerance must be between 0.10% and 50%.")
            return "slippage_bps", bps
        if key == "priority_fee":
            fee = int(text)
            if fee < 0 or fee > 20_000_000:
                raise AmountError("Priority fee must be between 0 and 20,000,000 lamports.")
            return "priority_fee_lamports", fee
        if key == "max_sol":
            value = float(parse_positive_decimal(text))
            if value > absolute_max_sol:
                raise AmountError(f"Maximum per-trade allocation cannot exceed {{absolute_max_sol}} SOL.")
            return "max_sol_per_trade", value
        if key == "daily_limit":
            value = float(parse_positive_decimal(text))
            if value > absolute_max_sol:
                raise AmountError(f"Daily spend limit cannot exceed {{absolute_max_sol}} SOL.")
            return "daily_spend_limit_sol", value
        if key == "snipe_amount":
            value = float(parse_positive_decimal(text))
            if value > absolute_max_sol:
                raise AmountError(f"Autosnipe allocation cannot exceed {{absolute_max_sol}} SOL.")
            return "autosnipe_amount_sol", value
        if key == "snipe_impact":
            value = float(parse_positive_decimal(text))
            if value > 95:
                raise AmountError("Autosnipe price-impact cap cannot exceed 95%.")
            return "autosnipe_max_price_impact_pct", value
        if key == "impact":
            value = float(parse_positive_decimal(text))
            if value > 95:
                raise AmountError("Manual price-impact cap cannot exceed 95%.")
            return "max_price_impact_pct", value
        if key == "auto_buy_amount":
            value = float(parse_positive_decimal(text))
            if value > absolute_max_sol:
                raise AmountError(f"Auto-buy allocation cannot exceed {{absolute_max_sol}} SOL.")
            return "auto_buy_amount_sol", value
        if key == "take_profit":
            value = float(parse_positive_decimal(text))
            if value > 5000:
                raise AmountError("Take-profit must be between 1% and 5,000%.")
            return "take_profit_pct", value
        if key == "stop_loss":
            value = float(parse_positive_decimal(text))
            if value > 99:
                raise AmountError("Stop-loss must be between 1% and 99%.")
            return "stop_loss_pct", value
    except AmountError:
        raise
    except Exception as exc:
        raise AmountError("Enter a valid numeric value for this setting.") from exc
    raise AmountError("Setting not recognised. Open /config to review the available parameters.")
