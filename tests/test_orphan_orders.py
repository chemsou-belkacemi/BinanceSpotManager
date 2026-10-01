"""Lacune L2 (audit du 2026-10-01) : ordres BSM orphelins chez Binance, jamais vus par le worker.

Au demarrage puis tous les ORPHAN_AUDIT_EVERY tours, le worker compare les ordres ouverts du compte
aux positions locales. Un ordre « BSM-… » inconnu suspend l'execution AUTOMATIQUE des signaux (echec
sur) ; le suivi des positions continue et rien n'est annule. Aucun reseau.
"""

from types import SimpleNamespace

from binance_spot_manager.binance_client import BinanceError
from binance_spot_manager.command_store import CommandStore
from binance_spot_manager.event_store import EventStore
from binance_spot_manager.models import Entry, OcoExit, Position, PositionStatus, TakeProfit
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.reconciliation_engine import find_orphan_bot_orders
from binance_spot_manager.signal_auto_execution import AutomaticSignalExecutor
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.symbol_rules import SymbolRulesCache, parse_symbol_rules
from scripts.bot_worker import ORPHAN_AUDIT_EVERY, RECONCILE_EVERY, Worker

SIGNAL = "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000"
NOW = 1_000_000.0
ORPHAN_SL = {
    "symbol": "ETHUSDT", "orderId": 77, "clientOrderId": "BSM-D-ETH-1a2b3c4d5e6f-SL",
    "side": "SELL", "type": "STOP_LOSS_LIMIT", "status": "NEW",
}


def worker_with_signal_waiting(tmp_path, open_orders):
    """Worker reduit : une position BTC suivie et un signal Telegram frais pret a partir."""
    rules = parse_symbol_rules({
        "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
        "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "100"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ],
    })
    client = SimpleNamespace(
        get_symbol_info=lambda symbol: rules.raw, get_price=lambda symbol: 84500,
        get_prices=lambda: {"BTCUSDT": 84500}, get_balances=lambda: {"USDT": {"free": 1000, "locked": 0}},
    )
    inbox, commands = SignalInbox(tmp_path / "signals.db"), CommandStore(tmp_path / "commands.db")
    inbox.receive("demo", SIGNAL, source="telegram", external_id="bot:-100:9", source_timestamp=NOW - 5)
    preferences = {
        "signal_telegram_enabled": True, "signal_telegram_auto_enabled": True,
        "signal_auto_execute_enabled": True, "signal_auto_execute_enabled_since": NOW - 100,
        "signal_auto_max_age_minutes": 5, "signal_sizing_mode": "FIXED", "signal_fixed_budget": 200,
        "signal_csi_gate_enabled": False,
    }
    events = EventStore(tmp_path / "events.jsonl")
    positions = PositionStore(tmp_path / "positions")
    tracked = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", status=PositionStatus.ACTIVE)
    positions.save(tracked)
    reads = []

    def get_open_orders(symbol=None):
        reads.append(symbol)
        return open_orders()

    worker = Worker.__new__(Worker)
    worker.events, worker.positions = events, positions
    worker.execution = SimpleNamespace(get_open_orders=get_open_orders)
    worker.auto_signal_executor = AutomaticSignalExecutor(
        "demo", inbox, commands, client, SymbolRulesCache(client),
        lambda: SimpleNamespace(min_reserve_percent=20), lambda: preferences, events, clock=lambda: NOW,
    )
    monitored = []
    worker._process_position = lambda position, price: monitored.append(position.position_id)
    worker._price_provider = lambda symbols: (lambda symbol: None)
    worker._reconcile = lambda positions: None
    worker._sync_quote_balance = lambda: None
    worker._loop = 1
    return SimpleNamespace(worker=worker, commands=commands, events=events, monitored=monitored,
                           tracked=tracked, reads=reads)


def critical(events):
    return [event for event in events.tail() if event["level"] == "CRITICAL"]


