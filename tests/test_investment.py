"""Investissement : TP seul ou SL seul, sans second ordre de sortie implicite."""

from types import SimpleNamespace

import pytest

from binance_spot_manager.automation_engine import AutomationEngine
from binance_spot_manager.config import Environment, RunMode, SecurityError, Settings
from binance_spot_manager.execution_engine import ExecutionEngine, OrderResult
from binance_spot_manager.investment_plan import (
    investment_risk_context, make_investment_position, preview_investment,
    preview_simple_buy,
)
from binance_spot_manager.models import (
    Commission, EntryStatus, PositionStatus, SLStatus, TPExecutionPolicy, TPStatus,
)
from binance_spot_manager.position_engine import PositionEngine
from binance_spot_manager.symbol_rules import parse_symbol_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def rules():
    return parse_symbol_rules({
        "symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT",
        "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ],
    })


def _preview(rules, mode):
    return preview_investment(
        rules, current_price=2000, capital=100, available_quote=200,
        reserve_percent=20, exit_mode=mode,
        exit_price=2500 if mode == "TP_ONLY" else 1900,
    )


def _filled_position(rules, mode):
    position = make_investment_position(_preview(rules, mode), current_price=2000)
    entry = position.entries[0]
    PositionEngine(rules).apply_entry_fill(
        position, entry.entry_id, executed_qty=entry.binance_qty,
        average_price=2000, quote_spent=entry.binance_qty * 2000,
        commissions=[Commission(asset="ETH", amount=0.00001)],
    )
    return position


def test_tp_only_has_no_sl_and_counts_capital_at_risk(rules):
    preview = _preview(rules, "TP_ONLY")
    assert preview.eligible
    position = _filled_position(rules, "TP_ONLY")
    assert position.status is PositionStatus.ACTIVE
    assert position.stop_loss.status is SLStatus.NONE
    assert position.stop_loss.resolved_price is None
    assert position.metrics.max_loss_at_sl < -90
    assert position.take_profits[0].execution_policy is TPExecutionPolicy.LIMIT_GTC


def test_sl_only_has_no_tp(rules):
    position = _filled_position(rules, "SL_ONLY")
    assert position.take_profits == []
    assert position.stop_loss.status is SLStatus.PLANNED
    assert position.stop_loss.resolved_price == 1900


def test_preview_rejects_invalid_levels_and_too_small_exit(rules):
    bad = preview_investment(
        rules, current_price=2000, capital=6, available_quote=100,
        reserve_percent=20, exit_mode="SL_ONLY", exit_price=2100,
    )
    assert not bad.eligible
    assert any("sous le prix" in error for error in bad.errors)


def test_simple_buy_uses_actual_quote_asset_and_no_exit(rules):
    preview = preview_simple_buy(
        rules, current_price=2000, capital=100,
        available_quote=200, reserve_percent=20,
    )
    assert preview.eligible
    assert preview.quote_asset == "USDT"
    assert preview.base_asset == "ETH"
    assert preview.estimated_spend < 100


def test_simple_buy_can_spend_btc_quote():
    rules = parse_symbol_rules({
        "symbol": "ETHBTC", "baseAsset": "ETH", "quoteAsset": "BTC",
        "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.000001"},
            {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"},
            {"filterType": "NOTIONAL", "minNotional": "0.00001"},
        ],
    })
    preview = preview_simple_buy(
        rules, current_price=0.03, capital=0.003,
        available_quote=0.01, reserve_percent=20,
    )
    assert preview.eligible
    assert preview.base_asset == "ETH"
    assert preview.quote_asset == "BTC"
    assert preview.estimated_spend < 0.003


def test_usdc_tracked_exit_uses_usdc_balance_and_usdt_risk(rules):
    usdc_rules = parse_symbol_rules({
        "symbol": "ETHUSDC", "baseAsset": "ETH", "quoteAsset": "USDC",
        "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ],
    })
    preview = _preview(usdc_rules, "SL_ONLY")
    assert preview.eligible and preview.quote_asset == "USDC"
    existing = _filled_position(rules, "TP_ONLY")
    prices = {"USDCUSDT": 1.02, "ETHUSDT": 2000, "EURUSDT": 1.2}
    balances = {
        "USDC": {"free": 200, "locked": 0},
        "USDT": {"free": 0, "locked": 0},
        "ETH": {"free": 0.05, "locked": 0},
    }
    plan, snapshot, rate = investment_risk_context(
        preview, capital=100, reserve_percent=20,
        balances=balances, prices=prices, positions=[existing],
    )
    assert rate == pytest.approx(1.02)
    assert plan.capital_total == pytest.approx(102)
    assert plan.capital_reserved == pytest.approx(40.8)
    assert snapshot.quote_balance == pytest.approx(204)
    assert snapshot.total_capital == pytest.approx(304)
    assert snapshot.total_risk_quote == pytest.approx(abs(existing.metrics.max_loss_at_sl))


def test_usdc_tracked_exit_fails_closed_without_fx(rules):
    usdc_rules = parse_symbol_rules({
        "symbol": "ETHUSDC", "baseAsset": "ETH", "quoteAsset": "USDC",
        "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ],
    })
    with pytest.raises(ValueError, match="USDC/USDT"):
        investment_risk_context(
            _preview(usdc_rules, "TP_ONLY"), capital=100,
            reserve_percent=20,
            balances={"USDC": {"free": 200, "locked": 0}},
            prices={}, positions=[],
        )


