"""Le heartbeat du worker doit refléter les positions réellement suivies."""

from types import SimpleNamespace

import pytest

from binance_spot_manager.models import Position, SyncStatus, WorkerState
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
