import pytest

from sniper.accounting import apply_buy, apply_sell, portfolio_totals, spend_decision
from sniper.amounts import AmountError, parse_sell_spec, sol_to_lamports
from sniper.settings_logic import validate_setting


def test_portfolio_does_not_mix_sol_and_usd():
    totals = portfolio_totals(2.0, 100.0, [(10.0, 0.5)])
    assert totals["sol"] == 2.0
    assert totals["sol_value_usd"] == 200.0
    assert totals["tokens_usd"] == 5.0
    assert totals["total_usd"] == 205.0


def test_position_math():
    raw, cost = apply_buy(0, 0.0, 1000, 0.2)
    assert (raw, cost) == (1000, 0.2)
    raw, cost = apply_buy(raw, cost, 1000, 0.2)
    assert (raw, cost) == (2000, 0.4)
    raw, cost, pnl = apply_sell(raw, cost, 500, 0.3)
    assert raw == 1500
    assert cost == pytest.approx(0.3)
    assert pnl == pytest.approx(0.2)


def test_spend_limits():
    base = dict(
        amount_sol=0.2,
        max_per_trade=0.1,
        spent_today=0.0,
        daily_limit=1.0,
        trades_last_hour=0,
        max_trades_per_hour=5,
        user_paused=False,
        global_paused=False,
        absolute_max_sol=100,
    )
    ok, reason = spend_decision(**base)
    assert not ok and "per-trade limit" in reason
    base["max_per_trade"] = 0.5
    base["spent_today"] = 0.9
    ok, reason = spend_decision(**base)
    assert not ok and "daily limit" in reason
    base["spent_today"] = 0
    base["user_paused"] = True
    ok, reason = spend_decision(**base)
    assert not ok and "paused" in reason
    base["user_paused"] = False
    ok, reason = spend_decision(**base)
    assert ok and reason == ""


def test_settings_validation():
    column, value = validate_setting("slippage", "5", absolute_max_sol=100)
    assert (column, value) == ("slippage_bps", 500)
    with pytest.raises(AmountError):
        validate_setting("slippage", "90", absolute_max_sol=100)
    column, value = validate_setting("max_sol", "0.25", absolute_max_sol=100)
    assert column == "max_sol_per_trade"
    with pytest.raises(AmountError):
        validate_setting("max_sol", "250", absolute_max_sol=100)
    with pytest.raises(AmountError):
        validate_setting("nope", "1", absolute_max_sol=100)


def test_sell_spec_and_lamports():
    raw, label = parse_sell_spec("50%", 1000, 6)
    assert raw == 500
    assert label == "50%"
    raw, _ = parse_sell_spec("1.5", 2_000_000, 6)
    assert raw == 1_500_000
    assert sol_to_lamports("0.1") == 100_000_000
    with pytest.raises(AmountError):
        parse_sell_spec("2", 1_000_000, 6)