def test_unknown_bsm_order_raises_a_critical_event_and_nothing_is_queued_automatically(tmp_path):
    """Faux openOrders : un SL BSM-D-ETH-… sans aucune position locale ETH."""
    orders = [dict(ORPHAN_SL)]
    setup = worker_with_signal_waiting(tmp_path, lambda: orders)

    setup.worker._tick()  # premier tour : controle avant toute mise en file

    alerts = critical(setup.events)
    assert len(alerts) == 1 and ORPHAN_SL["clientOrderId"] in alerts[0]["message"]
    assert setup.reads == [None]  # un seul appel, toutes les paires du compte
    assert setup.commands.list_recent("demo") == []  # rien mis en file par l'execution automatique
    assert setup.worker.auto_signal_executor.snapshot()["state"] == "SUSPENDED"
    assert setup.monitored == [setup.tracked.position_id]  # le suivi des positions continue

    setup.worker._loop = 2
    setup.worker._tick()  # controle suivant pas encore du : la suspension tient
    assert setup.commands.list_recent("demo") == []
    assert len(critical(setup.events)) == 1  # pas un evenement par tour

    orders.clear()  # ordre annule par le proprietaire
    setup.worker._loop = 1 + ORPHAN_AUDIT_EVERY
    setup.worker._tick()
    assert len(setup.commands.list_recent("demo")) == 1  # execution automatique retablie


def test_unreadable_open_orders_keep_automatic_execution_suspended(tmp_path):
    def offline():
        raise BinanceError("Echec reseau : ConnectionError")

    setup = worker_with_signal_waiting(tmp_path, offline)

    setup.worker._tick()

    assert setup.commands.list_recent("demo") == []
    assert setup.worker.auto_signal_executor.snapshot()["state"] == "SUSPENDED"
    assert setup.monitored == [setup.tracked.position_id]
    for loop in range(2, 1 + RECONCILE_EVERY):
        setup.worker._loop = loop
        setup.worker._tick()
    assert setup.reads == [None]  # pas un appel a chaque tour apres un echec
    setup.worker._loop = 1 + RECONCILE_EVERY
    setup.worker._tick()
    assert setup.reads == [None, None]
    assert setup.commands.list_recent("demo") == []
    warnings = [e for e in setup.events.tail() if "ordres orphelins impossible" in e["message"]]
    assert len(warnings) == 1


def test_orders_known_locally_or_placed_outside_bsm_are_not_orphans():
    entry_owner = Position(symbol="BTCUSDT", entries=[Entry(client_order_id="BSM-D-BTC-aaaa-E1")])
    closed_owner = Position(symbol="ETHUSDT", status=PositionStatus.CLOSED,
                            take_profits=[TakeProfit(order_id=5, client_order_id="BSM-D-ETH-bbbb-TP1GTC")])
    oco_owner = Position(symbol="SOLUSDT", oco_exit=OcoExit(
        order_list_id=1, list_client_order_id="BSM-D-SOL-cccc-OCO1", tp_order_id=10, sl_order_id=11,
        quantity=1))
    orders = [
        {"symbol": "BTCUSDT", "orderId": 1, "clientOrderId": "BSM-D-BTC-aaaa-E1"},
        {"symbol": "ETHUSDT", "orderId": 5, "clientOrderId": "BSM-D-ETH-bbbb-TP1GTC"},
        {"symbol": "SOLUSDT", "orderId": 11, "clientOrderId": "BSM-D-SOL-cccc-OCOSL1"},
        {"symbol": "BTCUSDT", "orderId": 5, "clientOrderId": "BSM-D-BTC-dddd-SL"},  # orderId d'une autre paire
        {"symbol": "BTCUSDT", "orderId": 6, "clientOrderId": "web_manual_order"},  # passe hors BSM
    ]

    orphans = find_orphan_bot_orders(orders, [entry_owner, closed_owner, oco_owner])

    assert [order["clientOrderId"] for order in orphans] == ["BSM-D-BTC-dddd-SL"]


def test_leaving_standby_checks_orphans_again_before_any_automatic_signal(tmp_path):
    setup = worker_with_signal_waiting(tmp_path, lambda: [dict(ORPHAN_SL)])
    setup.worker._loop, setup.worker._next_orphan_audit = 30, 61  # controle recent avant la veille
    setup.worker._in_standby = True
    setup.worker._leave_standby()

    setup.worker._tick()

    assert setup.reads == [None]
    assert len(critical(setup.events)) == 1
    assert setup.commands.list_recent("demo") == []
