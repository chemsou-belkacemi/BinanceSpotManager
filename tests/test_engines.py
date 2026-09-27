"""Tests unitaires — arithmetique, filtres Binance, sizing, positions, SL rules.

Aucun appel reseau : ces tests tournent hors ligne et doivent passer partout.
Usage :
    pytest -q
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from binance_spot_manager.models import (
    Commission,
    Entry,
    EntryStatus,
    OrderType,
    Position,
    PositionStatus,
    PriceMode,
    SLMode,
    SLRuleAfterTP,
    SLStatus,
    SignalSource,
    TPStatus,
    TakeProfit,
)
from binance_spot_manager.position_engine import (
    PositionEngine,
    finish_position,
    recompute_position,
    set_current_price,
)
from binance_spot_manager.symbol_rules import (
    SymbolRules,
    format_decimal,
    parse_symbol_rules,
)
from binance_spot_manager.strategy_engine import (
    EntrySpec,
    SLSpec,
    StrategyEngine,
    StrategySpec,
    TPSpec,
    degressive_split,
    equal_split,
    percent_change,
    progressive_split,
    resolve_entry_price,
    resolve_sl_price,
)
from binance_spot_manager.models import EntryReference, TPReference
from binance_spot_manager.models import CloseReason

pytestmark = pytest.mark.unit


# ==========================================================================
# Fixtures
# ==========================================================================

EXCHANGE_SYMBOL = {
    "symbol": "BTCUSDT",
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "status": "TRADING",
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000", "minPrice": "0.01000000", "maxPrice": "1000000.00000000"},
        {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000", "maxQty": "9000.00000000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000", "maxQty": "100.00000000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
        {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 200},
        {"filterType": "MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders": 5},
    ],
}


@pytest.fixture
def rules() -> SymbolRules:
    return parse_symbol_rules(EXCHANGE_SYMBOL)


# ==========================================================================
# Arrondis et filtres (section 12)
# ==========================================================================


def test_format_decimal_avoid_scientific_notation():
    assert format_decimal(Decimal("0.00001")) == "0.00001"
    assert format_decimal(Decimal("1E-5")) == "0.00001"
    assert format_decimal(Decimal("84000.5000")) == "84000.5"
    assert format_decimal(Decimal("0")) == "0"


def test_round_price_uses_tick_size(rules: SymbolRules):
    assert rules.round_price(84240.567) == Decimal("84240.57")
    assert rules.round_price(84240.567, mode="down") == Decimal("84240.56")
    assert rules.round_price(84240.561, mode="up") == Decimal("84240.57")
    assert rules.price_str(84000) == "84000.01" or rules.price_str(84000) == "84000"


def test_round_qty_always_rounds_down(rules: SymbolRules):
    assert rules.round_qty(0.00297678) == Decimal("0.00297000")
    assert rules.round_qty(0.00001999) == Decimal("0.00001000")
    assert rules.round_qty(0.00000001) == Decimal("0")


def test_round_qty_never_exceeds_requested(rules: SymbolRules):
    for value in ("0.000015", "1.234567", "0.0000009"):
        rounded = rules.round_qty(value)
        assert rounded <= Decimal(value)


def test_check_qty_min_and_step(rules: SymbolRules):
    assert rules.check_qty(0.00000001) != []
    assert rules.check_qty(0.00001001) != []  # non aligne sur stepSize
    assert rules.check_qty(0.00001000) == []


def test_check_notional(rules: SymbolRules):
    # 5 USDT est le minimum : 0.00001 BTC a 84000 = 0.84 USDT
    errors = rules.check_notional(84000, 0.00001)
    assert errors and "minNotional" in errors[0]
    assert rules.check_notional(84000, 0.001) == []


def test_validate_order_combines_errors(rules: SymbolRules):
    errors = rules.validate_order(84240.123, 0.00000001)
    assert len(errors) >= 2


def test_qty_for_quote(rules: SymbolRules):
    qty = rules.qty_for_quote(250, 84000)
    assert qty == Decimal("0.00297000")
    assert qty * Decimal("84000") <= Decimal("250")


def test_min_quote_for_order(rules: SymbolRules):
    minimum = rules.min_quote_for_order(84000)
    # max(minQty * price, minNotional) = max(0.84, 5)
    assert minimum == Decimal("5.00000000")


def test_parse_symbol_rules_reads_market_filters(rules: SymbolRules):
    assert rules.market_step_size == Decimal("0.00001000")
    assert rules.max_num_algo_orders == 5
    assert rules.is_trading
    assert rules.base_asset == "BTC"


# ==========================================================================
# Conversions prix <-> pourcentage (section 29)
# ==========================================================================


def test_percent_change():
    assert percent_change(102, 100) == pytest.approx(2.0)
    assert percent_change(96, 100) == pytest.approx(-4.0)
    assert percent_change(100, 0) == 0.0


def test_resolve_entry_price_fixed_and_percent():
    spec_fixed = EntrySpec(price_mode=PriceMode.FIXED_PRICE, price=82500.0)
    assert resolve_entry_price(
        spec_fixed, current_price=84000.0, reference_prices={}
    ) == 82500.0

    spec_percent = EntrySpec(price_mode=PriceMode.PERCENT, offset_percent=-2.0)
    assert resolve_entry_price(
        spec_percent,
        current_price=84000.0,
        reference_prices={EntryReference.ENTRY_1: 84000.0},
    ) == pytest.approx(82320.0)

    spec_positive = EntrySpec(price_mode=PriceMode.PERCENT, offset_percent=1.0)
    assert resolve_entry_price(
        spec_positive,
        current_price=84000.0,
        reference_prices={EntryReference.ENTRY_1: 84000.0},
    ) == pytest.approx(84840.0)


def test_resolve_entry_price_market_uses_current():
    spec = EntrySpec(order_type=OrderType.MARKET)
    assert resolve_entry_price(spec, current_price=84000.0, reference_prices={}) == 84000.0


def test_resolve_sl_price_modes():
    assert resolve_sl_price(
        SLSpec(mode=SLMode.FIXED_PRICE, value=79500.0),
        average_price=82940.0,
        first_entry_price=84000.0,
        last_entry_price=81000.0,
    ) == 79500.0

    assert resolve_sl_price(
        SLSpec(mode=SLMode.AVERAGE_PERCENT, value=-4.0),
        average_price=82940.0,
        first_entry_price=84000.0,
        last_entry_price=81000.0,
    ) == pytest.approx(82940.0 * 0.96)

    assert resolve_sl_price(
        SLSpec(mode=SLMode.ENTRY1_PERCENT, value=-3.0),
        average_price=82940.0,
        first_entry_price=84000.0,
        last_entry_price=81000.0,
    ) == pytest.approx(84000.0 * 0.97)

    assert resolve_sl_price(
        SLSpec(mode=SLMode.LAST_ENTRY_PERCENT, value=-2.0),
        average_price=82940.0,
        first_entry_price=84000.0,
        last_entry_price=81000.0,
    ) == pytest.approx(81000.0 * 0.98)


# ==========================================================================
# Repartitions de capital (section 30)
# ==========================================================================


def test_equal_split():
    assert equal_split(4) == [25.0, 25.0, 25.0, 25.0]
    assert sum(equal_split(3)) == pytest.approx(100.0)
    assert equal_split(0) == []


def test_progressive_and_degressive_splits():
    ascending = progressive_split(3)
    descending = degressive_split(3)
    assert sum(ascending) == pytest.approx(100.0)
    assert sum(descending) == pytest.approx(100.0)
    assert ascending[0] < ascending[-1]
    assert descending[0] > descending[-1]


# ==========================================================================
# Strategy Engine — plan complet
# ==========================================================================


def make_spec(**overrides) -> StrategySpec:
    base = dict(
        symbol="BTCUSDT",
        current_price=84000.0,
        available_quote=5000.0,
        reserve_percent=20.0,
        capital_mode="FIXED",
        capital_amount=500.0,
        entries=[
            EntrySpec(order_type=OrderType.MARKET, capital_percent=40.0),
            EntrySpec(
                price_mode=PriceMode.PERCENT,
                reference=EntryReference.ENTRY_1,
                offset_percent=-2.0,
                capital_percent=30.0,
            ),
            EntrySpec(
                price_mode=PriceMode.PERCENT,
                reference=EntryReference.ENTRY_1,
                offset_percent=-5.0,
                capital_percent=30.0,
            ),
        ],
        take_profits=[
            TPSpec(target_percent=2.34, sell_percent=20.0, sl_rule_after_hit=SLRuleAfterTP.BREAK_EVEN),
            TPSpec(target_percent=4.12, sell_percent=30.0, sl_rule_after_hit=SLRuleAfterTP.PREVIOUS_TP),
            TPSpec(target_percent=18.0, sell_percent=50.0, sl_rule_after_hit=SLRuleAfterTP.PREVIOUS_TP),
        ],
        stop_loss=SLSpec(mode=SLMode.AVERAGE_PERCENT, value=-4.0),
    )
    base.update(overrides)
    return StrategySpec(**base)


def test_plan_1_entry_1_tp(rules):
    spec = make_spec(
        entries=[EntrySpec(order_type=OrderType.MARKET, capital_percent=100.0)],
        take_profits=[TPSpec(target_percent=3.0, sell_percent=100.0)],
    )
    plan = StrategyEngine(rules).build(spec)
    assert plan.errors == []
    assert len(plan.entries) == 1
    assert plan.estimated_average_price == pytest.approx(84000.0)
    assert plan.take_profits[0].target_price == pytest.approx(84000.0 * 1.03)


def test_plan_3_entries_5_tps(rules):
    spec = make_spec(
        take_profits=[
            TPSpec(target_percent=pct, sell_percent=pct_sell)
            for pct, pct_sell in [(2, 15), (4, 20), (6, 20), (9, 20), (15, 25)]
        ]
    )
    plan = StrategyEngine(rules).build(spec)
    assert plan.errors == []
    assert len(plan.entries) == 3
    assert len(plan.take_profits) == 5
    assert plan.sell_percent_total == pytest.approx(100.0)


def test_plan_entries_reference_previous_entry(rules):
    spec = make_spec(
        entries=[
            EntrySpec(price_mode=PriceMode.FIXED_PRICE, price=84000.0, capital_percent=50.0),
            EntrySpec(
                price_mode=PriceMode.PERCENT,
                reference=EntryReference.PREVIOUS_ENTRY,
                offset_percent=-2.0,
                capital_percent=50.0,
            ),
        ],
        take_profits=[TPSpec(target_percent=5.0, sell_percent=100.0)],
    )
    plan = StrategyEngine(rules).build(spec)
    assert plan.entries[1].price == pytest.approx(84000.0 * 0.98)


def test_plan_rejects_bad_allocation(rules):
    spec = make_spec(
        entries=[
            EntrySpec(order_type=OrderType.MARKET, capital_percent=50.0),
            EntrySpec(
                price_mode=PriceMode.PERCENT,
                reference=EntryReference.ENTRY_1,
                offset_percent=-2.0,
                capital_percent=30.0,
            ),
        ]
    )
    plan = StrategyEngine(rules).build(spec)
    assert any("100 %" in e for e in plan.errors)


def test_plan_rejects_tp_allocation_over_100(rules):
    spec = make_spec(
        take_profits=[
            TPSpec(target_percent=2.0, sell_percent=60.0),
            TPSpec(target_percent=4.0, sell_percent=60.0),
        ]
    )
    plan = StrategyEngine(rules).build(spec)
    assert any("TP" in e and "100 %" in e for e in plan.errors)


def test_plan_rejects_tp_below_average(rules):
    spec = make_spec(take_profits=[TPSpec(target_percent=-5.0, sell_percent=100.0)])
    plan = StrategyEngine(rules).build(spec)
    assert any("prix moyen" in e for e in plan.errors)


def test_plan_rejects_incoherent_sl(rules):
    spec = make_spec(stop_loss=SLSpec(mode=SLMode.AVERAGE_PERCENT, value=+5.0))
    plan = StrategyEngine(rules).build(spec)
    assert any("SL" in e for e in plan.errors)


def test_plan_rejects_capital_above_balance(rules):
    spec = make_spec(capital_amount=9000.0)
    plan = StrategyEngine(rules).build(spec)
    assert any("Capital superieur" in e for e in plan.errors)


def test_plan_scenarios_are_built(rules):
    plan = StrategyEngine(rules).build(make_spec())
    assert plan.errors == []
    assert len(plan.scenarios) == 3
    labels = [s.label for s in plan.scenarios]
    assert labels[0].startswith("A")
    # Le prix moyen du scenario C est plus bas que celui du scenario A (DCA).
    assert plan.scenarios[2].average_price < plan.scenarios[0].average_price
    assert plan.scenarios[2].max_loss < 0


def test_scenario_between_a_and_c(rules):
    plan = StrategyEngine(rules).build(make_spec())
    a, b, c = plan.scenarios
    assert c.average_price <= b.average_price <= a.average_price
    assert c.invested >= b.invested >= a.invested


def test_capital_percent_mode(rules):
    spec = make_spec(
        capital_mode="PERCENT_OF_BALANCE",
        capital_amount=0.0,
        capital_percent=5.0,
    )
    plan = StrategyEngine(rules).build(spec)
    # reserve 20 % de 5000 = 1000, donc 4000 utilisables, 5 % = 200
    assert plan.capital_total == pytest.approx(200.0)


def test_risk_based_capital(rules):
    spec = make_spec(
        capital_mode="RISK_BASED",
        capital_amount=0.0,
        risk_percent=1.0,
        available_quote=10000.0,
        reserve_percent=0.0,
        stop_loss=SLSpec(mode=SLMode.AVERAGE_PERCENT, value=-4.0),
    )
    plan = StrategyEngine(rules).build(spec)
    # risque 100 USDT pour 4 % de distance => 2500 USDT de capital
    assert plan.capital_total == pytest.approx(2500.0)


def test_tp_gain_is_estimated(rules):
    plan = StrategyEngine(rules).build(make_spec())
    for tp in plan.take_profits:
        expected = tp.estimated_qty * (tp.target_price - plan.estimated_average_price)
        assert tp.gain_estimated == pytest.approx(expected)


# ==========================================================================
# Position Engine
# ==========================================================================


def make_position(rules) -> Position:
    plan = StrategyEngine(rules).build(make_spec())
    engine = PositionEngine(rules)
    return engine.from_plan(plan, make_spec())


def test_from_plan_creates_matching_children(rules):
    position = make_position(rules)
    assert position.symbol == "BTCUSDT"
    assert len(position.entries) == 3
    assert len(position.take_profits) == 3
    assert position.status is PositionStatus.PENDING_ENTRIES
    assert position.source_groups and len(position.source_groups) == 1
    assert len(position.source_groups[0].entry_ids) == 3


def test_from_plan_refuses_invalid_plan(rules):
    plan = StrategyEngine(rules).build(make_spec(capital_amount=99999.0))
    with pytest.raises(Exception):
        PositionEngine(rules).from_plan(plan, make_spec(capital_amount=99999.0))


def test_entry_fill_updates_average_and_status(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    entry = position.sorted_entries[0]

    engine.apply_entry_fill(
        position,
        entry.entry_id,
        executed_qty=entry.binance_qty,
        average_price=84000.0,
        quote_spent=entry.binance_qty * 84000.0,
        commissions=[Commission(asset="USDT", amount=1.0)],
    )

    assert entry.status is EntryStatus.FILLED
    assert position.status is PositionStatus.ACTIVE
    assert position.metrics.average_price == pytest.approx(84000.0)
    assert position.metrics.net_qty == pytest.approx(entry.binance_qty)
    assert position.metrics.break_even_with_fees > position.metrics.average_price


def test_partial_fill_is_detected(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    entry = position.sorted_entries[0]
    engine.apply_entry_fill(
        position,
        entry.entry_id,
        executed_qty=entry.binance_qty / 2,
        average_price=84000.0,
        quote_spent=entry.binance_qty / 2 * 84000.0,
    )
    assert entry.status is EntryStatus.PARTIALLY_FILLED


def test_average_price_weighted_by_all_entries(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    for entry in position.sorted_entries:
        engine.apply_entry_fill(
            position,
            entry.entry_id,
            executed_qty=entry.binance_qty,
            average_price=entry.resolved_price,
            quote_spent=entry.binance_qty * entry.resolved_price,
        )

    expected_qty = sum(e.binance_qty for e in position.entries)
    expected_average = (
        sum(e.binance_qty * e.resolved_price for e in position.entries) / expected_qty
    )
    assert position.metrics.average_price == pytest.approx(expected_average)
    assert position.metrics.net_qty == pytest.approx(expected_qty)


def test_add_entries_never_creates_second_position(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    extra = Entry(binance_qty=0.001, resolved_price=80000.0, quote_amount=80.0)
    engine.add_entries(position, [extra])
    assert len(position.entries) == 4
    assert position.entries[-1].sequence_number == 4
    assert len(position.source_groups) == 2


def test_tp_fill_sells_and_keeps_remainder(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    for entry in position.sorted_entries:
        engine.apply_entry_fill(
            position,
            entry.entry_id,
            executed_qty=entry.binance_qty,
            average_price=entry.resolved_price,
            quote_spent=entry.binance_qty * entry.resolved_price,
        )

    total_before = position.metrics.net_qty
    tp = position.sorted_tps[0]
    sell_qty = total_before * tp.sell_percent / 100.0
    engine.apply_tp_fill(
        position,
        tp.tp_id,
        executed_qty=sell_qty,
        average_price=tp.target_price,
        quote_received=sell_qty * tp.target_price,
        commissions=[Commission(asset="USDT", amount=0.1)],
    )

    assert tp.status is TPStatus.EXECUTED
    assert tp.gain_realized > 0
    assert position.metrics.net_qty == pytest.approx(total_before - sell_qty)
    assert position.pnl.realized > 0


def test_all_tps_executed_closes_position(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    for entry in position.sorted_entries:
        engine.apply_entry_fill(
            position,
            entry.entry_id,
            executed_qty=entry.binance_qty,
            average_price=entry.resolved_price,
            quote_spent=entry.binance_qty * entry.resolved_price,
        )
    for tp in position.sorted_tps:
        # Le test vend la totalite du restant a chaque TP. Utiliser le
        # sell_percent de la position de test vendrait un pourcentage du
        # restant (0.006 -> 0.003 -> 0.0015) et ne viderait jamais la
        # position : ce n'est pas ce qu'on veut verifier ici.
        qty = position.metrics.net_qty
        engine.apply_tp_fill(
            position,
            tp.tp_id,
            executed_qty=qty,
            average_price=tp.target_price,
            quote_received=qty * tp.target_price,
        )
    assert position.status is PositionStatus.CLOSED
    assert position.close_reason is CloseReason.ALL_TP_HIT


def test_recompute_is_idempotent(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    entry = position.sorted_entries[0]
    engine.apply_entry_fill(
        position,
        entry.entry_id,
        executed_qty=entry.binance_qty,
        average_price=84000.0,
        quote_spent=entry.binance_qty * 84000.0,
    )
    first = (position.metrics.average_price, position.metrics.net_qty, position.pnl.unrealized)
    recompute_position(position)
    recompute_position(position)
    second = (position.metrics.average_price, position.metrics.net_qty, position.pnl.unrealized)
    assert first == second


def test_unrealized_pnl_tracks_price(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    entry = position.sorted_entries[0]
    engine.apply_entry_fill(
        position,
        entry.entry_id,
        executed_qty=entry.binance_qty,
        average_price=84000.0,
        quote_spent=entry.binance_qty * 84000.0,
    )
    set_current_price(position, 85000.0)
    assert position.pnl.unrealized > 0
    set_current_price(position, 83000.0)
    assert position.pnl.unrealized < 0


# ==========================================================================
# SL evolutif (section 17)
# ==========================================================================


def test_sl_rule_break_even(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    position.metrics.average_price = 83000.0
    assert engine.compute_sl_rule_price(position, "BREAK_EVEN") == pytest.approx(83000.0)


def test_sl_rule_break_even_with_fees(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    position.metrics.average_price = 83000.0
    position.metrics.break_even_with_fees = 83100.0
    assert engine.compute_sl_rule_price(position, "BREAK_EVEN_WITH_FEES") == pytest.approx(83100.0)


def test_sl_rule_previous_tp(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    executed = position.sorted_tps[0]
    executed.status = TPStatus.EXECUTED
    assert engine.compute_sl_rule_price(
        position, "PREVIOUS_TP", after_tp_sequence=2
    ) == pytest.approx(executed.target_price)


def test_sl_rule_custom_percent(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    position.metrics.average_price = 100000.0
    assert engine.compute_sl_rule_price(position, "CUSTOM_PERCENT", 6.0) == pytest.approx(106000.0)


def test_sl_rule_no_change_keeps_price(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    position.stop_loss.resolved_price = 79500.0
    assert engine.compute_sl_rule_price(position, "NO_CHANGE") == pytest.approx(79500.0)


def test_sl_execution_closes_position(rules):
    position = make_position(rules)
    engine = PositionEngine(rules)
    entry = position.sorted_entries[0]
    engine.apply_entry_fill(
        position,
        entry.entry_id,
        executed_qty=entry.binance_qty,
        average_price=84000.0,
        quote_spent=entry.binance_qty * 84000.0,
    )
    engine.apply_sl_fill(
        position,
        executed_qty=position.metrics.net_qty,
        average_price=80640.0,
        quote_received=position.metrics.net_qty * 80640.0,
        commissions=[Commission(asset="USDT", amount=0.5)],
    )
    assert position.status is PositionStatus.CLOSED
    assert position.close_reason is CloseReason.SL_EXECUTED
    assert position.pnl.realized < 0
