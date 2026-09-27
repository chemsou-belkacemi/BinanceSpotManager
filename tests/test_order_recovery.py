"""Reprise d'ordres apres persistance/redemarrage, sans reseau ni nouvelle vente."""

from types import SimpleNamespace

import pytest

from binance_spot_manager.automation_engine import AutomationEngine, CycleResult
from binance_spot_manager.execution_engine import OrderResult
from binance_spot_manager.models import (
    Commission, Entry, EntryStatus, Position, PositionStatus,
    SLStatus, TakeProfit, TPStatus,
)
from binance_spot_manager.position_engine import PositionEngine, recompute_position
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.reconciliation_engine import ReconciliationEngine, ReconciliationReport


def saved_position(tmp_path):
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.status = PositionStatus.ACTIVE
    position.entries = [Entry(
        status=EntryStatus.FILLED, binance_qty=0.001, executed_qty=0.001,
        average_fill_price=84000, quote_spent=84,
        commissions=[Commission(asset="BTC", amount=0.000001)],
    )]
    position.take_profits = [TakeProfit(
        status=TPStatus.SUBMITTED, client_order_id="lost-tp", sell_percent=100,
        target_price=85000,
    )]
    recompute_position(position)
    store = PositionStore(tmp_path / "positions")
    store.save(position)
    return store, store.load(position.position_id)


def test_reconciliation_recovers_tp_by_client_id_after_restart(tmp_path):
    store, position = saved_position(tmp_path)
    confirmed = OrderResult(
        success=True, order_id=42, client_order_id="lost-tp", status="FILLED",
        executed_qty=0.000999, average_price=85000,
        cummulative_quote_qty=84.915,
        commissions=[Commission(asset="USDT", amount=0.084915)],
    )
    execution = SimpleNamespace(fetch_order_status=lambda *a, **k: confirmed)
    engine = ReconciliationEngine.__new__(ReconciliationEngine)
    engine.execution = execution
    engine.auto_apply_fills = True
    report = ReconciliationReport()

    engine._reconcile_take_profits(position, report, {}, {}, True)
    tp = position.take_profits[0]
    assert tp.status is TPStatus.EXECUTED
    assert tp.order_id == 42
    assert tp.executed_qty == pytest.approx(0.000999)
    assert tp.commission_total("USDT") == pytest.approx(0.084915)

    store.save(position)
    restored = store.load(position.position_id)
    engine._reconcile_take_profits(restored, ReconciliationReport(), {}, {}, True)
    assert restored.take_profits[0].executed_qty == pytest.approx(0.000999)


def test_reconciliation_adopts_open_tp_id_without_new_order(tmp_path):
    _, position = saved_position(tmp_path)
    engine = ReconciliationEngine.__new__(ReconciliationEngine)
    report = ReconciliationReport()
    order = {"orderId": 42, "clientOrderId": "lost-tp", "status": "NEW"}

    engine._reconcile_take_profits(position, report, {"lost-tp": order}, {}, True)

    assert position.take_profits[0].order_id == 42
    assert position.take_profits[0].status is TPStatus.SUBMITTED
    assert report.has_desync  # force la persistance de l'identifiant adopte


def test_uncertain_sl_is_adopted_after_restart_without_second_creation(tmp_path):
    store, position = saved_position(tmp_path)
    position.stop_loss.status = SLStatus.REPLACING
    position.stop_loss.client_order_id = "lost-sl"
    position.stop_loss.resolved_price = 80000
    position.stop_loss.quantity = 0.00099
    store.save(position)
    position = store.load(position.position_id)
    automation = AutomationEngine.__new__(AutomationEngine)
    automation.execution = SimpleNamespace(
        settings=SimpleNamespace(dry_run=False),
        fetch_order_status=lambda *a, **k: OrderResult(
            success=True, order_id=43, client_order_id="lost-sl", status="NEW"
        ),
    )
    automation.position_engine = PositionEngine()
    result = CycleResult()

    automation._check_stop_loss(position, 84000, result)

    assert position.stop_loss.status is SLStatus.ACTIVE
    assert position.stop_loss.order_id == 43
    assert not result.errors


@pytest.mark.parametrize("status, qty, expected", [
    ("PARTIALLY_FILLED", 0.0005, TPStatus.SUBMITTED),
    ("CANCELED", 0.0, TPStatus.CANCELED),
])
def test_recovery_does_not_validate_partial_or_cancelled_tp(tmp_path, status, qty, expected):
    _, position = saved_position(tmp_path)
    engine = ReconciliationEngine.__new__(ReconciliationEngine)
    engine.auto_apply_fills = True
    engine.execution = SimpleNamespace(fetch_order_status=lambda *a, **k: OrderResult(
        success=True, order_id=42, status=status, executed_qty=qty,
        average_price=85000, cummulative_quote_qty=qty * 85000,
    ))
    report = ReconciliationReport()

    engine._reconcile_take_profits(position, report, {}, {}, True)

    assert position.take_profits[0].status is expected
    assert position.take_profits[0].executed_qty == 0
    assert report.has_desync


def test_read_only_recovery_never_changes_position(tmp_path):
    _, position = saved_position(tmp_path)
    engine = ReconciliationEngine.__new__(ReconciliationEngine)
    engine.auto_apply_fills = True
    engine.execution = SimpleNamespace(fetch_order_status=lambda *a, **k: OrderResult(
        success=True, order_id=42, status="FILLED", executed_qty=0.000999,
        average_price=85000, cummulative_quote_qty=84.915,
    ))
    before = position.model_dump()
    report = ReconciliationReport()

    engine._reconcile_take_profits(position, report, {}, {}, False)

    assert position.model_dump() == before
    assert report.findings[0].kind == "FILL_DETECTED"
