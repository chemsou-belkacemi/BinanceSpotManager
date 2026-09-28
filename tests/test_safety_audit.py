"""Regressions d'audit : concurrence, reprise, transport et annulations."""

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests

from binance_spot_manager.binance_client import BinanceError, BinanceSpotClient
from binance_spot_manager.bot_process_manager import WorkerLock
from binance_spot_manager.file_mutex import FileMutex
from binance_spot_manager.config import Environment, RunMode, SecurityError, Settings
from binance_spot_manager.event_store import EventStore
from binance_spot_manager.execution_engine import ExecutionEngine
from binance_spot_manager.models import Position, PositionStatus, SLStatus
from binance_spot_manager.order_journal import OrderJournal
from binance_spot_manager.position_store import ConcurrentPositionUpdate, PositionStore
from binance_spot_manager.reconciliation_engine import ReconciliationEngine

pytestmark = pytest.mark.unit


def client_for_test(tmp_path):
    client = BinanceSpotClient(Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test-key", demo_api_secret="test-secret"))
    client._time_synced_at = time.time()
    client._order_journal = OrderJournal(tmp_path / "orders.sqlite3")
    return client


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


def test_intent_is_claimed_once_across_connections(tmp_path):
    path = tmp_path / "orders.sqlite3"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: OrderJournal(path).claim("demo", "BTCUSDT", "same-id", {}), range(20)))
    assert sum(results) == 1
    assert not OrderJournal(path).claim("demo", "BTCUSDT", "same-id", {})


def test_timeout_then_new_client_does_not_repeat_post(tmp_path, monkeypatch, caplog):
    first = client_for_test(tmp_path)
    def fail(*a, **k):
        raise requests.Timeout("URL?signature=SECRET&apiKey=test-key")
    monkeypatch.setattr(first._session, "request", fail)
    with pytest.raises(BinanceError) as exc:
        first.create_order(symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity="0.001", client_order_id="stable")
    assert exc.value.is_ambiguous_write
    assert "SECRET" not in caplog.text + str(exc.value)
    second = client_for_test(tmp_path)
    monkeypatch.setattr(second._session, "request", lambda *a, **k: pytest.fail("Second POST forbidden"))
    with pytest.raises(BinanceError) as duplicate:
        second.create_order(symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity="0.001", client_order_id="stable")
    assert duplicate.value.is_ambiguous_write


@pytest.mark.parametrize("settings", [Settings(), Settings(environment=Environment.LIVE, run_mode=RunMode.DEMO_MANUAL)])
def test_transport_blocks_dry_run_and_live_before_http(settings, monkeypatch):
    client = BinanceSpotClient(settings)
    monkeypatch.setattr(client._session, "request", lambda *a, **k: pytest.fail("HTTP forbidden"))
    with pytest.raises(SecurityError):
        client.create_order(symbol="BTCUSDT", side="BUY", order_type="MARKET", client_order_id="test")


def test_malformed_order_response_is_ambiguous_and_redirects_disabled(tmp_path, monkeypatch):
    client = client_for_test(tmp_path)
    def request(*a, **k):
        assert k["allow_redirects"] is False
        return response({})
    monkeypatch.setattr(client._session, "request", request)
    with pytest.raises(BinanceError) as exc:
        client.create_order(symbol="BTCUSDT", side="BUY", order_type="MARKET", client_order_id="incomplete")
    assert exc.value.is_ambiguous_write


def test_two_worker_locks_cannot_acquire_same_lease(tmp_path):
    first, second = WorkerLock(tmp_path / "worker.lock"), WorkerLock(tmp_path / "worker.lock")
    try:
        assert first.acquire()
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_mutex_is_shared_with_another_process(tmp_path):
    path = tmp_path / "process.lease"
    code = (
        "import sys; from pathlib import Path; "
        "from binance_spot_manager.file_mutex import FileMutex; "
        "lock = FileMutex(Path(sys.argv[1])); print(lock.acquire()); lock.release()"
    )
    def child_can_acquire():
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True,
            text=True, timeout=15, check=True,
        )
        return result.stdout.strip() == "True"

    with FileMutex(path):
        assert not child_can_acquire()
    assert child_can_acquire()


