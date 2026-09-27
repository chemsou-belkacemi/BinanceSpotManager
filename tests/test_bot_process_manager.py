"""Cycle de vie du worker : tests sans lancement de processus reel."""

from binance_spot_manager.bot_process_manager import WorkerStatus, worker_command


def test_windows_worker_prefers_windowless_interpreter(tmp_path):
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    python.touch()
    pythonw.touch()
    script = tmp_path / "bot_worker.py"
    assert worker_command(str(python), script, windows=True) == [str(pythonw), str(script)]
    assert worker_command(str(python), script, windows=False) == [str(python), str(script)]


def test_windows_worker_falls_back_when_pythonw_is_absent(tmp_path):
    python = tmp_path / "python.exe"
    python.touch()
    script = tmp_path / "bot_worker.py"
    assert worker_command(str(python), script, windows=True) == [str(python), str(script)]


def test_worker_status_reports_stopping_before_active():
    status = WorkerStatus(running=True, pid_alive=True, stop_flag_present=True)
    assert status.label == "Worker en arrêt"
