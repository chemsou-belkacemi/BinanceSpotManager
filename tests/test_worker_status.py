"""Le heartbeat du worker doit refléter les positions réellement suivies."""

from types import SimpleNamespace
import time

import pytest

from binance_spot_manager.execution_engine import OrderResult
from binance_spot_manager.binance_client import BinanceError
from binance_spot_manager.models import (
    BotRuntime, Commission, Entry, EntryStatus, OcoExit, Position, PositionStatus,
    SLStatus, SyncStatus, TakeProfit, TPStatus, WorkerState,
)
from binance_spot_manager.position_engine import PositionEngine, recompute_position
from binance_spot_manager.symbol_rules import parse_symbol_rules
from binance_spot_manager.reconciliation_engine import ReconciliationReport
from scripts.bot_worker import Worker
from binance_spot_manager.config import Settings

pytestmark = pytest.mark.unit


def test_storage_error_stays_visible_without_skipping_readable_positions():
    position = Position(symbol="ETHUSDT")
    worker = Worker.__new__(Worker)
    worker.positions = SimpleNamespace(
        list_open=lambda: [position], read_errors=["broken.json : JSON invalide"],
    )
    worker._price_provider = lambda symbols: lambda symbol: None
    processed = []
    worker._process_position = lambda p, price: processed.append(p)
    worker._loop = 1
    with pytest.raises(RuntimeError, match="broken.json"):
        worker._tick()
    assert processed == [position]


def test_inconsistent_oco_emits_one_critical_event_and_never_sells():
    position = Position(symbol="BTCUSDT", oco_exit=OcoExit(
        order_list_id=1, list_client_order_id="oco", tp_order_id=10,
        sl_order_id=11, quantity=0.00035,
    ))
    emitted = []
    worker = Worker.__new__(Worker)
    worker.execution = SimpleNamespace(fetch_order_status=lambda symbol, order_id: OrderResult(
        success=True, order_id=order_id, status="FILLED", executed_qty=0.0001,
    ))  # aucune methode d'ecriture ou de vente disponible
    worker.events = SimpleNamespace(append=lambda *a, **k: emitted.append((a, k)))
    worker._monitor_oco(position, None)
    worker._monitor_oco(position, None)
    assert position.oco_exit.status == "ERROR"
    assert position.sync_status is SyncStatus.DESYNC_DETECTED
    assert len(emitted) == 1
    assert emitted[0][1]["level"] == "CRITICAL"
    assert emitted[0][1]["tp_order_id"] == 10
    assert emitted[0][1]["sl_order_id"] == 11


def test_missing_oco_branch_is_rechecked_when_connection_recovers():
    position = Position(symbol="BTCUSDT", oco_exit=OcoExit(
        order_list_id=1, list_client_order_id="oco", tp_order_id=10,
        sl_order_id=11, quantity=0.00035,
    ))
    available = [False]
    queried = []

    def fetch(symbol, order_id):
        queried.append(order_id)
        if order_id == 10 and not available[0]:
            return None
        return OrderResult(success=True, order_id=order_id, status="NEW")

    worker = Worker.__new__(Worker)
    worker.execution = SimpleNamespace(fetch_order_status=fetch)
    emitted = []
    worker.events = SimpleNamespace(append=lambda *a, **k: emitted.append((a, k)))
    worker._monitor_oco(position, None)
    assert position.oco_exit.status == "ACTIVE"
    assert position.sync_status is SyncStatus.DESYNC_DETECTED
    # Le marqueur survit a une sauvegarde/relecture et donc a un redemarrage.
    position = Position.model_validate_json(position.model_dump_json())
    worker._monitor_oco(position, None)
    assert len(emitted) == 1
    assert position.oco_exit.missing_branch_alerted
    available[0] = True
    worker._monitor_oco(position, None)
    assert queried == [10, 11, 10, 11, 10, 11]
    assert len(emitted) == 2
    assert emitted[-1][1]["level"] == "INFO"
    assert not position.oco_exit.missing_branch_alerted
    # Le retour des lectures ne prouve pas que tous les ecarts ont ete resolus.
    assert position.sync_status is SyncStatus.DESYNC_DETECTED
    available[0] = False
    worker._monitor_oco(position, None)
    assert len(emitted) == 3
    assert emitted[-1][1]["level"] == "ERROR"


