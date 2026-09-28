"""Reprise des achats partiels depuis les cumuls Binance, sans aucun ordre envoye."""

from types import SimpleNamespace

import pytest

from binance_spot_manager.config import Settings, RunMode
from binance_spot_manager.event_store import EventStore
from binance_spot_manager.execution_engine import normalize_order_response
from binance_spot_manager.models import Entry, EntryStatus, OrderType, Position, PositionStatus, SLStatus
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.reconciliation_engine import ReconciliationEngine


def order(status, quantity):
    return {
        "symbol": "BTCUSDT", "orderId": 42, "clientOrderId": "test-entry", "status": status,
        "executedQty": str(quantity), "cummulativeQuoteQty": str(quantity * 100), "origQty": "1",
        "fills": [{"qty": str(quantity), "price": "100", "commissionAsset": "BTC",
                   "commission": str(quantity * 0.001)}] if quantity else [],
    }


@pytest.fixture
def recovery(tmp_path):
    state = {"order": order("NEW", 0)}
    execution = SimpleNamespace(
        settings=Settings(run_mode=RunMode.DEMO_MANUAL),
        get_open_orders=lambda symbol: [state["order"]] if state["order"]["status"] in {"NEW", "PARTIALLY_FILLED"} else [],
        fetch_order_status=lambda *a, **k: normalize_order_response(state["order"]),
    )
    events = EventStore(tmp_path / "events.jsonl")
    engine = ReconciliationEngine(execution, events=events)
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", status=PositionStatus.PENDING_ENTRIES)
    position.stop_loss.status = SLStatus.NONE
    position.entries = [Entry(order_type=OrderType.LIMIT, status=EntryStatus.SUBMITTED,
                              binance_qty=1, quote_amount=100, client_order_id="test-entry")]
    return engine, state, position, PositionStore(tmp_path / "positions")


def test_partial_purchase_continues_after_reload_without_double_counting(recovery):
    engine, state, position, store = recovery
    for quantity in (0.4, 0.7, 1.0):
        state["order"] = order("FILLED" if quantity == 1 else "PARTIALLY_FILLED", quantity)
        report = engine.reconcile(position)
        assert any(f.kind == "FILL_APPLIED" for f in report.findings)
        assert position.metrics.net_qty == pytest.approx(quantity * 0.999)
        assert position.entries[0].quote_spent == pytest.approx(quantity * 100)
        assert position.entries[0].commission_total("BTC") == pytest.approx(quantity * 0.001)
        store.save(position)
        position = store.load(position.position_id)  # Etat retrouve apres redemarrage.
        repeated = engine.reconcile(position)
        assert not any(f.kind == "FILL_APPLIED" for f in repeated.findings)
        assert position.metrics.net_qty == pytest.approx(quantity * 0.999)
    assert position.entries[0].status is EntryStatus.FILLED
    assert position.entries[0].order_id == 42


@pytest.mark.parametrize("terminal,expected", [("CANCELED", EntryStatus.CANCELED), ("EXPIRED", EntryStatus.EXPIRED)])
def test_partially_filled_then_canceled_keeps_purchase_and_terminal_status(recovery, terminal, expected):
    engine, state, position, _ = recovery
    state["order"] = order("PARTIALLY_FILLED", 0.4)
    engine.reconcile(position)
    state["order"] = order(terminal, 0.4)
    engine.reconcile(position)
    assert position.entries[0].status is expected
    assert not position.entries[0].is_open_on_binance
    assert position.metrics.net_qty == pytest.approx(0.3996)


def test_older_binance_snapshot_does_not_erase_recorded_purchase(recovery):
    engine, state, position, _ = recovery
    state["order"] = order("PARTIALLY_FILLED", 0.7)
    engine.reconcile(position)
    before = position.entries[0].model_dump()
    state["order"] = order("PARTIALLY_FILLED", 0.4)
    report = engine.reconcile(position)
    assert any(f.kind == "STALE_FILL" for f in report.findings)
    assert position.entries[0].model_dump() == before


def test_read_only_partial_recovery_leaves_position_and_events_untouched(recovery):
    engine, state, position, _ = recovery
    state["order"] = order("PARTIALLY_FILLED", 0.4)
    before = position.model_dump_json()
    report = engine.reconcile(position, apply=False)
    assert any(f.kind == "FILL_DETECTED" for f in report.findings)
    assert position.model_dump_json() == before
    assert engine.events.tail() == []


def test_open_order_can_be_adopted_by_client_id_without_new_order(recovery):
    engine, _, position, _ = recovery
    report = engine.reconcile(position)
    assert any(f.kind == "ORDER_ADOPTED" for f in report.findings)
    assert position.entries[0].order_id == 42
    assert position.entries[0].executed_qty == 0


def test_worker_persists_partial_then_completed_purchase_across_restart(recovery):
    from scripts.bot_worker import Worker

    engine, state, position, store = recovery
    store.save(position)
    for status, quantity in (("PARTIALLY_FILLED", 0.4), ("FILLED", 1.0)):
        worker = Worker.__new__(Worker)
        worker.positions = store
        worker.reconciliation = engine
        worker.notifications = SimpleNamespace(desync=lambda *args: None, notify_position_event=lambda *args: None)
        state["order"] = order(status, quantity)
        worker._reconcile(store.list_open())
        restored = store.load(position.position_id)
        assert restored.entries[0].executed_qty == quantity
        assert restored.metrics.net_qty == pytest.approx(quantity * 0.999)
    assert restored.entries[0].status is EntryStatus.FILLED
