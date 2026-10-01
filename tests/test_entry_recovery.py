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


def ticking_worker(engine, store, loop, *, restarted=False, clock=None):
    from scripts.bot_worker import Worker

    worker = Worker.__new__(Worker)
    worker.positions = store
    worker.reconciliation = engine
    worker.notifications = SimpleNamespace(desync=lambda *args: None, notify_position_event=lambda *args: None)
    worker._loop = loop
    worker._resume_reconcile = restarted        # pose par Worker.__init__ (demarrage) et _leave_standby (veille)
    worker._price_provider = lambda symbols: (lambda symbol: None)     # aucun prix : seul le suivi compte
    worker._sync_quote_balance = lambda: None
    if clock is not None:
        worker._monotonic = lambda: clock[0]    # horloge monotone simulee, en secondes
    return worker


def test_a_filled_limit_entry_is_recorded_on_the_next_tick_not_twelve_ticks_later(recovery):
    """Lacune L1 (audit du 2026-10-01) : tant que l'achat rempli n'est pas constate, aucun stop.
    Premiere relecture d'un achat en attente : au tour suivant, puis au plus toutes les 10 s."""
    engine, state, position, store = recovery
    store.save(position)
    state["order"] = order("FILLED", 1.0)
    ticking_worker(engine, store, loop=5, clock=[1000.0])._tick()   # ni premier tour, ni multiple de 12
    restored = store.load(position.position_id)
    assert restored.entries[0].status is EntryStatus.FILLED
    assert restored.metrics.net_qty == pytest.approx(0.999)


def test_a_pending_entry_is_reread_at_most_once_every_ten_seconds(recovery):
    """Charge Binance : a 1 s par tour, relire un achat en attente a chaque tour approchait la
    limite de poids (6 000/min) avec 5 positions ; une relecture toutes les 10 s suffit."""
    from scripts.bot_worker import AWAITING_FILL_RECONCILE_SECONDS

    engine, _, position, store = recovery
    store.save(position)                                            # entree SUBMITTED : en attente
    seen = []
    spy = SimpleNamespace(reconcile=lambda p: seen.append(p.position_id) or engine.reconcile(p))
    clock = [1000.0]
    worker = ticking_worker(spy, store, loop=5, clock=clock)        # ni reprise, ni multiple de 12
    worker._tick()
    clock[0], worker._loop = clock[0] + 1, 6                        # tour suivant, 1 s plus tard
    worker._tick()
    assert seen == [position.position_id]
    clock[0], worker._loop = 1000.0 + AWAITING_FILL_RECONCILE_SECONDS, 7
    worker._tick()                                                  # 10 s apres la premiere relecture
    assert seen == [position.position_id] * 2


def test_a_full_reconciliation_also_counts_as_a_reread_of_pending_entries(recovery):
    from scripts.bot_worker import RECONCILE_EVERY

    engine, _, position, store = recovery
    store.save(position)
    seen = []
    spy = SimpleNamespace(reconcile=lambda p: seen.append(p.position_id) or engine.reconcile(p))
    clock = [1000.0]
    worker = ticking_worker(spy, store, loop=RECONCILE_EVERY, clock=clock)
    worker._tick()                                                  # reconciliation complete
    clock[0], worker._loop = clock[0] + 1, RECONCILE_EVERY + 1
    worker._tick()                                                  # pas de seconde lecture 1 s apres
    assert seen == [position.position_id]


def test_a_starting_worker_owes_a_full_reconciliation_on_its_first_tick(monkeypatch):
    """Le constructeur pose le drapeau de reprise ; aucun composant reel (Binance, stockage) n'est cree."""
    from unittest.mock import MagicMock

    from scripts import bot_worker

    for name in ("get_settings", "EventStore", "PositionStore", "RuntimeStore", "WorkerLock",
                 "BinanceSpotClient", "SymbolRulesCache", "ExecutionEngine", "PositionEngine",
                 "AutomationEngine", "ReconciliationEngine", "NotificationEngine", "DemoMarketPriceStream",
                 "CommandStore", "SignalInbox", "DashboardService", "CommandProcessor", "FeeTokenMonitor",
                 "TelegramSignalPoller", "account_scope", "AutomaticSignalExecutor", "CsiClient"):
        monkeypatch.setattr(bot_worker, name, MagicMock())
    assert bot_worker.Worker()._resume_reconcile is True


def test_every_open_position_is_reconciled_on_the_first_tick_after_a_restart(recovery):
    engine, _, position, store = recovery
    position.entries[0].status = EntryStatus.FILLED                 # rien en attente : seul le 1er tour compte
    store.save(position)
    seen = []
    spy = SimpleNamespace(reconcile=lambda p: seen.append(p.position_id) or engine.reconcile(p))
    worker = ticking_worker(spy, store, loop=1, restarted=True)
    worker._tick()
    assert seen == [position.position_id]
    seen.clear()
    worker._loop = 2
    worker._tick()                                                  # la reprise n'est faite qu'une fois
    assert seen == []


def test_restart_reconciliation_is_postponed_not_lost_when_the_first_tick_fails(recovery):
    engine, _, position, store = recovery
    position.entries[0].status = EntryStatus.FILLED
    store.save(position)
    seen = []
    spy = SimpleNamespace(reconcile=lambda p: seen.append(p.position_id) or engine.reconcile(p))
    worker = ticking_worker(spy, store, loop=1, restarted=True)
    healthy = worker._price_provider

    def unavailable(symbols):
        raise RuntimeError("prix illisibles")

    worker._price_provider = unavailable
    with pytest.raises(RuntimeError, match="prix illisibles"):
        worker._tick()
    assert seen == []
    worker._price_provider = healthy
    worker._loop = 2
    worker._tick()
    assert seen == [position.position_id]


def test_every_open_position_is_reconciled_again_when_the_worker_leaves_standby(recovery):
    """Sous Docker, Arreter met le worker en veille sans quitter le process : la relance est une
    reprise (rien n'a ete suivi pendant la veille) alors que _loop ne repasse jamais a 1."""
    engine, _, position, store = recovery
    position.entries[0].status = EntryStatus.FILLED                 # rien en attente de remplissage
    store.save(position)
    seen = []
    spy = SimpleNamespace(reconcile=lambda p: seen.append(p.position_id) or engine.reconcile(p))
    worker = ticking_worker(spy, store, loop=7)                     # ni premier tour, ni multiple de 12
    worker.events = engine.events
    worker._in_standby = True
    worker._leave_standby()
    worker._tick()
    assert seen == [position.position_id]
    seen.clear()
    worker._loop = 8
    worker._tick()
    assert seen == []
