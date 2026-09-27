"""Le heartbeat du worker doit refléter les positions réellement suivies."""

from types import SimpleNamespace

import pytest

from binance_spot_manager.execution_engine import OrderResult
from binance_spot_manager.models import (
    Commission, Entry, EntryStatus, OcoExit, Position, PositionStatus,
    SLStatus, SyncStatus, TakeProfit, TPStatus, WorkerState,
)
from binance_spot_manager.position_engine import PositionEngine, recompute_position
from binance_spot_manager.symbol_rules import parse_symbol_rules
from binance_spot_manager.reconciliation_engine import ReconciliationReport
from scripts.bot_worker import Worker

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("count", "expected_state"),
    [(0, WorkerState.IDLE), (1, WorkerState.MONITORING)],
)
def test_loop_reports_monitored_positions(monkeypatch, count, expected_state):
    worker = Worker.__new__(Worker)
    worker.settings = SimpleNamespace(worker_interval=1)
    worker._running = True
    worker._loop = 0
    states = []

    worker._set_state = lambda state, message, **fields: states.append((state, fields))
    worker._stop_requested = lambda: False
    worker._has_open_positions = lambda: count > 0

    def tick():
        worker._running = False
        return count

    worker._tick = tick
    monkeypatch.setattr("scripts.bot_worker.time.sleep", lambda _: None)

    worker._loop_forever()

    assert states[-1][0] is expected_state
    assert states[-1][1]["positions_monitored"] == count
    assert states[-1][1]["loop_count"] == 1


def test_reconciliation_persists_recovered_sync_status():
    worker = Worker.__new__(Worker)
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.sync_status = SyncStatus.DESYNC_DETECTED
    saved = []

    def reconcile(current):
        current.sync_status = SyncStatus.RECONCILED
        return ReconciliationReport(status=SyncStatus.RECONCILED)

    worker.reconciliation = SimpleNamespace(reconcile=reconcile)
    worker.positions = SimpleNamespace(save=saved.append)

    worker._reconcile([position])

    assert saved == [position]
    assert position.sync_status is SyncStatus.RECONCILED


def test_oco_monitor_records_confirmed_tp_without_second_sell():
    rules = parse_symbol_rules({
        "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
        "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
        ],
    })
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.status = PositionStatus.ACTIVE
    position.entries.append(Entry(
        status=EntryStatus.FILLED, executed_qty=0.00035,
        average_fill_price=84000, quote_spent=29.4,
        commissions=[Commission(asset="BTC", amount=0.00000035)],
    ))
    position.take_profits.append(TakeProfit(target_price=85000, sell_percent=100, status=TPStatus.SUBMITTED))
    position.stop_loss.status = SLStatus.ACTIVE
    position.oco_exit = OcoExit(
        order_list_id=1, list_client_order_id="oco", tp_order_id=10,
        sl_order_id=11, quantity=0.00034,
    )
    recompute_position(position)
    orders = {
        10: OrderResult(success=True, order_id=10, status="FILLED", executed_qty=0.00034,
                        cummulative_quote_qty=28.9, average_price=85000),
        11: OrderResult(success=True, order_id=11, status="CANCELED"),
    }
    worker = Worker.__new__(Worker)
    worker.execution = SimpleNamespace(
        fetch_order_status=lambda symbol, order_id: orders[order_id],
        fetch_my_trades=lambda symbol, order_id: [],
    )
    worker.position_engine = PositionEngine(rules)
    worker.rules_cache = SimpleNamespace(get=lambda symbol: rules)
    emitted = []
    worker.events = SimpleNamespace(append=lambda *args, **kwargs: emitted.append((args, kwargs)))
    worker.notifications = SimpleNamespace(
        tp_executed=lambda position, tp: "tp",
        position_finished=lambda position: "finished",
        notify_position_event=lambda position, notice: {},
    )

    worker._monitor_oco(position, 85000)

    assert position.status is PositionStatus.CLOSED
    assert position.take_profits[0].executed_qty == pytest.approx(0.00034)
    assert position.stop_loss.status is SLStatus.CANCELED
    assert position.oco_exit.status == "FILLED"
    assert len(emitted) == 2


def test_oco_monitor_records_confirmed_stop_and_sends_notice():
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.status = PositionStatus.ACTIVE
    position.entries.append(Entry(
        status=EntryStatus.FILLED, executed_qty=0.00035,
        average_fill_price=84000, quote_spent=29.4,
        commissions=[Commission(asset="BTC", amount=0.00000035)],
    ))
    position.take_profits.append(TakeProfit(target_price=85000, sell_percent=100, status=TPStatus.SUBMITTED))
    position.oco_exit = OcoExit(
        order_list_id=1, list_client_order_id="oco", tp_order_id=10,
        sl_order_id=11, quantity=0.00034,
    )
    recompute_position(position)
    orders = {
        10: OrderResult(success=True, order_id=10, status="CANCELED"),
        11: OrderResult(success=True, order_id=11, status="FILLED", executed_qty=0.00034,
                        cummulative_quote_qty=27.5, average_price=80880),
    }
    worker = Worker.__new__(Worker)
    worker.execution = SimpleNamespace(
        fetch_order_status=lambda symbol, order_id: orders[order_id],
        fetch_my_trades=lambda symbol, order_id: [],
    )
    worker.position_engine = PositionEngine()
    notices = []
    events = []
    worker.events = SimpleNamespace(append=lambda *args, **kwargs: events.append(args[0]))
    worker.notifications = SimpleNamespace(
        sl_executed=lambda current: "sl",
        position_finished=lambda current: "finished",
        notify_position_event=lambda current, notice: notices.append(notice),
    )

    worker._monitor_oco(position, 80880)

    assert position.status is PositionStatus.CLOSED
    assert position.stop_loss.status is SLStatus.EXECUTED
    assert notices == ["sl", "finished"]
    assert [event.value for event in events] == ["SL_EXECUTED", "POSITION_FINISHED"]