def test_old_oco_without_alert_marker_remains_compatible():
    oco = OcoExit.model_validate({
        "order_list_id": 1, "list_client_order_id": "oco", "tp_order_id": 10,
        "sl_order_id": 11, "quantity": 0.00035,
    })
    assert not oco.missing_branch_alerted


@pytest.mark.parametrize("error", [BinanceError("offline"), ValueError("invalid position")])
def test_position_failure_does_not_skip_other_positions_or_hide_error(error):
    from scripts.bot_worker import RECONCILE_EVERY

    failed = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    healthy = Position(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT")
    worker = Worker.__new__(Worker)
    worker.positions = SimpleNamespace(list_open=lambda: [failed, healthy])
    worker._price_provider = lambda symbols: lambda symbol: 100.0
    visited = []
    reconciled = []
    balances = []

    def process(position, price):
        visited.append(position)
        if position is failed:
            raise error

    worker._process_position = process
    worker._reconcile = lambda positions: reconciled.extend(positions)
    worker._sync_quote_balance = lambda: balances.append(True)
    worker._loop = RECONCILE_EVERY
    with pytest.raises(RuntimeError, match="BTCUSDT") as captured:
        worker._tick()
    assert str(error) in str(captured.value)
    assert visited == [failed, healthy]
    assert reconciled == [healthy]
    assert balances == [True]


def test_tick_checks_oco_without_price_but_skips_local_automation():
    oco_position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    oco_position.oco_exit = OcoExit(
        order_list_id=1, list_client_order_id="oco", tp_order_id=10,
        sl_order_id=11, quantity=0.00035,
    )
    oco_position.metrics.current_price = 84000
    local_position = Position(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT")
    saved = []
    monitored = []
    worker = Worker.__new__(Worker)
    worker.positions = SimpleNamespace(
        list_open=lambda: [oco_position, local_position], save=saved.append,
    )
    worker._price_provider = lambda symbols: lambda symbol: None
    worker._monitor_oco = lambda position, price: monitored.append((position, price))
    worker.automation = SimpleNamespace(
        run_cycle=lambda *a: pytest.fail("Local automation requires a fresh price"),
    )
    worker._loop = 1
    assert worker._tick() == 2
    assert monitored == [(oco_position, None)]
    assert saved == [oco_position]
    assert oco_position.metrics.current_price == 84000


def test_oco_without_price_keeps_last_valuation_and_checks_branches():
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.oco_exit = OcoExit(
        order_list_id=1, list_client_order_id="oco", tp_order_id=10,
        sl_order_id=11, quantity=0.00035,
    )
    position.metrics.current_price = 84000
    previous_metrics = position.metrics.model_dump()
    queried = []
    worker = Worker.__new__(Worker)
    worker.execution = SimpleNamespace(
        fetch_order_status=lambda symbol, order_id: queried.append(order_id) or
        OrderResult(success=True, order_id=order_id, status="NEW"),
    )
    worker._monitor_oco(position, None)
    assert queried == [10, 11]
    assert position.metrics.model_dump() == previous_metrics
    assert position.oco_exit.status == "ACTIVE"


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


@pytest.mark.parametrize(
    ("saved", "expected"),
    [({"worker_interval": 1}, 1), ({"worker_interval": 2}, 2),
     ({"worker_interval": "invalid"}, 5), ({}, 5),
     ({"worker_interval": 0}, 1), ({"worker_interval": 999}, 300)],
)
def test_worker_interval_uses_saved_preference(monkeypatch, saved, expected):
    worker = Worker.__new__(Worker)
    worker.settings = SimpleNamespace(worker_interval=5)
    monkeypatch.setattr(
        "scripts.bot_worker.get_settings_store",
        lambda: SimpleNamespace(load=lambda: saved),
    )

    assert worker._worker_interval() == expected


def test_worker_uses_stream_price_before_rest():
    worker = Worker.__new__(Worker)
    worker.market_prices = SimpleNamespace(prices=lambda symbols: {"BTCUSDT": 85000.0})
    worker.client = SimpleNamespace(
        get_prices=lambda symbols: pytest.fail("REST should not be called")
    )
    worker._price_cache = {}
    worker._price_fetched_at = 0.0

    assert worker._price_provider(["BTCUSDT"])("BTCUSDT") == 85000.0
    assert worker._price_sources == {"BTCUSDT": "WebSocket"}


def test_worker_never_trades_on_stale_rest_price_after_failure():
    worker = Worker.__new__(Worker)
    worker.market_prices = SimpleNamespace(prices=lambda symbols: {})
    worker.client = SimpleNamespace(
        get_prices=lambda symbols: (_ for _ in ()).throw(BinanceError("offline"))
    )
    worker._price_cache = {"BTCUSDT": 84000.0}
    worker._price_fetched_at = time.time() - 10
    worker._worker_interval = lambda: 1

    assert worker._price_provider(["BTCUSDT"])("BTCUSDT") is None
    assert worker._price_sources == {"BTCUSDT": "Indisponible"}


def test_worker_persists_read_only_market_diagnostic():
    worker = Worker.__new__(Worker)
    worker.settings = Settings()
    saved = []
    worker.runtime_store = SimpleNamespace(load=BotRuntime, save=saved.append)
    worker.market_prices = SimpleNamespace(snapshot=lambda: {"state": "CONNECTED"})
    worker._price_sources = {"BTCUSDT": "REST"}
    worker._set_state(WorkerState.MONITORING)
    assert saved[0].price_diagnostics == {
        "state": "CONNECTED", "sources": {"BTCUSDT": "REST"},
    }


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


@pytest.mark.parametrize("price", [85000, None])
@pytest.mark.parametrize("initial_partial", [False, True])
def test_oco_monitor_records_confirmed_tp_without_second_sell(price, initial_partial):
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
        position_finished=lambda position, **k: "finished",
        notify_position_event=lambda position, notice: {},
    )

    if initial_partial:
        final_order = orders[10]
        orders[10] = OrderResult(
            success=True, order_id=10, status="PARTIALLY_FILLED", executed_qty=0.0001,
        )
        worker._monitor_oco(position, price)
        assert position.oco_exit.status == "PARTIAL"
        assert position.take_profits[0].executed_qty == 0
        orders[10] = final_order
        emitted.clear()
    worker._monitor_oco(position, price)
    worker._monitor_oco(position, price)  # aucun fill ni notification en double

    assert position.status is PositionStatus.CLOSED
    assert position.take_profits[0].executed_qty == pytest.approx(0.00034)
    assert position.stop_loss.status is SLStatus.CANCELED
    assert position.oco_exit.status == "FILLED"
    assert len(emitted) == 2


@pytest.mark.parametrize("price", [80880, None])
def test_oco_monitor_records_confirmed_stop_and_sends_notice(price):
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
        position_finished=lambda current, **k: "finished",
        notify_position_event=lambda current, notice: notices.append(notice),
    )

    worker._monitor_oco(position, price)

    assert position.status is PositionStatus.CLOSED
    assert position.stop_loss.status is SLStatus.EXECUTED
    assert notices == ["sl", "finished"]
    assert [event.value for event in events] == ["SL_EXECUTED", "POSITION_FINISHED"]


