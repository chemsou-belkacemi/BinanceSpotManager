"""Le bip d'une alerte ne doit pas ecrire hors du fragment Streamlit."""

from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

import ui_common


def test_worker_stop_alert_with_sound_does_not_crash_fragment(monkeypatch):
    records = []
    service = SimpleNamespace(
        user_settings=lambda: {"browser_alert_sound": True},
        events=SimpleNamespace(tail=lambda **kwargs: list(records)),
    )
    monkeypatch.setattr(ui_common, "get_service", lambda: service)

    def render():
        import ui_common

        ui_common.global_alerts()

    app = AppTest.from_function(render, default_timeout=10).run()
    assert not app.exception
    records.append({
        "ts": "2026-09-27T23:00:00Z", "event": "WORKER_STOPPED",
        "level": "INFO", "message": "Worker arrete", "symbol": "",
    })
    app.run()
    assert not app.exception
    assert len(app.get("audio")) == 1
    assert len(app.get("html")) == 1