def test_simple_buy_retry_adopts_existing_after_balance_changes(rules):
    client = SimpleNamespace(
        get_price=lambda symbol: 2000,
        get_free_balance=lambda asset: 200,
    )
    sent = []
    execution = ExecutionEngine(
        client=client, rules_cache=SimpleNamespace(get=lambda symbol, **kwargs: rules),
        settings=Settings(run_mode=RunMode.DEMO_AUTO),
        events=SimpleNamespace(append=lambda *args, **kwargs: None),
    )
    current = {"order": None}
    execution.find_existing_order = lambda symbol, client_id: current["order"]
    execution._create_order_safe = lambda **kwargs: (
        sent.append(kwargs) or OrderResult(success=True, status="FILLED", order_id=9)
    )
    first = execution.place_simple_buy(
        symbol="ETHUSDT", quantity=0.01, client_order_id="same-id"
    )
    current["order"] = {
        "orderId": 9, "clientOrderId": "same-id", "status": "FILLED",
        "executedQty": "0.01", "cummulativeQuoteQty": "20",
    }
    client.get_free_balance = lambda asset: 0
    second = execution.place_simple_buy(
        symbol="ETHUSDT", quantity=0.01, client_order_id="same-id"
    )
    assert first.success and second.success
    assert len(sent) == 1


def test_simple_buy_refuses_live_before_network(rules):
    execution = ExecutionEngine(
        client=SimpleNamespace(get_price=lambda symbol: pytest.fail("appel réseau")),
        rules_cache=SimpleNamespace(get=lambda symbol, **kwargs: rules),
        settings=Settings(environment=Environment.LIVE, run_mode=RunMode.LIVE),
    )
    with pytest.raises(SecurityError):
        execution.place_simple_buy(symbol="ETHUSDT", quantity=0.01, client_order_id="x")


def test_gtc_tp_is_sent_once_without_creating_sl(rules):
    position = _filled_position(rules, "TP_ONLY")
    tp = position.take_profits[0]
    calls = []
    execution = ExecutionEngine(settings=Settings(run_mode=RunMode.DEMO_AUTO))
    execution.rules = lambda symbol: rules
    execution.find_existing_order = lambda symbol, client_id: None
    execution._create_order_safe = lambda **kwargs: (
        calls.append(kwargs) or OrderResult(success=True, status="NEW", order_id=42)
    )
    result = execution.place_tp_limit_gtc(position, tp)
    assert result.success
    assert calls[0]["time_in_force"] == "GTC"
    assert calls[0]["order_type"] == "LIMIT"
    assert calls[0]["quantity"] < position.entries[0].executed_qty
    assert tp.status is TPStatus.SUBMITTED
    assert position.stop_loss.status is SLStatus.NONE


def test_worker_never_places_missing_sl_for_tp_only(rules):
    position = _filled_position(rules, "TP_ONLY")
    sent = []
    execution = SimpleNamespace(
        settings=SimpleNamespace(dry_run=False),
        place_tp_limit_gtc=lambda current, tp: (
            sent.append("tp") or OrderResult(success=True, status="NEW", order_id=42)
        ),
        place_stop_loss=lambda *args, **kwargs: pytest.fail("SL non demandé"),
    )
    engine = AutomationEngine(
        execution=execution, position_engine=PositionEngine(rules),
        rules_cache=SimpleNamespace(get=lambda symbol: rules),
        events=SimpleNamespace(append=lambda *args, **kwargs: None),
    )
    engine.run_cycle(position, 2050)
    assert sent == ["tp"]


def test_no_tp_does_not_close_pending_purchase(rules):
    position = make_investment_position(_preview(rules, "SL_ONLY"), current_price=2000)
    engine = AutomationEngine(
        execution=SimpleNamespace(settings=SimpleNamespace(dry_run=True)),
        position_engine=PositionEngine(rules),
        rules_cache=SimpleNamespace(get=lambda symbol: rules),
        events=SimpleNamespace(append=lambda *args, **kwargs: None),
    )
    engine.run_cycle(position, 2000)
    assert position.status is PositionStatus.PENDING_ENTRIES


def test_worker_places_only_stop_for_sl_only(rules):
    position = _filled_position(rules, "SL_ONLY")
    sent = []

    def place_stop(current, *, stop_price, quantity, attempt, keep_level=False):
        sent.append((stop_price, quantity))
        current.stop_loss.status = SLStatus.ACTIVE
        return OrderResult(success=True, status="NEW", order_id=77)

    execution = SimpleNamespace(
        settings=SimpleNamespace(dry_run=True),
        place_stop_loss=place_stop,
        place_tp_limit_gtc=lambda *args: pytest.fail("TP non demandé"),
    )
    engine = AutomationEngine(
        execution=execution, position_engine=PositionEngine(rules),
        rules_cache=SimpleNamespace(get=lambda symbol: rules),
        events=SimpleNamespace(append=lambda *args, **kwargs: None),
    )
    engine.run_cycle(position, 2000)
    assert len(sent) == 1
    assert sent[0][0] == 1900
    assert position.status is PositionStatus.ACTIVE
