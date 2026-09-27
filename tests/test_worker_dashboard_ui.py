"""Le bouton d'arret suit la fin du worker sans second clic ni processus reel."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from binance_spot_manager.bot_process_manager import WorkerStatus
from ui_common import get_service


DASHBOARD = Path(__file__).resolve().parents[1] / "pages" / "1_Dashboard.py"


def test_one_stop_click_waits_then_refreshes(monkeypatch):
    service = get_service()
    status = WorkerStatus(
        running=True, pid_alive=True, pid=12345, state="MONITORING",
    )
    calls = []
    monkeypatch.setattr(service, "worker_status", lambda: status)
    monkeypatch.setattr(
        service.process_manager, "request_stop",
        lambda: (calls.append("stop") or True, "Arret demande"),
    )

    app = AppTest.from_file(str(DASHBOARD), default_timeout=20).run()
    assert not app.exception
    stop = next(button for button in app.button if "Arrêter proprement" in button.label)
    stop.click().run()
    assert not app.exception
    assert calls == ["stop"]
    assert app.session_state["worker_stop_pending"] is True

    status.running = False
    status.pid_alive = False
    status.state = "STOPPED"
    app.run()
    assert not app.exception
    assert app.session_state["worker_stop_pending"] is False
    assert calls == ["stop"]
