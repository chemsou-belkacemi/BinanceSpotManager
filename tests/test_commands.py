"""Demandes persistantes et execution exclusivement dans le worker, hors reseau."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from binance_spot_manager.command_store import CommandStore, account_scope
from binance_spot_manager.command_processor import CommandProcessor
from binance_spot_manager.config import Settings, RunMode
from binance_spot_manager.event_store import EventStore
from binance_spot_manager.execution_engine import OrderResult
from binance_spot_manager.models import Entry, EntryStatus, Position, PositionStatus, SLStatus, TakeProfit, TPStatus
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.risk_engine import RiskLimits
from binance_spot_manager.symbol_rules import parse_symbol_rules


def test_same_confirmation_survives_concurrent_connections(tmp_path):
    path = tmp_path / "commands.db"
    def submit(_):
        return CommandStore(path).enqueue("demo", "SIMPLE_BUY", {"symbol": "BTCUSDT"}, request_key="same")["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(submit, range(20)))) == 1
    store = CommandStore(path)
    with pytest.raises(ValueError, match="autre demande"):
        store.enqueue("demo", "SIMPLE_BUY", {"symbol": "ETHUSDT"}, request_key="same")
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(r is not None for r in pool.map(lambda _: store.claim("demo"), range(20))) == 1


def test_interrupted_commands_are_not_replayed(tmp_path):
    store = CommandStore(tmp_path / "commands.db")
    row = store.enqueue("demo", "CANCEL_ORDER", {}, request_key="one")
    assert store.claim("other") is None
    assert store.claim("demo")["id"] == row["id"]
    restarted = CommandStore(store.path)
    assert restarted.recover_interrupted("demo") == 1
    assert restarted.claim("demo") is None
    assert restarted.list_recent("demo")[0]["state"] == "UNCERTAIN"


def test_expiration_and_pending_cancel(tmp_path, monkeypatch):
    store = CommandStore(tmp_path / "commands.db")
    monkeypatch.setattr("binance_spot_manager.command_store.time.time", lambda: 10)
    row = store.enqueue("demo", "CANCEL_ORDER", {}, request_key="one", ttl=1)
    monkeypatch.setattr("binance_spot_manager.command_store.time.time", lambda: 12)
    assert store.claim("demo") is None
    assert store.list_recent("demo")[0]["state"] == "EXPIRED"
    row = store.enqueue("demo", "CANCEL_ORDER", {}, request_key="two")
    assert store.cancel_pending("demo", row["id"])
    assert not store.cancel_pending("demo", row["id"])
    assert store.claim("demo") is None


@pytest.fixture
def processor(tmp_path):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test-key", demo_api_secret="test-secret")
    rules = parse_symbol_rules({"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
                               "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "100"},
                                           {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                           {"filterType": "NOTIONAL", "minNotional": "5"}]})
    calls = []
    client = SimpleNamespace(get_price=lambda symbol: 84000, get_prices=lambda: {"BTCUSDT": 84000, "EURUSDT": 1.1},
                             get_balances=lambda: {"USDT": {"free": 10000, "locked": 0}}, get_free_balance=lambda asset: 10000)
    def place(position, entry, **kwargs):
        calls.append(entry.client_order_id)
        entry.order_id, entry.status = 7, EntryStatus.FILLED
        return OrderResult(success=True, status="FILLED", order_id=7, executed_qty=entry.binance_qty,
                           average_price=84000, cummulative_quote_qty=entry.binance_qty * 84000)
    execution = SimpleNamespace(settings=settings, client=client, rules=lambda *a, **k: rules,
                                events=EventStore(tmp_path / "events.jsonl"), place_entry=place,
                                place_simple_buy=lambda **k: (calls.append(k) or OrderResult(success=True, order_id=8, status="FILLED")),
                                cancel_order=lambda *a, **k: (calls.append(k) or OrderResult(success=True, order_id=k["order_id"], status="CANCELED")))
    worker = CommandProcessor(CommandStore(tmp_path / "commands.db"), PositionStore(tmp_path / "positions"), execution, RiskLimits)
    return worker, calls


def proposed():
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", status=PositionStatus.PENDING_ENTRIES, creation_price=84000)
    position.entries = [Entry(binance_qty=0.001, quote_amount=84, resolved_price=84000)]
    position.stop_loss.resolved_price = 80000
    position.take_profits = [TakeProfit(target_price=90000, sell_percent=100)]
    return position


def queue_position(worker, position=None):
    position = position or proposed()
    worker.store.enqueue(worker.scope, "SUBMIT_POSITION", {
        "position": position.model_dump(mode="json"), "entry_ids": [e.entry_id for e in position.entries], "reference_price": 84000,
    }, request_key="new")
    return position


def test_ui_queue_does_not_send_before_worker_and_worker_sends_once(processor):
    worker, calls = processor
    position = queue_position(worker)
    assert calls == []
    assert not worker.positions.exists(position.position_id)
    assert worker.run_one() == "SUCCEEDED"
    assert len(calls) == 1
    assert worker.positions.load(position.position_id).entries[0].executed_qty == 0.001
    assert worker.run_one() is None
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["price", "risk", "stop", "funds"])
def test_worker_revalidates_before_post(processor, failure):
    worker, calls = processor
    position = proposed()
    if failure == "price":
        worker.execution.client.get_price = lambda symbol: 90000
    elif failure == "risk":
        worker.risk_limits = lambda: RiskLimits(max_exposure_per_symbol_percent=0.1)
    elif failure == "stop":
        position.stop_loss.resolved_price = 85000
    else:
        worker.execution.client.get_balances = lambda: {"USDT": {"free": 10, "locked": 0}}
    queue_position(worker, position)
    assert worker.run_one() == "FAILED"
    assert calls == []


def test_unknown_entry_keeps_stable_identity_and_is_not_resent(processor):
    worker, calls = processor
    def timeout(position, entry, **kwargs):
        calls.append(entry.client_order_id)
        raise RuntimeError("Reponse perdue")
    worker.execution.place_entry = timeout
    position = queue_position(worker)
    assert worker.run_one() == "UNCERTAIN"
    saved = worker.positions.load(position.position_id)
    assert saved.entries[0].status is EntryStatus.SUBMITTED
    assert saved.entries[0].client_order_id
    worker.store.recover_interrupted(worker.scope)
    assert worker.run_one() is None
    assert len(calls) == 1


def test_submitted_plan_cannot_inject_existing_sell_orders(processor):
    worker, calls = processor
    position = proposed()
    position.stop_loss.order_id = 123
    queue_position(worker, position)
    assert worker.run_one() == "FAILED"
    assert calls == []


def test_manual_cancel_pauses_automation_instead_of_recreating_stop(processor):
    worker, calls = processor
    p = proposed()
    p.stop_loss.status, p.stop_loss.order_id = SLStatus.ACTIVE, 42
    worker.positions.save(p)
    worker.store.enqueue(worker.scope, "CANCEL_ORDER", {"symbol": p.symbol, "order_id": 42}, request_key="cancel")
    assert worker.run_one() == "SUCCEEDED"
    p = worker.positions.load(p.position_id)
    assert p.automation.paused
    assert p.stop_loss.status is SLStatus.CANCELED


def test_unconfirmed_cancel_never_changes_local_state(processor):
    worker, calls = processor
    worker.execution.cancel_order = lambda *a, **k: OrderResult(success=False, status="UNKNOWN", error="Rapprochement requis")
    p = proposed()
    p.stop_loss.status, p.stop_loss.order_id = SLStatus.ACTIVE, 42
    worker.positions.save(p)
    worker.store.enqueue(worker.scope, "CANCEL_ORDER", {"symbol": p.symbol, "order_id": 42}, request_key="cancel")
    assert worker.run_one() == "UNCERTAIN"
    assert worker.positions.load(p.position_id).stop_loss.status is SLStatus.ACTIVE


def test_close_refuses_unknown_tp(processor):
    worker, calls = processor
    p = proposed()
    p.take_profits[0].status, p.take_profits[0].client_order_id = TPStatus.SUBMITTED, "unknown-tp"
    worker.positions.save(p)
    worker.store.enqueue(worker.scope, "CLOSE_LOCAL", {"position_id": p.position_id}, request_key="close")
    assert worker.run_one() == "FAILED"
    assert worker.positions.load(p.position_id).is_open
    assert calls == []


def test_account_and_mode_scopes_are_distinct():
    a = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="a")
    b = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="b")
    c = Settings(run_mode=RunMode.DRY_RUN, demo_api_key="a")
    assert len({account_scope(a), account_scope(b), account_scope(c)}) == 3
    assert "secret-key" not in account_scope(Settings(demo_api_key="secret-key"))


def test_connection_command_does_not_send_an_order(processor):
    worker, calls = processor
    pings = []
    worker.execution.client.ping = lambda: pings.append("ping")
    worker.store.enqueue(worker.scope, "CHECK_CONNECTION", {}, request_key="check")
    assert worker.run_one() == "SUCCEEDED"
    assert pings == ["ping"]
    assert calls == []
