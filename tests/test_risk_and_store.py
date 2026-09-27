"""Tests du moteur de risque et de la regle 'une paire = une position'.

Aucun appel reseau.
"""

from __future__ import annotations

import pytest

from binance_spot_manager.models import Entry, Position, PositionStatus, utcnow
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.risk_engine import PortfolioSnapshot, RiskEngine, RiskLimits
from binance_spot_manager.strategy_engine import (
    ResolvedEntry,
    ResolvedSL,
    ResolvedTP,
    StrategyPlan,
)

pytestmark = pytest.mark.unit


def make_plan(capital: float = 500.0, loss: float = -20.0) -> StrategyPlan:
    plan = StrategyPlan(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    plan.capital_total = capital
    plan.capital_allocated = capital
    plan.estimated_total_qty = 0.006
    plan.estimated_average_price = 83000.0
    plan.loss_max_estimated = loss
    plan.stop_loss = ResolvedSL(mode=plan.stop_loss.mode, value=-4.0, price=79500.0, max_loss=loss)
    plan.entries = [
        ResolvedEntry(sequence=1, price=84000.0, capital_percent=100.0, quote_amount=capital, qty=0.006)
    ]
    plan.take_profits = [ResolvedTP(sequence=1, target_price=86000.0, sell_percent=100.0)]
    return plan


def make_open_position(symbol: str = "BTCUSDT", capital: float = 500.0, risk: float = 15.0) -> Position:
    position = Position(symbol=symbol, base_asset="BTC", quote_asset="USDT")
    position.status = PositionStatus.ACTIVE
    position.metrics.capital_committed = capital
    position.metrics.max_loss_at_sl = -risk
    position.entries.append(Entry(sequence_number=1, binance_qty=0.006, executed_qty=0.006))
    return position


# ==========================================================================
# Snapshot
# ==========================================================================


def test_snapshot_ignores_closed_positions():
    open_position = make_open_position("BTCUSDT")
    closed = make_open_position("ETHUSDT")
    closed.status = PositionStatus.CLOSED

    snapshot = RiskEngine.snapshot([open_position, closed], quote_balance=3000.0)
    assert snapshot.open_positions == 1
    assert snapshot.capital_committed == pytest.approx(500.0)
    assert "ETHUSDT" not in snapshot.exposure_by_symbol


def test_snapshot_aggregates_exposure_per_symbol():
    first = make_open_position("BTCUSDT", capital=300.0)
    second = make_open_position("BTCUSDT", capital=200.0)
    snapshot = RiskEngine.snapshot([first, second], quote_balance=1000.0)
    assert snapshot.exposure_by_symbol["BTCUSDT"] == pytest.approx(500.0)


def test_snapshot_converts_usdc_positions_before_aggregating_risk():
    usdt = make_open_position("BTCUSDT", capital=300, risk=15)
    usdc = make_open_position("BTCUSDC", capital=200, risk=10)
    usdc.quote_asset = "USDC"
    snapshot = RiskEngine.snapshot(
        [usdt, usdc], quote_balance=1000,
        quote_rates={"USDT": 1.0, "USDC": 1.02},
    )
    assert snapshot.capital_committed == pytest.approx(504)
    assert snapshot.total_risk_quote == pytest.approx(25.2)
    assert snapshot.exposure_by_symbol["BTCUSDC"] == pytest.approx(204)
    with pytest.raises(ValueError, match="USDC"):
        RiskEngine.snapshot([usdc], 1000, quote_rates={"USDT": 1.0})


# ==========================================================================
# Refus
# ==========================================================================


def test_accepts_reasonable_trade():
    engine = RiskEngine(RiskLimits())
    snapshot = PortfolioSnapshot(quote_balance=10000.0, total_risk_quote=0.0)
    report = engine.evaluate(make_plan(capital=500.0, loss=-20.0), snapshot, symbol="BTCUSDT")
    assert report.accepted
    assert report.planned_risk_percent == pytest.approx(0.2, abs=0.01)


def test_refuses_risk_per_position_too_high():
    engine = RiskEngine(RiskLimits(max_risk_per_position_percent=1.0))
    snapshot = PortfolioSnapshot(quote_balance=1000.0)
    report = engine.evaluate(make_plan(capital=500.0, loss=-50.0), snapshot, symbol="BTCUSDT")
    assert not report.accepted
    assert any("risque portefeuille depasse" in r.lower() for r in report.refusals)


def test_refuses_when_reserve_violated():
    engine = RiskEngine(RiskLimits(max_risk_per_position_percent=50.0))
    snapshot = PortfolioSnapshot(quote_balance=500.0)
    plan = make_plan(capital=400.0, loss=-5.0)
    plan.capital_reserved = 200.0
    report = engine.evaluate(plan, snapshot, symbol="BTCUSDT")
    assert not report.accepted
    assert any("Reserve" in r for r in report.refusals)


def test_refuses_when_max_positions_reached():
    engine = RiskEngine(RiskLimits(max_open_positions=2, max_risk_per_position_percent=50.0))
    snapshot = PortfolioSnapshot(quote_balance=10000.0, open_positions=2)
    report = engine.evaluate(make_plan(capital=100.0, loss=-1.0), snapshot, symbol="BTCUSDT")
    assert not report.accepted
    assert any("maximum" in r.lower() for r in report.refusals)


def test_refuses_when_symbol_exposure_too_high():
    engine = RiskEngine(
        RiskLimits(max_exposure_per_symbol_percent=10.0, max_risk_per_position_percent=50.0)
    )
    snapshot = PortfolioSnapshot(quote_balance=1000.0)
    snapshot.exposure_by_symbol["BTCUSDT"] = 400.0
    report = engine.evaluate(make_plan(capital=500.0, loss=-1.0), snapshot, symbol="BTCUSDT")
    assert not report.accepted
    assert any("Exposition" in r for r in report.refusals)


def test_refuses_total_risk_too_high():
    engine = RiskEngine(
        RiskLimits(max_total_risk_percent=2.0, max_risk_per_position_percent=50.0)
    )
    snapshot = PortfolioSnapshot(quote_balance=1000.0, total_risk_quote=15.0)
    report = engine.evaluate(make_plan(capital=100.0, loss=-10.0), snapshot, symbol="BTCUSDT")
    assert not report.accepted
    assert any("risque total" in r.lower() for r in report.refusals)


def test_invalid_plan_is_refused():
    engine = RiskEngine()
    plan = make_plan()
    plan.errors.append("test")
    report = engine.evaluate(plan, PortfolioSnapshot(quote_balance=1000.0), symbol="BTCUSDT")
    assert not report.accepted


# ==========================================================================
# Regle : une paire = une position active (section 3)
# ==========================================================================


@pytest.fixture
def store(tmp_path) -> PositionStore:
    return PositionStore(tmp_path)


def test_store_roundtrip(store):
    position = make_open_position("BTCUSDT")
    store.save(position)
    loaded = store.load(position.position_id)
    assert loaded is not None
    assert loaded.symbol == "BTCUSDT"
    assert loaded.metrics.capital_committed == pytest.approx(500.0)


def test_find_active_by_symbol(store):
    store.save(make_open_position("BTCUSDT"))
    assert store.find_active_by_symbol("btcusdt") is not None
    assert store.find_active_by_symbol("ETHUSDT") is None


def test_closed_position_does_not_block_new_one(store):
    position = make_open_position("BTCUSDT")
    position.status = PositionStatus.CLOSED
    store.save(position)
    assert store.find_active_by_symbol("BTCUSDT") is None


def test_no_second_active_position_on_same_symbol(store):
    store.save(make_open_position("BTCUSDT"))
    existing = store.find_active_by_symbol("BTCUSDT")
    assert existing is not None
    # Un second enregistrement ne doit pas creer d'entree concurrente :
    assert store.count_active() == 1


def test_active_symbols_set(store):
    store.save(make_open_position("BTCUSDT"))
    store.save(make_open_position("ETHUSDT"))
    assert store.active_symbols() == {"BTCUSDT", "ETHUSDT"}
    assert store.count_active() == 2


def test_find_entry_by_client_order_id(store):
    position = make_open_position("BTCUSDT")
    position.entries[0].client_order_id = "BSM-D-BTC-abc-E1"
    store.save(position)
    found = store.find_entry_by_client_order_id("BSM-D-BTC-abc-E1")
    assert found is not None
    found_position, entry_id = found
    assert found_position.position_id == position.position_id
    assert entry_id == position.entries[0].entry_id


def test_atomic_write_leaves_no_temp_file(tmp_path):
    store = PositionStore(tmp_path)
    store.save(make_open_position("BTCUSDT"))
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_corrupted_file_is_ignored(tmp_path):
    store = PositionStore(tmp_path)
    store.save(make_open_position("BTCUSDT"))
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    positions = store.list_all()
    assert len(positions) == 1
