"""Changing sizing mode must immediately unlock the appropriate budget field."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from binance_spot_manager.position_store import JsonFileStore


def test_fixed_budget_can_be_edited_before_saving_mode(monkeypatch, tmp_path):
    store = JsonFileStore(tmp_path / "settings.json")
    monkeypatch.setattr(
        "binance_spot_manager.position_store.get_settings_store", lambda: store,
    )
    monkeypatch.setattr(
        "binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
        lambda self: {"BNB": {"free": 0.1, "locked": 0}},
    )
    monkeypatch.setattr(
        "binance_spot_manager.binance_client.BinanceSpotClient.get_price",
        lambda self, symbol: 500,
    )
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    assert not app.exception
    assert app.number_input(key="signal_fixed_budget_input").disabled

    app.get_by_key("signal_sizing_mode_choice").set_value("FIXED").run()
    assert not app.exception
    assert not app.number_input(key="signal_fixed_budget_input").disabled
    assert store.load() == {}

    app.number_input(key="signal_fixed_budget_input").set_value(75)
    next(b for b in app.button if b.label == "Enregistrer le budget des signaux").click().run()
    assert not app.exception
    assert store.load()["signal_sizing_mode"] == "FIXED"
    assert store.load()["signal_fixed_budget"] == 75
    app.run()
    assert app.number_input(key="signal_fixed_budget_input").value == 75


def test_auto_execution_requires_authorization_and_persists_activation(monkeypatch, tmp_path):
    store = JsonFileStore(tmp_path / "settings.json")
    store.save({
        "signal_telegram_enabled": True,
        "signal_telegram_auto_enabled": True,
        "signal_telegram_chats": "99",
    })
    monkeypatch.setattr(
        "binance_spot_manager.position_store.get_settings_store", lambda: store,
    )
    monkeypatch.setattr(
        "binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
        lambda self: {"BNB": {"free": 0.1, "locked": 0}},
    )
    monkeypatch.setattr(
        "binance_spot_manager.binance_client.BinanceSpotClient.get_price",
        lambda self, symbol: 500,
    )
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()

    app.get_by_key("signal_auto_execute_toggle").set_value(True).run()
    app.get_by_key("signal_auto_execute_authorization").set_value(True).run()
    next(b for b in app.button if b.label == "Enregistrer l'exécution automatique").click().run()

    assert not app.exception
    saved = store.load()
    assert saved["signal_auto_execute_enabled"] is True
    assert saved["signal_auto_execute_enabled_since"] > 0
    assert saved["signal_auto_max_age_minutes"] == 5
