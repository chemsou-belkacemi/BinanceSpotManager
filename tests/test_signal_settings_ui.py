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
    # Désactivé par défaut depuis 2026-10-08 : le propriétaire l'active explicitement.
    assert app.get_by_key("signal_csi_gate_toggle").value is False
    app.get_by_key("signal_csi_gate_toggle").set_value(True).run()

    app.get_by_key("signal_csi_hold_indetermine_toggle").set_value(True)
    app.get_by_key("signal_csi_when_unavailable_choice").set_value("Exécuter quand même, sans avis")
    app.get_by_key("signal_csi_source_names_input").set_value("-1001234=Suhaib")
    next(b for b in app.button if b.label == "Enregistrer les liens avec CSI").click().run()
    assert not app.exception
    saved = store.load()
    assert saved["signal_csi_gate_enabled"] is True
    assert saved["signal_csi_hold_indetermine"] is True
    assert saved["signal_csi_when_unavailable"] == "ALLOW"
    assert saved["signal_csi_source_names"] == "-1001234=Suhaib"
    app.run()
    assert any("injoignable" in w.value and "exécutés sans avis" in w.value for w in app.warning)

    app.get_by_key("signal_csi_source_names_input").set_value("Suhaib")
    next(b for b in app.button if b.label == "Enregistrer les liens avec CSI").click().run()
    assert any("Nom de groupe invalide" in e.value for e in app.error)
    assert store.load()["signal_csi_source_names"] == "-1001234=Suhaib"


def test_signal_stop_trailing_is_on_by_default_and_can_be_turned_off(monkeypatch, tmp_path):
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
    assert app.get_by_key("signal_trail_stop_toggle").value is True

    app.get_by_key("signal_trail_stop_toggle").set_value(False)
    next(b for b in app.button if b.label == "Enregistrer le suivi du SL").click().run()
    assert not app.exception
    assert store.load()["signal_trail_stop"] is False


# ==========================================================================
# Routage : confirmation manuelle ou exécution automatique
# ==========================================================================


def open_settings(monkeypatch, tmp_path, saved):
    import json

    store = JsonFileStore(tmp_path / "settings.json")
    store.save(saved)
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setattr("binance_spot_manager.position_store.get_settings_store", lambda: store)
    monkeypatch.setattr("binance_spot_manager.event_store.EVENTS_FILE", events_path)
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
                        lambda self: {"BNB": {"free": 0.1, "locked": 0}})
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_price",
                        lambda self, symbol: 500)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    assert not app.exception

    def journal():
        if not events_path.exists():
            return []
        return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line]

    return app, store, journal


def click(app, label):
    next(b for b in app.button if b.label == label).click().run()


def test_trusted_chats_must_be_allowlisted_and_widening_needs_authorization(monkeypatch, tmp_path):
    app, store, journal = open_settings(monkeypatch, tmp_path, {"signal_telegram_chats": "-100123, -100456"})

    app.text_input(key="signal_trusted_chats_input").set_value("-100999").run()
    click(app, "Enregistrer le routage des signaux")
    assert any("hors des conversations autorisées" in e.value for e in app.error)
    assert "signal_auto_trusted_chats" not in store.load()

    app.text_input(key="signal_trusted_chats_input").set_value("-100123").run()
    click(app, "Enregistrer le routage des signaux")
    assert any("cocher l'autorisation" in e.value for e in app.error)
    assert "signal_auto_trusted_chats" not in store.load()

    app.checkbox(key="signal_routing_widen_authorization").check().run()
    click(app, "Enregistrer le routage des signaux")
    assert not app.exception
    saved = store.load()
    assert saved["signal_auto_trusted_chats"] == [-100123]
    assert len(saved["signal_auto_base_assets"]) == 16  # liste pré-remplie conservée
    assert any(e["event"] == "SIGNAL_ROUTING_CHANGED" for e in journal())


def test_thresholds_bounds_clamp_and_tightening_needs_no_authorization(monkeypatch, tmp_path):
    from binance_spot_manager.risk_engine import RiskLimits
    from binance_spot_manager.signal_routing import RoutingPolicy

    app, store, journal = open_settings(monkeypatch, tmp_path, {"signal_telegram_chats": "-100123"})
    assert app.number_input(key="signal_review_max_risk_input").value == 0.5
    # Resserrer (liste d'actifs réduite, 2 ordres/24 h) : aucune autorisation requise.
    app.text_input(key="signal_base_assets_input").set_value("BTC, ETH").run()
    app.number_input(key="signal_auto_max_per_24h_input").set_value(2).run()
    click(app, "Enregistrer le routage des signaux")
    assert not any("autorisation" in e.value or "Groupes" in e.value for e in app.error)
    assert store.load()["signal_auto_base_assets"] == ["BTC", "ETH"]
    # Un seuil plus large que la limite dure est enregistré mais plafonné à l'usage.
    app.checkbox(key="signal_routing_widen_authorization").check().run()
    app.number_input(key="signal_review_max_risk_input").set_value(5.0).run()
    click(app, "Enregistrer le routage des signaux")
    assert store.load()["signal_review_max_risk_percent"] == 5.0
    assert RoutingPolicy.from_mapping(store.load(), RiskLimits()).max_risk_percent == 1.0
    assert len([e for e in journal() if e["event"] == "SIGNAL_ROUTING_CHANGED"]) == 2


def test_rearm_stores_timestamp_and_empty_declarations_warn(monkeypatch, tmp_path):
    app, store, journal = open_settings(monkeypatch, tmp_path, {
        "signal_telegram_chats": "-100123", "signal_auto_execute_enabled": True,
        "signal_auto_execute_enabled_since": 900, "signal_auto_base_assets": []})
    assert any("aucun groupe de confiance ou aucun actif validé" in w.value for w in app.warning)
    assert next(b for b in app.button if b.label == "Réarmer l'automatique").disabled
    app.checkbox(key="signal_rearm_confirmation").check().run()
    click(app, "Réarmer l'automatique")
    assert store.load()["signal_auto_breaker_reset_at"] > 0
    assert any(e["event"] == "SIGNAL_ROUTING_CHANGED" and "réarmés" in e["message"] for e in journal())
