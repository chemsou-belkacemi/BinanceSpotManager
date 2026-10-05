"""Alertes de connexion : un message à chaque connexion réussie à l'interface et quand un compte se bloque après
trop d'échecs ; jamais le mot de passe ni le code ; désactivable ; la connexion ne dépend jamais de l'alerte."""
from __future__ import annotations

import time

from streamlit.testing.v1 import AppTest

import ui_common
from binance_spot_manager import auth
from binance_spot_manager.auth import AccountStore
from binance_spot_manager.notification_engine import NotificationEngine
from test_automation import settings  # noqa: F401 - fixture partagee
from test_location_ui import PASSWORD, ROOT, instance, login, texts  # noqa: F401 - fixtures partagees


def wrong_code(secret):
    return "000000" if auth.totp(secret, time.time()) != "000000" else "111111"


def test_a_successful_login_is_announced_not_a_simple_failure(instance, monkeypatch):  # noqa: F811
    alerts = []
    monkeypatch.setattr(ui_common, "_alert_login", lambda kind, user: alerts.append((kind, user)))
    account = AccountStore().create("client", PASSWORD)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    login(app, wrong_code(account.totp_secret))
    assert alerts == []                                                        # un échec simple : rien
    login(app, auth.totp(account.totp_secret, time.time()))
    assert not app.exception and alerts == [("LOGIN", "client")]
    assert "Par où commencer" in texts(app)


def test_the_account_store_flags_the_failure_that_locks(instance):  # noqa: F811
    account = AccountStore().create("client", PASSWORD)
    store = AccountStore()
    results = [store.authenticate("client", PASSWORD, wrong_code(account.totp_secret))
               for _ in range(auth.MAX_FAILURES)]
    assert [r.locked for r in results] == [False] * (auth.MAX_FAILURES - 1) + [True]
    assert results[-1].username == "client"


def test_login_messages_never_carry_secrets(settings):  # noqa: F811
    engine = NotificationEngine(settings)
    login_notice = engine.login_alert("LOGIN", "client", "203.0.113.7")
    assert login_notice.event == "LOGIN" and "client" in login_notice.title and "203.0.113.7" in login_notice.body
    locked = engine.login_alert("LOCKED", "client", "203.0.113.7")
    assert locked.level == "CRITICAL" and "bloque" in locked.title
    assert PASSWORD not in login_notice.body + locked.body


def test_alerts_can_be_switched_off_and_never_break_the_login(monkeypatch, tmp_path):
    from binance_spot_manager.position_store import JsonFileStore

    store = JsonFileStore(tmp_path / "settings.json")
    store.save({"login_alerts_enabled": False})
    monkeypatch.setattr("binance_spot_manager.position_store.get_settings_store", lambda: store)
    built = []
    monkeypatch.setattr("binance_spot_manager.notification_engine.NotificationEngine.login_alert",
                        lambda self, *a: built.append(a))
    ui_common._alert_login("LOGIN", "client")
    assert built == []
    store.save({})

    def broken(self, *a):
        raise RuntimeError("telegram")

    monkeypatch.setattr("binance_spot_manager.notification_engine.NotificationEngine.login_alert", broken)
    ui_common._alert_login("LOGIN", "client")                                    # aucune exception