@pytest.mark.parametrize("tp_status, tp_qty, expected", [
    ("PARTIALLY_FILLED", 0.0001, "PARTIAL"),
    ("CANCELED", 0.0, "FAILED"),
])
def test_oco_partial_or_manual_cancellation_never_sends_second_sell(tp_status, tp_qty, expected):
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.status = PositionStatus.ACTIVE
    position.entries = [Entry(
        status=EntryStatus.FILLED, executed_qty=0.00035,
        average_fill_price=84000, quote_spent=29.4,
    )]
    position.take_profits = [TakeProfit(
        target_price=85000, sell_percent=100, status=TPStatus.SUBMITTED,
    )]
    position.oco_exit = OcoExit(
        order_list_id=1, list_client_order_id="oco", tp_order_id=10,
        sl_order_id=11, quantity=0.00035,
    )
    worker = Worker.__new__(Worker)
    orders = {
        10: OrderResult(success=True, order_id=10, status=tp_status, executed_qty=tp_qty),
        11: OrderResult(success=True, order_id=11, status="CANCELED"),
    }
    worker.execution = SimpleNamespace(
        fetch_order_status=lambda symbol, order_id: orders[order_id],
    )  # aucune methode permettant une nouvelle vente
    emitted = []
    worker.events = SimpleNamespace(append=lambda *a, **k: emitted.append((a, k)))

    worker._monitor_oco(position, 85000)
    worker._monitor_oco(position, 85000)

    assert position.oco_exit.status == expected
    assert position.sync_status is SyncStatus.DESYNC_DETECTED
    assert position.take_profits[0].executed_qty == 0
    assert len(emitted) == 1
    assert emitted[0][1]["level"] == "CRITICAL"


