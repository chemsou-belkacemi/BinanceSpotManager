"""Interrupteurs du propriétaire (2026-10-06, désactivés par défaut) : toutes les conversations autorisées valent
groupe de confiance ; toutes les cryptos sont acceptées en automatique. Les autres contrôles restent."""
from __future__ import annotations

from binance_spot_manager import signal_routing
from binance_spot_manager.signal_inbox import SignalInbox
from test_signal_auto_execution import SIMPLE, TRUSTED_CHAT, enabled_preferences, executor, telegram_id

FET = SIMPLE.replace("BTC/USDT", "FET/USDT")
OTHER_CHAT = -100777


def run(tmp_path, text, chat, **preferences):
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", text, source="telegram", external_id=telegram_id(1, chat=chat), source_timestamp=995)
    worker, _ = executor(tmp_path, inbox, enabled_preferences(**preferences))
    return worker.process_pending(), inbox.recent("demo")[0]


def reasons(row):
    return {r.code for r in signal_routing.decision_from_json(row.get("route") or "").reasons} if row.get("route") else set()


def test_switches_are_off_by_default():
    policy = signal_routing.RoutingPolicy.from_mapping({})
    assert not policy.trust_all_chats and not policy.all_assets
    assert not signal_routing.RoutingPolicy.from_mapping({"signal_auto_trust_all_chats": "true"}).trust_all_chats


def test_every_allowed_conversation_counts_as_trusted_when_switched_on(tmp_path):
    chats = f"{TRUSTED_CHAT},{OTHER_CHAT}"
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    _, row = run(tmp_path / "off", SIMPLE, OTHER_CHAT, signal_telegram_chats=chats)
    assert "C_SOURCE_UNDECLARED" in reasons(row)
    outcome, row = run(tmp_path / "on", SIMPLE, OTHER_CHAT, signal_telegram_chats=chats,
                       signal_auto_trust_all_chats=True)
    assert outcome == ["QUEUED"]


def test_every_crypto_is_accepted_when_switched_on(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    _, row = run(tmp_path / "off", FET, TRUSTED_CHAT, signal_auto_base_assets=["BTC"])
    assert "C_UNIVERSE" in reasons(row)
    _, row = run(tmp_path / "on", FET, TRUSTED_CHAT, signal_auto_base_assets=["BTC"], signal_auto_all_assets=True)
    assert "C_UNIVERSE" not in reasons(row)


def test_turning_a_switch_on_is_a_widening():
    before = signal_routing.RoutingPolicy.from_mapping({})
    after = signal_routing.RoutingPolicy.from_mapping({"signal_auto_trust_all_chats": True,
                                                       "signal_auto_all_assets": True})
    widened = before.widened_by(after)
    assert "toutes les conversations autorisées de confiance" in widened and "tous les actifs acceptés" in widened
    assert after.widened_by(before) == []


def test_settings_save_the_switches_with_the_widening_authorisation(monkeypatch, tmp_path):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from binance_spot_manager.position_store import JsonFileStore

    store = JsonFileStore(tmp_path / "settings.json")
    monkeypatch.setattr("binance_spot_manager.position_store.get_settings_store", lambda: store)
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
                        lambda self: {"BNB": {"free": 0.1, "locked": 0}})
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_price", lambda self, symbol: 500)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    assert not app.exception
    app.toggle(key="signal_trust_all_chats_toggle").set_value(True)
    app.toggle(key="signal_all_assets_toggle").set_value(True)
    next(b for b in app.button if b.label == "Enregistrer le routage des signaux").click().run()
    assert not app.exception and not store.load().get("signal_auto_trust_all_chats")      # élargissement non autorisé
    app.checkbox(key="signal_routing_widen_authorization").check()
    next(b for b in app.button if b.label == "Enregistrer le routage des signaux").click().run()
    saved = store.load()
    assert saved["signal_auto_trust_all_chats"] is True and saved["signal_auto_all_assets"] is True


def test_routing_save_shows_what_was_stored_and_the_confirmation_never_stays(tmp_path, monkeypatch):
    """La case de confirmation ne reste jamais cochée (une confirmation par enregistrement) ; le message de réussite
    rappelle ce qui est réellement enregistré, pour qu'on ne croie pas avoir enregistré en cochant seulement."""
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from binance_spot_manager.position_store import JsonFileStore

    store = JsonFileStore(tmp_path / "settings.json")
    monkeypatch.setattr("binance_spot_manager.position_store.get_settings_store", lambda: store)
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
                        lambda self: {"BNB": {"free": 0.1, "locked": 0}})
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_price", lambda self, symbol: 500)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    app.toggle(key="signal_trust_all_chats_toggle").set_value(True)
    app.toggle(key="signal_all_assets_toggle").set_value(True)
    app.toggle(key="signal_honor_demo_manual_toggle").set_value(False)
    app.checkbox(key="signal_routing_widen_authorization").check()
    next(b for b in app.button if b.label == "Enregistrer le routage des signaux").click().run()
    assert not app.exception
    message = " ".join(s.value for s in app.success)
    assert "conversations de confiance = toutes" in message and "cryptos acceptées = toutes" in message
    assert "DEMO_MANUAL impose la confirmation = non" in message
    saved = store.load()
    assert saved["signal_auto_trust_all_chats"] is True and saved["signal_route_honor_demo_manual"] is False
    fresh = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
    fresh.switch_page("pages/5_Settings.py").run()
    assert fresh.checkbox(key="signal_routing_widen_authorization").value is False          # jamais gardée
    assert fresh.toggle(key="signal_trust_all_chats_toggle").value is True                  # l'interrupteur, si
