"""Liens avec CSI désactivés par défaut (2026-10-08) : avis avant l'exécution automatique, conseil de taille,
revue « volatilité haute », retour d'exécution, feu CSI. Sans clé enregistrée, BSM n'appelle pas CSI et n'écrit
rien pour lui ; un réglage enregistré garde sa valeur. Aucun réseau, aucun ordre."""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from binance_spot_manager import csi_light
from binance_spot_manager.csi_client import (
    CSI_LINK_DEFAULTS,
    FEEDBACK_ENABLED_KEY,
    GATE_ENABLED_KEY,
    HIGH_VOLATILITY_REVIEW_KEY,
    SIZE_ADVICE_ENABLED_KEY,
    CsiOpinion,
    GatePolicy,
    link_enabled,
)
from binance_spot_manager.command_store import CommandStore
from binance_spot_manager.position_store import JsonFileStore, PositionStore
from binance_spot_manager.signal_feedback import SignalFeedbackWriter
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.signal_routing import RoutingPolicy
from test_signal_auto_execution import SIMPLE, enabled_preferences, executor, telegram_id
from test_signal_csi import NOW, btc_rules, csi_text, drop_preferences, queue_signal, receive_csi  # noqa: F401

LINKS = (GATE_ENABLED_KEY, SIZE_ADVICE_ENABLED_KEY, HIGH_VOLATILITY_REVIEW_KEY, FEEDBACK_ENABLED_KEY)


class ExplodingCsi:
    """Faux client CSI : tout appel fait échouer le test."""

    def evaluate(self, *args, **kwargs):
        pytest.fail("POST /evaluate appelé alors que l'avis CSI est désactivé")

    def risk(self, *args, **kwargs):
        pytest.fail("GET /risk appelé alors que le conseil de taille est désactivé")

    def meteo(self, *args, **kwargs):
        pytest.fail("GET /meteo appelé alors que le feu CSI est désactivé")


class CountingCsi:
    """Faux client CSI : avis fixe et conseil de risque, appels comptés."""

    def __init__(self, verdict="FAVORABLE"):
        self.verdict, self.evaluate_calls, self.risk_calls = verdict, 0, 0

    def evaluate(self, text, *, source, record=True, user_validated=False):
        self.evaluate_calls += 1
        return CsiOpinion(verdict=self.verdict, summary="résumé", source=source,
                          evaluated_at="2026-10-08T00:00:00+00:00")

    def risk(self):
        self.risk_calls += 1
        return {"available": True, "pairs": {"BTCUSDT": {"move_24h_pct": 2.5, "relative_size": 1.6}}}


def without_links(preferences):
    """Réglages sans AUCUNE clé de lien avec CSI : seules les valeurs par défaut comptent."""
    return {key: value for key, value in preferences.items() if key not in LINKS}