def test_tp_notification_is_sent_even_when_cycle_also_reports_error():
    from binance_spot_manager.automation_engine import CycleResult

    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.status = PositionStatus.ACTIVE
    position.take_profits.append(TakeProfit(sequence_number=1, target_price=85000))
    sent = []
    worker = Worker.__new__(Worker)
    worker.automation = SimpleNamespace(run_cycle=lambda p, price: CycleResult(
        tp_executed=1, errors=["SL non restaure apres TP"],
    ))
    worker.positions = SimpleNamespace(save=lambda p: None)
    worker.notifications = SimpleNamespace(
        tp_executed=lambda position, tp: "tp",
        notify_position_event=lambda position, notice: sent.append(notice),
    )

    with pytest.raises(RuntimeError, match="SL non restaure"):
        worker._process_position(position, 85000)

    assert sent == ["tp"]


def _crossed_stop_worker(monkeypatch, *, price=80000.0, saved=None):
    from binance_spot_manager.models import EventType

    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.status = PositionStatus.ACTIVE
    worker = Worker.__new__(Worker)
    events, sent, saved_positions, closes = [], [], [], []

    def get_price(symbol):
        if isinstance(price, Exception):
            raise price
        return price

    worker.client = SimpleNamespace(get_price=get_price)
    worker.execution = SimpleNamespace()
    worker.positions = SimpleNamespace(save=saved_positions.append)
    worker.events = SimpleNamespace(append=lambda event, message, **kw: events.append((event, kw.get("level"))))
    worker.notifications = SimpleNamespace(
        stop_crossed_exit=lambda *args: "exit",
        stop_crossed_paused=lambda *args: "paused",
        notify_position_event=lambda position, notice: sent.append(notice),
    )
    monkeypatch.setattr(
        "scripts.bot_worker.get_settings_store",
        lambda: SimpleNamespace(load=lambda: saved if saved is not None else {}),
    )
    monkeypatch.setattr(
        "binance_spot_manager.market_close.close_market",
        lambda position, execution, positions, reason: closes.append(reason) or {"message": "Vente au marche confirmee"},
    )
    return worker, position, SimpleNamespace(events=events, sent=sent, closes=closes, EventType=EventType)


