import pytest

from binance_spot_manager.signal_sizing import (
    SignalSizingPolicy,
    suggest_signal_budget,
    suggest_signal_budget_from_account,
)


def test_fixed_budget_is_capped_by_existing_reserve():
    result = suggest_signal_budget(
        SignalSizingPolicy(mode="FIXED", fixed_budget=100),
        available_quote=80,
        total_capital=1000,
        reserve_percent=20,
    )
    assert result.budget == 64
    assert result.capped_by_reserve


def test_percent_budget_uses_total_portfolio_value():
    result = suggest_signal_budget(
        SignalSizingPolicy(mode="PERCENT", capital_percent=5),
        available_quote=600,
        total_capital=1000,
        reserve_percent=20,
    )
    assert result.budget == 50
    assert result.applied_percent == 5
    assert not result.reduced


def test_adaptive_budget_reduces_below_free_capital_threshold():
    policy = SignalSizingPolicy(
        mode="ADAPTIVE", capital_percent=5,
        low_balance_threshold_percent=30, low_balance_budget_percent=2,
    )
    result = suggest_signal_budget(
        policy, available_quote=299, total_capital=1000, reserve_percent=20,
    )
    assert result.budget == 20
    assert result.free_capital_percent == pytest.approx(29.9)
    assert result.applied_percent == 2
    assert result.reduced


def test_adaptive_budget_keeps_normal_percent_at_exact_threshold():
    policy = SignalSizingPolicy(
        mode="ADAPTIVE", capital_percent=5,
        low_balance_threshold_percent=30, low_balance_budget_percent=2,
    )
    result = suggest_signal_budget(
        policy, available_quote=300, total_capital=1000, reserve_percent=20,
    )
    assert result.budget == 50
    assert not result.reduced


def test_invalid_saved_values_fall_back_to_safe_defaults():
    policy = SignalSizingPolicy.from_mapping({
        "signal_sizing_mode": "unknown",
        "signal_capital_percent": 500,
        "signal_low_balance_budget_percent": "bad",
    })
    assert policy.mode == "ADAPTIVE"
    assert policy.capital_percent == 5
    assert policy.low_balance_budget_percent == 2


def test_account_sizing_values_all_assets_and_uses_quote_free_balance():
    balances = {
        "USDT": {"free": 250, "locked": 0},
        "ETH": {"free": 0.25, "locked": 0},
    }
    prices = {"ETHUSDT": 3000}
    result, missing = suggest_signal_budget_from_account(
        SignalSizingPolicy(mode="ADAPTIVE", capital_percent=5,
                           low_balance_threshold_percent=30,
                           low_balance_budget_percent=2),
        balances=balances,
        prices=prices,
        quote_asset="USDT",
        reserve_percent=20,
    )
    assert missing == ()
    assert result.total_capital == 1000
    assert result.free_capital_percent == 25
    assert result.budget == 20
