"""Worker supervise par Docker : etat par heartbeat, veille au lieu d'un arret."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from binance_spot_manager import bot_process_manager as bpm
from binance_spot_manager.bot_process_manager import BotProcessManager, WorkerLock
from binance_spot_manager.config import Settings
from binance_spot_manager.models import BotRuntime, WorkerState, utcnow
from binance_spot_manager.position_store import RuntimeStore

pytestmark = pytest.mark.unit


@pytest.fixture
def docker_manager(tmp_path, monkeypatch):
    flag = tmp_path / "bot_stop.flag"
    monkeypatch.setattr(bpm, "BOT_STOP_FLAG", flag)
    monkeypatch.setattr(bpm, "get_settings_store", lambda: SimpleNamespace(load=lambda: {"worker_interval": 1}))
    runtime = RuntimeStore(tmp_path / "bot_runtime.json")
    manager = BotProcessManager(
        Settings(worker_supervisor="docker"),
        runtime_store=runtime,
        lock=WorkerLock(tmp_path / "bot_worker.lock", pid_checks=False),
        events=SimpleNamespace(append=lambda *args, **kwargs: None),
    )

    def heartbeat(state: WorkerState, age: float = 1.0, pid: int = 1) -> None:
        runtime.save(BotRuntime(state=state, pid=pid, heartbeat_at=utcnow() - timedelta(seconds=age)))

    return manager, heartbeat, flag


def test_status_uses_heartbeat_not_pid(docker_manager):
    manager, heartbeat, _ = docker_manager
    heartbeat(WorkerState.MONITORING, pid=999_999)  # PID d'un autre conteneur

    status = manager.status()

    assert status.running and status.pid_alive
    assert status.label == "Worker actif"


def test_old_heartbeat_means_container_down(docker_manager):
    manager, heartbeat, _ = docker_manager
    heartbeat(WorkerState.MONITORING, age=120)

    assert not manager.status().pid_alive
    assert not manager.status().running


def test_standby_is_alive_but_not_running(docker_manager):
    manager, heartbeat, flag = docker_manager
    flag.write_text("stop")
    heartbeat(WorkerState.PAUSED)

    status = manager.status()

    assert status.pid_alive and not status.running
    assert status.label == "Worker en veille"


def test_start_resumes_standby_by_removing_flag(docker_manager):
    manager, heartbeat, flag = docker_manager
    flag.write_text("stop")
    heartbeat(WorkerState.PAUSED)

    ok, _ = manager.start(wait_seconds=0)

    assert ok
    assert not flag.exists()


def test_start_without_heartbeat_points_to_make(docker_manager):
    manager, _, flag = docker_manager

    ok, message = manager.start(wait_seconds=0)

    assert not ok
    assert "make worker-start" in message
    assert not flag.exists()


def test_force_stop_and_cleanup_never_touch_a_docker_worker(docker_manager):
    manager, heartbeat, flag = docker_manager
    flag.write_text("stop")
    heartbeat(WorkerState.PAUSED)

    ok, message = manager.stop(force=True)
    manager.clear_orphan_state()

    assert not ok and "make worker-restart" in message
    assert flag.exists()  # la veille demandee est conservee


def test_lock_without_pid_checks_ignores_foreign_pid(tmp_path):
    lock_path = tmp_path / "bot_worker.lock"
    lock_path.write_text('{"pid": 1}')  # PID 1 existe toujours, dans un autre conteneur

    lock = WorkerLock(lock_path, pid_checks=False)

    assert lock.acquire(pid=4242)
    lock.release(pid=4242)


def _loop_worker(monkeypatch, *, docker: bool, stop_flag_cycles: int):
    from scripts.bot_worker import Worker

    worker = Worker.__new__(Worker)
    worker.settings = SimpleNamespace(worker_interval=1, worker_managed_by_docker=docker)
    worker._running = True
    worker._loop = 0
    states, ticks, events = [], [], []
    worker._set_state = lambda state, message="", **fields: states.append(state)
    worker.events = SimpleNamespace(append=lambda event, message, **kw: events.append(message))
    worker._has_open_positions = lambda: False
    flags = iter([True] * stop_flag_cycles + [False])
    worker._stop_requested = lambda: next(flags)

    def tick():
        ticks.append(1)
        worker._running = False
        return 0

    worker._tick = tick
    monkeypatch.setattr("scripts.bot_worker.time.sleep", lambda _: None)
    worker._loop_forever()
    return states, ticks, events


def test_docker_worker_waits_in_standby_then_resumes(monkeypatch):
    states, ticks, events = _loop_worker(monkeypatch, docker=True, stop_flag_cycles=3)

    assert states.count(WorkerState.PAUSED) == 3
    assert ticks == [1]  # aucun cycle de trading pendant la veille
    assert sum("veille" in message for message in events) == 1


def test_local_worker_still_exits_on_stop_flag(monkeypatch):
    states, ticks, _ = _loop_worker(monkeypatch, docker=False, stop_flag_cycles=1)

    assert ticks == []
    assert WorkerState.PAUSED not in states