def test_crossed_stop_sells_at_market_after_fresh_price_check(monkeypatch):
    from binance_spot_manager.models import CloseReason

    worker, position, seen = _crossed_stop_worker(monkeypatch, price=80000.0)
    worker._exit_on_crossed_stop(position, 80640.0)

    assert seen.closes == [CloseReason.STOP_CROSSED]
    assert seen.sent == ["exit"]
    assert not position.automation.paused


def test_crossed_stop_does_not_sell_when_price_recovered(monkeypatch):
    worker, position, seen = _crossed_stop_worker(monkeypatch, price=81000.0)
    worker._exit_on_crossed_stop(position, 80640.0)

    assert seen.closes == [] and seen.sent == []
    assert not position.automation.paused


def test_crossed_stop_without_fresh_price_does_nothing(monkeypatch):
    worker, position, seen = _crossed_stop_worker(monkeypatch, price=BinanceError("timeout"))
    worker._exit_on_crossed_stop(position, 80640.0)

    assert seen.closes == [] and seen.sent == []


def test_crossed_stop_pauses_when_auto_exit_disabled(monkeypatch):
    worker, position, seen = _crossed_stop_worker(
        monkeypatch, price=80000.0, saved={"exit_on_crossed_stop": False},
    )
    worker._exit_on_crossed_stop(position, 80640.0)

    assert seen.closes == []
    assert position.automation.paused
    assert seen.sent == ["paused"]
    assert (seen.EventType.ERROR, "CRITICAL") in seen.events


def test_crossed_stop_pauses_when_market_exit_fails(monkeypatch):
    worker, position, seen = _crossed_stop_worker(monkeypatch, price=80000.0)

    def fail(*args, **kwargs):
        raise RuntimeError("Annulation non confirmee : aucune vente envoyee")

    monkeypatch.setattr("binance_spot_manager.market_close.close_market", fail)
    worker._exit_on_crossed_stop(position, 80640.0)

    assert position.automation.paused
    assert seen.sent == ["paused"]
    assert (seen.EventType.ERROR, "CRITICAL") in seen.events


def test_cycle_with_crossed_stop_triggers_exit_before_reporting_error():
    from binance_spot_manager.automation_engine import CycleResult

    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.status = PositionStatus.ACTIVE
    worker = Worker.__new__(Worker)
    handled = []
    worker.automation = SimpleNamespace(run_cycle=lambda p, price: CycleResult(
        stop_crossed_at=80640.0, errors=["SL non recree : Stop price would trigger immediately."],
    ))
    worker.positions = SimpleNamespace(save=lambda p: None)
    worker._exit_on_crossed_stop = lambda p, stop: handled.append(stop)

    with pytest.raises(RuntimeError):
        worker._process_position(position, 80000)

    assert handled == [80640.0]



def test_routing_failure_never_skips_commands_or_position_monitoring():
    import sqlite3

    worker = Worker.__new__(Worker)
    events, ran, monitored = [], [], []

    def broken():
        raise sqlite3.OperationalError("database is locked")

    worker.auto_signal_executor = SimpleNamespace(process_pending=broken)
    worker.command_processor = SimpleNamespace(run_one=lambda: ran.append(1))
    worker.events = SimpleNamespace(append=lambda *a, **k: events.append((a, k)))
    position = Position(symbol="ETHUSDT")
    worker.positions = SimpleNamespace(list_open=lambda: [position], read_errors=[])
    worker._price_provider = lambda symbols: lambda symbol: None
    worker._process_position = lambda p, price: monitored.append(p)
    worker._loop = 1
    worker._next_orphan_audit = 10**9  # audit des ordres orphelins hors sujet ici (tests/test_orphan_orders.py)

    assert worker._tick() == 1
    assert ran == [1] and monitored == [position]
    assert events and "Routage des signaux interrompu" in events[0][0][1]
    assert events[0][1]["level"] == "ERROR"
