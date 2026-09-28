"""Notifications du navigateur : autorisation et envoi hors de l'onglet actif."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from binance_spot_manager.browser_notifications import (
    PERMISSION_HTML, browser_alert_preferences, notification_html,
)


APP = Path(__file__).resolve().parents[1] / "app.py"


def test_browser_payload_escapes_log_text_and_requires_permission():
    html = notification_html(
        {"ts": "1", "event": "ERROR", "message": "</script><script>alert(1)</script>"},
        duration_seconds=5, manual=False,
    )
    assert "</script><script>alert(1)" not in html
    assert "Notification.permission !== \"granted\"" in html
    assert "new Notification(" in html
    assert '"duration_ms": 5000' in html


def test_permission_is_requested_directly_from_browser_click():
    assert 'button.addEventListener("click"' in PERMISSION_HTML
    assert "Notification.requestPermission()" in PERMISSION_HTML


def test_browser_preferences_are_bounded():
    prefs = browser_alert_preferences({
        "browser_alert_sound": True,
        "browser_alert_mode": "manual",
        "browser_alert_duration": 999,
    })
    assert prefs.sound and prefs.manual and prefs.duration_seconds == 120
    assert browser_alert_preferences({"browser_alert_duration": "invalid"}).duration_seconds == 5


def test_controls_live_only_in_settings_and_test_emits_native_script(monkeypatch):
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
                        lambda self: {"BNB": {"free": 0.1, "locked": 0.02}})
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_price", lambda self, symbol: 500)
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not app.exception
    assert len(app.get("html")) == 0
    assert not any("Tester le bip" in button.label for button in app.button)
    app.switch_page("pages/5_Settings.py").run()
    assert not app.exception
    assert any(m.label == "Valeur du BNB disponible pour les frais" and m.value == "50.00 USDT" for m in app.metric)
    assert any(n.label == "M'alerter si mon BNB disponible vaut moins de (USDT)" for n in app.number_input)
    assert len(app.get("html")) == 1  # controle d'autorisation navigateur
    assert app.get("html")[0].proto.unsafe_allow_javascript
    next(button for button in app.button if "Tester la notification Windows" in button.label).click().run()
    assert not app.exception
    assert len(app.get("dialog")) == 0
    assert len(app.get("html")) == 2  # autorisation + notification systeme
    assert all(element.proto.unsafe_allow_javascript for element in app.get("html"))
    assert len(app.get("audio")) == 1
    app.switch_page("pages/4_History.py").run()
    assert not app.exception
    assert not any("Tester la notification Windows" in button.label for button in app.button)