def telegram_signal(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    return inbox


# -- valeurs par défaut ---------------------------------------------------------------------------------------

def test_every_csi_link_is_disabled_by_default():
    assert set(CSI_LINK_DEFAULTS) == set(LINKS) and not any(CSI_LINK_DEFAULTS.values())
    assert all(link_enabled({}, key) is False for key in LINKS)
    assert GatePolicy().enabled is False and GatePolicy.from_mapping({}).enabled is False
    assert RoutingPolicy().csi_high_volatility_review is False
    assert RoutingPolicy.from_mapping({}).csi_high_volatility_review is False
    assert csi_light.LightPolicy.from_mapping({}).enabled is False


def test_a_saved_setting_keeps_its_value():
    saved = {key: True for key in LINKS}
    assert all(link_enabled(saved, key) for key in LINKS)
    assert GatePolicy.from_mapping(saved).enabled is True
    assert RoutingPolicy.from_mapping(saved).csi_high_volatility_review is True
    assert not any(link_enabled({key: False for key in LINKS}, key) for key in LINKS)
    # Un texte n'active que s'il dit oui (bool("false") vaudrait True).
    assert link_enabled({SIZE_ADVICE_ENABLED_KEY: "false"}, SIZE_ADVICE_ENABLED_KEY) is False
    assert link_enabled({SIZE_ADVICE_ENABLED_KEY: "oui"}, SIZE_ADVICE_ENABLED_KEY) is True


# -- avis CSI et conseil de taille ----------------------------------------------------------------------------

def test_without_settings_the_worker_never_calls_csi_and_never_holds_a_signal(tmp_path):
    inbox = telegram_signal(tmp_path)
    worker, commands = executor(tmp_path, inbox, without_links(enabled_preferences()), csi_client=ExplodingCsi())

    assert worker.process_pending() == ["QUEUED"]
    metrics = commands.list_recent("demo")[0]["payload"]["route"]["metrics"]
    assert "csi_size" not in metrics                                      # rien à afficher sur la page Signaux
    assert inbox.recent("demo")[0].get("csi_verdict") in (None, "")


def test_without_settings_an_unreachable_csi_holds_nothing(tmp_path):
    inbox = telegram_signal(tmp_path)
    worker, commands = executor(tmp_path, inbox, without_links(enabled_preferences()), csi_client=None)

    assert worker.process_pending() == ["QUEUED"] and len(commands.list_recent("demo")) == 1


def test_saved_gate_and_size_advice_are_honoured(tmp_path):
    inbox = telegram_signal(tmp_path)
    csi = CountingCsi()
    preferences = enabled_preferences(**{GATE_ENABLED_KEY: True, SIZE_ADVICE_ENABLED_KEY: True})
    worker, commands = executor(tmp_path, inbox, preferences, csi_client=csi)

    assert worker.process_pending() == ["QUEUED"]
    assert csi.evaluate_calls == 1 and csi.risk_calls == 1
    advice = commands.list_recent("demo")[0]["payload"]["route"]["metrics"]["csi_size"]
    assert advice["available"] and advice["proposed_budget"] == pytest.approx(144.0)
    assert inbox.recent("demo")[0]["csi_verdict"] == "FAVORABLE"


def test_saved_gate_holds_an_unfavourable_signal_as_before(tmp_path):
    inbox = telegram_signal(tmp_path)
    worker, commands = executor(tmp_path, inbox, enabled_preferences(**{GATE_ENABLED_KEY: True}),
                                csi_client=CountingCsi("DEFAVORABLE"))

    assert worker.process_pending() == ["REVIEW"] and commands.list_recent("demo") == []
    assert '"C_CSI_OPINION"' in inbox.recent("demo")[0]["route"]


def test_saved_gate_without_size_advice_never_asks_for_risk(tmp_path):
    inbox = telegram_signal(tmp_path)
    csi = CountingCsi()
    worker, commands = executor(tmp_path, inbox, enabled_preferences(**{GATE_ENABLED_KEY: True}), csi_client=csi)

    assert worker.process_pending() == ["QUEUED"]
    assert csi.evaluate_calls == 1 and csi.risk_calls == 0
    assert "csi_size" not in commands.list_recent("demo")[0]["payload"]["route"]["metrics"]


# -- revue « volatilité haute » --------------------------------------------------------------------------------

@pytest.mark.parametrize("saved, expected", [(None, "QUEUED"), (False, "QUEUED"), (True, "REVIEW")])
def test_high_volatility_review_only_when_saved(tmp_path, saved, expected):
    inbox = SignalInbox(tmp_path / "signals.db")
    receive_csi(inbox, csi_text(VOLATILITY_REGIME="HIGH"))
    preferences = without_links(drop_preferences())
    if saved is not None:
        preferences[HIGH_VOLATILITY_REVIEW_KEY] = saved
    worker, _ = executor(tmp_path, inbox, preferences, now=NOW, csi_client=ExplodingCsi())

    assert worker.process_pending() == [expected]
    route = inbox.recent("demo")[0].get("route") or ""
    assert ('"R8_CSI_HIGH_VOLATILITY"' in route) is (expected == "REVIEW")


# -- retour d'exécution -----------------------------------------------------------------------------------------

def feedback_writer(tmp_path, preferences, *, enabled=True):
    inbox = SignalInbox(tmp_path / "signals.db")
    commands = CommandStore(tmp_path / "commands.db")
    writer = SignalFeedbackWriter("demo", inbox, commands, positions=PositionStore(tmp_path / "positions"),
                                  directory=tmp_path / "outgoing", registry_path=tmp_path / "feedback.db",
                                  clock=lambda: NOW, enabled=enabled, preferences=preferences)
    return inbox, commands, writer


def test_without_settings_no_feedback_file_is_written(tmp_path, btc_rules):  # noqa: F811
    inbox, commands, writer = feedback_writer(tmp_path, lambda: {})

    assert writer.enabled is False and writer.snapshot()["state"] == "DISABLED"
    queue_signal(inbox, commands, writer, csi_text(), btc_rules)
    assert writer.sync([]) == 0
    assert writer.record_rejection("CSI-X-1", "BTCUSDT", "refus") is False
    assert not writer.path.exists() and not (tmp_path / "outgoing").exists()
    assert not writer.knows("CSI-X-1")


def test_saved_feedback_setting_writes_as_before_and_is_read_each_time(tmp_path, btc_rules):  # noqa: F811
    preferences = {FEEDBACK_ENABLED_KEY: True}
    inbox, commands, writer = feedback_writer(tmp_path, lambda: preferences)

    assert writer.enabled is True and writer.snapshot()["state"] == "ACTIVE"
    queue_signal(inbox, commands, writer, csi_text(), btc_rules)
    assert writer.sync([]) == 1                                            # RECEIVED, comme avant
    assert len(writer.path.read_text(encoding="utf-8").splitlines()) == 1

    preferences[FEEDBACK_ENABLED_KEY] = False                              # coupé dans Settings, sans redémarrage
    assert writer.enabled is False and writer.sync([]) == 0
    assert writer.record_rejection("CSI-X-2", "BTCUSDT", "refus") is False
    assert len(writer.path.read_text(encoding="utf-8").splitlines()) == 1


def test_feedback_stays_off_in_dry_run_or_with_unreadable_settings(tmp_path):
    _, _, dry = feedback_writer(tmp_path / "dry", lambda: {FEEDBACK_ENABLED_KEY: True}, enabled=False)
    assert dry.enabled is False

    def broken():
        raise OSError("settings.json illisible")

    _, _, unreadable = feedback_writer(tmp_path / "ko", broken)
    assert unreadable.enabled is False and unreadable.sync([]) == 0


def test_the_worker_wires_the_feedback_switch_to_the_settings(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from scripts import bot_worker

    for name in ("get_settings", "EventStore", "PositionStore", "RuntimeStore", "WorkerLock",
                 "BinanceSpotClient", "SymbolRulesCache", "ExecutionEngine", "PositionEngine",
                 "AutomationEngine", "ReconciliationEngine", "NotificationEngine", "DemoMarketPriceStream",
                 "CommandStore", "SignalInbox", "DashboardService", "CommandProcessor", "FeeTokenMonitor",
                 "TelegramSignalPoller", "account_scope", "AutomaticSignalExecutor", "CsiClient",
                 "SignalFeedbackWriter"):
        monkeypatch.setattr(bot_worker, name, MagicMock())
    saved = {FEEDBACK_ENABLED_KEY: True}
    monkeypatch.setattr(bot_worker, "get_settings_store", lambda: SimpleNamespace(load=lambda: saved))
    bot_worker.get_settings.return_value.dry_run = False

    bot_worker.Worker()
    kwargs = bot_worker.SignalFeedbackWriter.call_args.kwargs
    assert kwargs["enabled"] is True and kwargs["preferences"]() == saved


# -- Settings → Signaux → Liens avec CSI ------------------------------------------------------------------------

def settings_app(monkeypatch, store, probe):
    monkeypatch.setattr("binance_spot_manager.position_store.get_settings_store", lambda: store)
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
                        lambda self: {"BNB": {"free": 0.1, "locked": 0}})
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_price", lambda self, symbol: 500)
    monkeypatch.setattr("binance_spot_manager.csi_client.CsiClient.probe", probe)
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    assert not app.exception
    return app


def test_settings_show_every_csi_link_off_and_never_probe_csi(monkeypatch, tmp_path):
    store = JsonFileStore(tmp_path / "settings.json")
    app = settings_app(monkeypatch, store, lambda self: pytest.fail("CSI interrogé alors que l'avis est désactivé"))

    assert any(s.value == "Liens avec CSI (tous désactivés par défaut)" for s in app.subheader)
    for key in ("signal_csi_gate_toggle", "signal_csi_size_advice_toggle", "signal_review_volatility_toggle",
                "signal_csi_feedback_toggle"):
        assert app.get_by_key(key).value is False
    for hidden in ("signal_csi_hold_indetermine_toggle", "signal_csi_when_unavailable_choice",
                   "signal_csi_source_names_input"):
        with pytest.raises(KeyError):
            app.get_by_key(hidden)                                         # visibles seulement si l'avis est activé
    assert any("Feu de protection CSI" in c.value and "désactivé" in c.value for c in app.caption)
    assert store.load() == {}


def test_settings_save_every_csi_link(monkeypatch, tmp_path):
    store = JsonFileStore(tmp_path / "settings.json")
    app = settings_app(monkeypatch, store, lambda self: (None, "CSI injoignable (ConnectionError)"))

    for key in ("signal_csi_size_advice_toggle", "signal_review_volatility_toggle", "signal_csi_feedback_toggle"):
        app.get_by_key(key).set_value(True)
    next(b for b in app.button if b.label == "Enregistrer les liens avec CSI").click().run()
    assert not app.exception
    saved = store.load()
    assert saved == {GATE_ENABLED_KEY: False, SIZE_ADVICE_ENABLED_KEY: True, HIGH_VOLATILITY_REVIEW_KEY: True,
                     FEEDBACK_ENABLED_KEY: True}                           # options de l'avis non écrites

    # Couper la revue « volatilité haute » élargit l'automatique : confirmation exigée.
    app.get_by_key("signal_review_volatility_toggle").set_value(False).run()
    next(b for b in app.button if b.label == "Enregistrer les liens avec CSI").click().run()
    assert any("cocher la confirmation" in e.value for e in app.error)
    assert store.load()[HIGH_VOLATILITY_REVIEW_KEY] is True
    app.get_by_key("signal_csi_volatility_widen_authorization").set_value(True)
    next(b for b in app.button if b.label == "Enregistrer les liens avec CSI").click().run()
    assert store.load()[HIGH_VOLATILITY_REVIEW_KEY] is False


def test_settings_respect_saved_links(monkeypatch, tmp_path):
    store = JsonFileStore(tmp_path / "settings.json")
    store.save({key: True for key in LINKS})
    app = settings_app(monkeypatch, store, lambda self: ({"ready": True, "detail": "prêt"}, ""))

    for key in ("signal_csi_gate_toggle", "signal_csi_size_advice_toggle", "signal_review_volatility_toggle",
                "signal_csi_feedback_toggle"):
        assert app.get_by_key(key).value is True
    assert app.get_by_key("signal_csi_hold_indetermine_toggle").value is False
    assert any("CSI : prêt" in s.value for s in app.success)
