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
    app.get_by_key("signal_auto_touch_stop_toggle").set_value(True).run()
    app.number_input(key="signal_auto_entry_count_input").set_value(2).run()
    app.get_by_key("signal_auto_entry_distribution_choice").set_value("CUSTOM").run()
    app.get_by_key("signal_auto_entry_custom_input").set_value("30;70").run()
    app.number_input(key="signal_auto_tp_count_input").set_value(2).run()
    app.get_by_key("signal_auto_tp_distribution_choice").set_value("CUSTOM").run()
    app.get_by_key("signal_auto_tp_custom_input").set_value("80;20").run()
    next(b for b in app.button if b.label == "Enregistrer l'exécution automatique").click().run()

    assert not app.exception
    saved = store.load()
    assert saved["signal_auto_execute_enabled"] is True
    assert saved["signal_auto_execute_enabled_since"] > 0
    assert saved["signal_auto_max_age_minutes"] == 5
    assert saved["signal_auto_touch_stop"] is True
    assert saved["signal_auto_entry_count"] == 2
    assert saved["signal_auto_entry_distribution"] == "CUSTOM"
    assert saved["signal_auto_entry_custom_percentages"] == "30;70"
    assert saved["signal_auto_tp_count"] == 2
    assert saved["signal_auto_tp_distribution"] == "CUSTOM"
    assert saved["signal_auto_tp_custom_percentages"] == "80;20"


def test_csi_gate_settings_are_saved_and_csi_outage_is_shown(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        "binance_spot_manager.csi_client.CsiClient.probe",
        lambda self: (None, "CSI injoignable sur http://csi-api:8503 (ConnectionError)"),
    )
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    assert not app.exception
    assert app.get_by_key("signal_csi_gate_toggle").value is True          # prudent par défaut
    assert any("injoignable" in w.value and "retenus" in w.value for w in app.warning)

    app.get_by_key("signal_csi_hold_indetermine_toggle").set_value(True)
    app.get_by_key("signal_csi_when_unavailable_choice").set_value("Exécuter quand même, sans avis")
    app.get_by_key("signal_csi_source_names_input").set_value("-1001234=Suhaib")
    next(b for b in app.button if b.label == "Enregistrer l'avis CSI").click().run()
    assert not app.exception
    saved = store.load()
    assert saved["signal_csi_gate_enabled"] is True
    assert saved["signal_csi_hold_indetermine"] is True
    assert saved["signal_csi_when_unavailable"] == "ALLOW"
    assert saved["signal_csi_source_names"] == "-1001234=Suhaib"

    app.get_by_key("signal_csi_source_names_input").set_value("Suhaib")
    next(b for b in app.button if b.label == "Enregistrer l'avis CSI").click().run()
    assert any("Nom de groupe invalide" in e.value for e in app.error)
    assert store.load()["signal_csi_source_names"] == "-1001234=Suhaib"