def test_stale_position_cannot_overwrite_newer_copy(tmp_path):
    store = PositionStore(tmp_path)
    p = Position(symbol="BTCUSDT", status=PositionStatus.ACTIVE)
    store.save(p)
    stale = store.load(p.position_id)
    p.tags = ["new"]
    store.save(p)
    stale.tags = ["old"]
    with pytest.raises(ConcurrentPositionUpdate):
        store.save(stale)
    assert store.load(p.position_id).tags == ["new"]
    backup = tmp_path / ".backups" / f"{p.position_id}.json"
    assert json.loads(backup.read_text(encoding="utf-8"))["revision"] == 1


def test_store_rejects_traversal_and_preserves_same_symbol_positions(tmp_path):
    store = PositionStore(tmp_path)
    with pytest.raises(ValueError):
        store.load("../outside")
    first = Position(symbol="BTCUSDT", status=PositionStatus.ACTIVE)
    second = Position(symbol="BTCUSDT", status=PositionStatus.ACTIVE)
    store.save(first)
    store.save(second)
    assert len(store.list_active_by_symbol("BTCUSDT")) == 2
    stale = Position(position_id=first.position_id, symbol="BTCUSDT", status=PositionStatus.ACTIVE)
    with pytest.raises(ConcurrentPositionUpdate):
        store.save(stale)


@pytest.mark.parametrize("status,qty,expected", [("NEW", "0", False), ("FILLED", "1", False), ("CANCELED", "0", True), ("CANCELED", "0.5", False)])
def test_unknown_cancel_requires_terminal_unfilled_confirmation(status, qty, expected):
    def cancel(*a, **k):
        raise BinanceError("unknown", code=-2011)
    client = SimpleNamespace(cancel_order=cancel, get_order=lambda *a, **k: {
        "orderId": 1, "status": status, "executedQty": qty, "cummulativeQuoteQty": "10",
    })
    engine = ExecutionEngine(client, settings=Settings(run_mode=RunMode.DEMO_MANUAL))
    assert engine.cancel_order("BTCUSDT", order_id=1).success is expected


def test_read_only_reconciliation_does_not_change_sl_or_emit_events(tmp_path):
    p = Position(symbol="BTCUSDT", status=PositionStatus.ACTIVE)
    p.stop_loss.status = SLStatus.ACTIVE
    p.stop_loss.order_id = 1
    before = p.model_dump_json()
    from binance_spot_manager.execution_engine import OrderResult
    execution = SimpleNamespace(settings=Settings(run_mode=RunMode.DEMO_MANUAL),
        get_open_orders=lambda symbol: [], fetch_order_status=lambda *a, **k: OrderResult(success=True, status="CANCELED"))
    events = EventStore(tmp_path / "events.jsonl")
    report = ReconciliationEngine(execution, events=events).reconcile(p, apply=False)
    assert report.has_desync
    assert p.model_dump_json() == before
    assert events.tail() == []


def test_event_tail_reads_last_valid_records(tmp_path):
    events = EventStore(tmp_path / "events.jsonl")
    for n in range(5):
        events.append("TEST", "é" * 40000, n=n)
    assert [row["data"]["n"] for row in events.tail(2)] == [3, 4]


def test_recovered_fill_includes_base_commission(tmp_path, monkeypatch):
    client = client_for_test(tmp_path)
    monkeypatch.setattr(client, "get_my_trades", lambda *a, **k: [
        {"id": 1, "qty": "0.001", "price": "85000", "commission": "0.000001", "commissionAsset": "BTC"},
    ])
    raw = {"symbol": "BTCUSDT", "orderId": 1, "executedQty": "0.001", "status": "FILLED"}
    hydrated = client._with_fills(raw)
    from binance_spot_manager.execution_engine import normalize_order_response
    assert normalize_order_response(hydrated).commissions[0].amount == 0.000001
    assert "fills" not in raw


def test_incomplete_fills_block_net_quantity_calculation(tmp_path, monkeypatch):
    client = client_for_test(tmp_path)
    monkeypatch.setattr(client, "get_my_trades", lambda *a, **k: [])
    with pytest.raises(BinanceError, match="incomplet"):
        client._with_fills({"symbol": "BTCUSDT", "orderId": 1, "executedQty": "0.001"})


def test_settings_repr_and_dump_never_include_secrets():
    settings = Settings(demo_api_key="secret-key-test", demo_api_secret="secret-token-test", smtp_password="secret-password-test")
    content = repr(settings) + settings.model_dump_json()
    assert "secret-key-test" not in content
    assert "secret-token-test" not in content
    assert "secret-password-test" not in content
