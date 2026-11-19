from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN

LAMPORTS = Decimal(1_000_000_000)


class AmountError(ValueError):
    pass


def parse_positive_decimal(raw: str) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise AmountError("Enter a valid numeric amount.") from exc
    if not value.is_finite() or value <= 0:
        raise AmountError("Amount must be greater than zero.")
    return value


def sol_to_lamports(amount_sol: Decimal | float | str) -> int:
    value = amount_sol if isinstance(amount_sol, Decimal) else parse_positive_decimal(str(amount_sol))
    lamports = (value * LAMPORTS).to_integral_value(ROUND_DOWN)
    if lamports <= 0:
        raise AmountError("Amount is below the minimum transferable unit.")
    if lamports > 2**63:
        raise AmountError("Amount exceeds the supported transaction limit.")
    return int(lamports)


def lamports_to_sol(lamports: int) -> float:
    return float(Decimal(lamports) / LAMPORTS)


def ui_to_raw(amount: Decimal | str, decimals: int) -> int:
    value = amount if isinstance(amount, Decimal) else parse_positive_decimal(str(amount))
    if decimals < 0 or decimals > 18:
        raise AmountError("Token precision is invalid; the amount cannot be processed.")
    raw = (value * (Decimal(10) ** decimals)).to_integral_value(ROUND_DOWN)
    if raw <= 0:
        raise AmountError("Token amount is below the minimum transferable unit.")
    return int(raw)


def raw_to_ui(amount_raw: int, decimals: int) -> float:
    if amount_raw < 0:
        return 0.0
    return float(Decimal(amount_raw) / (Decimal(10) ** decimals))


def percent_of_raw(balance_raw: int, percent: Decimal) -> int:
    if percent <= 0 or percent > 100:
        raise AmountError("Percentage must be greater than 0 and no greater than 100%.")
    if balance_raw <= 0:
        raise AmountError("No token balance is available to sell.")
    raw = int((Decimal(balance_raw) * percent / Decimal(100)).to_integral_value(ROUND_DOWN))
    if raw <= 0:
        raise AmountError("The selected percentage is too small for the available token balance.")
    return min(raw, balance_raw)


def parse_sell_spec(token: str, balance_raw: int, decimals: int) -> tuple[int, str]:
    """Return (raw_amount, label). Accepts '1.5' or '50%'."""
    text = token.strip()
    if text.endswith("%"):
        percent = parse_positive_decimal(text[:-1])
        raw = percent_of_raw(balance_raw, percent)
        return raw, f"{percent}%"
    ui = parse_positive_decimal(text)
    raw = ui_to_raw(ui, decimals)
    if raw > balance_raw:
        raise AmountError("Requested sell quantity exceeds the available wallet balance.")
    return raw, f"{ui} tokens"
