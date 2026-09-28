from types import SimpleNamespace
import pytest

from binance_spot_manager.event_store import EventStore
from binance_spot_manager.fee_token import (
    AccountCommission,
    FeeTokenMonitor,
    FeeTokenPolicy,
    assess_bnb_fees,
)


def test_disabled_monitor_never_blocks():
    result = assess_bnb_fees({}, {}, FeeTokenPolicy(enabled=False), quote_asset="USDT", quote_notional=1000)
    assert result.sufficient
    assert not result.enabled


def test_fee_estimate_uses_free_bnb_and_safety_margin():
    policy = FeeTokenPolicy(alert_threshold_usdt=0.5, estimated_fee_percent=0.1, safety_multiplier=1.25)
    result = assess_bnb_fees(
        {"BNB": {"free": 0.002, "locked": 0.1}},
        {"BNBUSDT": 500},
        policy,
        quote_asset="USDT",
        quote_notional=1000,
    )
    assert result.estimated_required_bnb == 0.0025
    assert result.required_bnb == 0.0025
    assert not result.sufficient  # Le BNB bloque ne peut pas payer les frais.
    assert result.free_usdt == 1
    assert result.locked_usdt == 50
    assert result.required_usdt == 1.25


def test_unknown_conversion_is_not_declared_safe():
    result = assess_bnb_fees(
        {"BNB": {"free": 1, "locked": 0}}, {}, FeeTokenPolicy(),
        quote_asset="USDC", quote_notional=100,
    )
    assert not result.conversion_available
    assert not result.sufficient


def test_monitor_alerts_once_then_reports_recovery(tmp_path):
    balance = {"free": 0.001, "locked": 0}
    client = SimpleNamespace(
        get_balances=lambda: {"BNB": dict(balance)},
        get_price=lambda symbol: 500,
    )
    events = EventStore(tmp_path / "events.jsonl")
    monitor = FeeTokenMonitor(
        client, events,
        lambda: {"bnb_fee_monitor_enabled": True, "bnb_fee_alert_threshold_usdt": 5},
        interval_seconds=5,
    )
    monitor.check(force=True)
    monitor.check(force=True)
    balance["free"] = 0.02
    monitor.check(force=True)
    assert [row["event"] for row in events.tail()] == ["FEE_TOKEN_LOW", "FEE_TOKEN_RECOVERED"]
    assert "USDT" in events.tail()[0]["message"]


def test_usdt_threshold_changes_with_market_value_not_bnb_quantity():
    policy = FeeTokenPolicy(alert_threshold_usdt=5)
    balances = {"BNB": {"free": 0.01, "locked": 1}}
    before = assess_bnb_fees(balances, {"BNBUSDT": 600}, policy)
    after = assess_bnb_fees(balances, {"BNBUSDT": 400}, policy)
    assert before.free_usdt == 6 and before.sufficient
    assert after.free_usdt == 4 and after.low


def test_usdc_buy_is_converted_to_bnb_then_usdt():
    check = assess_bnb_fees(
        {"BNB": {"free": 1}}, {"BNBUSDT": 600, "BNBUSDC": 500},
        FeeTokenPolicy(alert_threshold_usdt=0, estimated_fee_percent=0.075),
        quote_asset="USDC", quote_notional=1000,
    )
    assert check.estimated_required_bnb == pytest.approx(0.001875)
    assert check.required_usdt == pytest.approx(1.125)


def test_missing_price_is_unknown_and_monitor_does_not_interrupt_worker(tmp_path):
    check = assess_bnb_fees({"BNB": {"free": 1}}, {}, FeeTokenPolicy())
    assert check.free_usdt is None
    assert not check.sufficient and not check.low
    def offline(symbol):
        raise RuntimeError("offline")
    events = EventStore(tmp_path / "events.jsonl")
    monitor = FeeTokenMonitor(SimpleNamespace(get_balances=lambda: {}, get_price=offline), events, lambda: {})
    assert monitor.check(force=True) is None
    assert monitor.check(force=True) is None
    assert len(events.tail()) == 1


def test_legacy_bnb_value_is_never_interpreted_as_usdt():
    assert FeeTokenPolicy.from_mapping({"bnb_fee_alert_threshold": 0.01}).alert_threshold_usdt == 5
    assert FeeTokenPolicy.from_mapping({"bnb_fee_alert_threshold_usdt": 10}).alert_threshold_usdt == 10


def commission_response():
    return {
        "symbol": "BTCUSDT",
        "standardCommission": {"maker": "0.001", "taker": "0.001", "buyer": "0", "seller": "0"},
        "taxCommission": dict.fromkeys(("maker", "taker", "buyer", "seller"), "0"),
        "specialCommission": dict.fromkeys(("maker", "taker", "buyer", "seller"), "0"),
        "discount": {"enabledForAccount": True, "enabledForSymbol": True,
                     "discountAsset": "BNB", "discount": "0.75"},
    }


def test_binance_discount_is_applied_to_buy_and_sell():
    fees = AccountCommission.from_response(commission_response(), "BTCUSDT")
    for side in ("BUY", "SELL"):
        for liquidity in ("maker", "taker"):
            assert fees.percent(side, liquidity, pay_in_bnb=False) == pytest.approx(0.1)
            assert fees.percent(side, liquidity, pay_in_bnb=True) == pytest.approx(0.075)


def test_side_specific_taxes_and_special_fees_are_not_discounted():
    response = commission_response()
    response["standardCommission"]["buyer"] = "0.0002"
    response["taxCommission"]["taker"] = "0.0001"
    response["specialCommission"]["buyer"] = "0.0003"
    fees = AccountCommission.from_response(response, "BTCUSDT")
    assert fees.percent("BUY", "taker", pay_in_bnb=True) == pytest.approx(0.13)
    assert fees.percent("SELL", "taker", pay_in_bnb=True) == pytest.approx(0.085)


def test_disabled_pair_discount_uses_full_standard_fee():
    response = commission_response()
    response["discount"]["enabledForSymbol"] = False
    fees = AccountCommission.from_response(response, "BTCUSDT")
    assert not fees.bnb_enabled
    assert fees.percent("BUY", "taker", pay_in_bnb=True) == pytest.approx(0.1)


@pytest.mark.parametrize("bad_value", ["NaN", "-0.001", "Infinity"])
def test_invalid_binance_commission_is_rejected(bad_value):
    response = commission_response()
    response["standardCommission"]["taker"] = bad_value
    with pytest.raises(ValueError):
        AccountCommission.from_response(response, "BTCUSDT")
