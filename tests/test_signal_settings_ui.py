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
