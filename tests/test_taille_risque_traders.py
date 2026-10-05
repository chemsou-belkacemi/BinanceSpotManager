"""Taille selon le risque, trader ou canal perdant (revue ou taille réduite), conseil de taille de CSI affiché sans
être appliqué, raison de clôture gardée. Tout est désactivé par défaut. Aucun ordre réel, aucun réseau."""
from __future__ import annotations

import pytest

from binance_spot_manager.csi_client import CsiOpinion, CsiUnavailable, size_advice, size_advice_text
from binance_spot_manager.market_close import close_market, poll_market_close
from binance_spot_manager.models import CloseReason, PositionStatus
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.signal_sizing import (
    ChannelPolicy,
    RiskSizingPolicy,
    average_entry_price,
    risk_based_budget,
)
from test_automation import make_position, rules  # noqa: F401 - fixture partagée
from test_cloture_reliquat_affichage import with_dust
from test_journal_telegram_corrections import losing_position
from test_market_close import setup  # noqa: F401
from test_signal_auto_execution import SIMPLE, enabled_preferences, executor, positions_stub, telegram_id
from test_suivi_canaux_protection import labelled

ON = RiskSizingPolicy(enabled=True, risk_percent=0.4, max_budget_percent=30.0)


# -- calcul ------------------------------------------------------------------------------------------------------

def test_the_same_loss_at_every_stop():
    near = risk_based_budget(ON, entries=[100.0], stop=98.0, total_capital=10_000, usable_quote=8_000)
    far = risk_based_budget(ON, entries=[100.0], stop=92.0, total_capital=10_000, usable_quote=8_000)
    assert near.budget == pytest.approx(2_000) and far.budget == pytest.approx(500)
    assert near.budget * 0.02 == pytest.approx(far.budget * 0.08) == pytest.approx(40)   # 0,4 % de 10 000
    assert near.stop_distance_pct == pytest.approx(2.0) and near.capped_by == ""


def test_the_cap_and_the_reserve_limit_a_very_close_stop():
    capped = risk_based_budget(ON, entries=[100.0], stop=99.5, total_capital=10_000, usable_quote=8_000)
    assert capped.budget == pytest.approx(3_000) and capped.capped_by == "plafond"       # 8 000 voulus
    short = risk_based_budget(ON, entries=[100.0], stop=99.5, total_capital=10_000, usable_quote=1_200)
    assert short.budget == pytest.approx(1_200) and short.capped_by == "réserve"


def test_no_size_without_a_usable_stop_or_when_disabled():
    assert risk_based_budget(ON, entries=[100.0], stop=None, total_capital=10_000, usable_quote=8_000) is None
    assert risk_based_budget(ON, entries=[100.0], stop=101.0, total_capital=10_000, usable_quote=8_000) is None
    assert risk_based_budget(ON, entries=[100.0], stop=98.0, total_capital=0, usable_quote=8_000) is None
    off = RiskSizingPolicy()
    assert not off.enabled and risk_based_budget(off, entries=[100.0], stop=98.0, total_capital=10_000,
                                                 usable_quote=8_000) is None


def test_the_average_entry_follows_the_budget_split():
    assert average_entry_price([100.0, 90.0]) == pytest.approx(2 / (1 / 100 + 1 / 90))
    assert average_entry_price([100.0, 90.0], [70, 30]) == pytest.approx(100 / (70 / 100 + 30 / 90))
    assert average_entry_price([]) == 0.0


def test_settings_are_off_by_default_and_bounded():
    assert RiskSizingPolicy.from_mapping({}) == RiskSizingPolicy(enabled=False, risk_percent=0.4, max_budget_percent=20)
    assert RiskSizingPolicy.from_mapping({"signal_risk_percent": 50}).risk_percent == 0.4      # hors bornes
    policy = ChannelPolicy.from_mapping({})
    assert not policy.enabled and policy.action == "REVIEW" and policy.min_trades == 30
    assert ChannelPolicy.from_mapping({"signal_channel_action": "SELL"}).action == "REVIEW"
    assert ChannelPolicy(kept_percent=50).reduced(84.37) == pytest.approx(42.18)


# -- exécution automatique ---------------------------------------------------------------------------------------

def _queued_metrics(tmp_path, preferences, *, positions=None, csi_client=None, origin=""):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995,
                        origin=origin)
    worker, commands = executor(tmp_path, inbox, preferences, positions=positions, csi_client=csi_client)
    outcome = worker.process_pending()
    if outcome != ["QUEUED"]:
        return outcome, inbox.recent("demo")[0]
    payload = commands.get_by_request_key("demo", f"signal:{row['id']}")["payload"]
    return outcome, payload


def test_automatic_signals_use_the_risk_size_only_when_enabled(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    _, payload = _queued_metrics(tmp_path / "off", enabled_preferences())
    assert payload["route"]["metrics"]["budget"] == 90 and "risk_sizing" not in payload["route"]["metrics"]
    _, payload = _queued_metrics(tmp_path / "on", enabled_preferences(signal_risk_sizing_enabled=True))
    sized = payload["route"]["metrics"]["risk_sizing"]
    # Capital 1 000, perte visée 0,4 % = 4 ; entrée 84 000, stop 80 000 : distance 4,76 % → budget 84.
    assert sized["budget"] == pytest.approx(84.0) and payload["route"]["metrics"]["budget"] == pytest.approx(84.0)
    spent = sum(e["quote_amount"] for e in payload["position"]["entries"])
    assert spent == pytest.approx(84.0, abs=0.1)


CANDLE = "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 82320 (4h)"


def test_a_candle_close_stop_is_sized_on_its_backup_stop(tmp_path):
    """Relecture : un SL à la clôture peut vendre jusqu'au stop de secours (3 % plus bas par défaut) ; dimensionné
    sur le niveau nominal, la perte au secours valait 2,5 fois la cible."""
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", CANDLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    worker, commands = executor(tmp_path, inbox, enabled_preferences(signal_risk_sizing_enabled=True))
    assert worker.process_pending() == ["QUEUED"]
    payload = commands.list_recent("demo")[0]["payload"]
    sized = payload["route"]["metrics"]["risk_sizing"]
    backup = 82320 * 0.97
    assert sized["sizing_stop"] == pytest.approx(backup)
    budget = sized["budget"]
    assert budget * (84000 - backup) / 84000 == pytest.approx(4.0, abs=0.01)       # 0,4 % de 1 000 au secours


def test_a_candle_close_stop_without_backup_is_held(tmp_path):
    """Sans stop de secours, la perte d'un SL à la clôture n'a pas de borne : aucune taille, revue."""
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", CANDLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    preferences = enabled_preferences(signal_risk_sizing_enabled=True, signal_candle_backup_percent=0)
    worker, commands = executor(tmp_path, inbox, preferences)
    assert worker.process_pending() == ["REVIEW"] and commands.list_recent("demo") == []
    assert '"D_RISK_SIZING"' in inbox.recent("demo")[0]["route"]


def test_risk_size_follows_the_entry_split_actually_applied(tmp_path):
    two = "PAIR: BTC/USDT\nENTRY 1: 84000\nENTRY 2: 82000\nT1: 90000\nSL: 80000"
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", two, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    preferences = enabled_preferences(signal_risk_sizing_enabled=True, signal_auto_entry_count=2,
                                      signal_auto_entry_distribution="CUSTOM",
                                      signal_auto_entry_custom_percentages="20;80")
    worker, commands = executor(tmp_path, inbox, preferences)
    assert worker.process_pending() == ["QUEUED"]
    payload = commands.list_recent("demo")[0]["payload"]
    entries = payload["position"]["entries"]
    loss = sum(e["quote_amount"] / e["resolved_price"] * (e["resolved_price"] - 80000) for e in entries)
    assert loss == pytest.approx(4.0, abs=0.05)                                       # parts 20/80 réellement suivies


@pytest.mark.parametrize("action,min_loss,expected_budget", [
    ("REDUCE", 0.0, 45.0),            # perdant : la moitié du budget, signal envoyé
    ("REDUCE", 1_000.0, 90.0),        # perte sous le seuil choisi : aucun effet
])
def test_a_losing_trader_can_get_a_smaller_size(tmp_path, rules, action, min_loss, expected_budget):  # noqa: F811
    losers = [labelled(losing_position(rules), "Canal A") for _ in range(10)]
    preferences = enabled_preferences(signal_channel_review_enabled=True, signal_channel_review_min_trades=10,
                                      signal_channel_action=action, signal_channel_kept_percent=50,
                                      signal_channel_min_loss=min_loss)
    outcome, payload = _queued_metrics(tmp_path, preferences, positions=positions_stub(losers), origin="Canal A")
    assert outcome == ["QUEUED"]
    assert payload["route"]["metrics"]["budget"] == pytest.approx(expected_budget)
    reduction = payload["route"]["metrics"].get("channel_reduction")
    assert bool(reduction) == (expected_budget < 90)
    if reduction:
        assert "« Canal A » perdant" in reduction["detail"]


def test_the_reduction_applies_after_the_risk_size(tmp_path, rules):  # noqa: F811
    losers = [labelled(losing_position(rules), "Canal A") for _ in range(10)]
    preferences = enabled_preferences(signal_risk_sizing_enabled=True, signal_channel_review_enabled=True,
                                      signal_channel_review_min_trades=10, signal_channel_action="REDUCE")
    outcome, payload = _queued_metrics(tmp_path, preferences, positions=positions_stub(losers), origin="Canal A")
    metrics = payload["route"]["metrics"]
    assert outcome == ["QUEUED"] and metrics["risk_sizing"]["budget"] == pytest.approx(84.0)
    assert metrics["budget"] == pytest.approx(42.0)


def test_unreadable_positions_still_hold_the_signal(tmp_path):
    outcome, saved = _queued_metrics(tmp_path, enabled_preferences(signal_channel_review_enabled=True),
                                     positions=positions_stub(read_errors=["illisible"]))
    assert outcome == ["REVIEW"] and '"D_RISK"' in saved["route"]


def test_the_review_action_still_holds_a_losing_trader(tmp_path, rules):  # noqa: F811
    losers = [labelled(losing_position(rules), "Canal A") for _ in range(10)]
    preferences = enabled_preferences(signal_channel_review_enabled=True, signal_channel_review_min_trades=10)
    outcome, saved = _queued_metrics(tmp_path, preferences, positions=positions_stub(losers), origin="Canal A")
    assert outcome == ["REVIEW"] and '"C_CHANNEL_LOSING"' in saved["route"]


class RiskCsi:
    """Client CSI factice : avis favorable et conseil de risque à 24 h."""

    def __init__(self, risk=None, *, fail=False):
        self._risk, self.fail, self.risk_calls = risk, fail, 0

    def evaluate(self, text, *, source, record=True, user_validated=False):
        return CsiOpinion(verdict="FAVORABLE", summary="ok", source=source, evaluated_at="2026-10-06T00:00:00+00:00")

    def risk(self):
        self.risk_calls += 1
        if self.fail:
            raise CsiUnavailable("CSI injoignable")
        return self._risk


ADVICE = {"available": True, "origin": "2026-10-06T00:00:00+00:00",
          "pairs": {"BTCUSDT": {"move_24h_pct": 2.5, "relative_size": 1.6, "stop_floor_pct": 2.5}}}


def test_csi_size_advice_is_shown_never_applied(tmp_path):
    csi = RiskCsi(ADVICE)
    _, payload = _queued_metrics(tmp_path, enabled_preferences(), csi_client=csi)
    advice = payload["route"]["metrics"]["csi_size"]
    assert advice["available"] and advice["proposed_budget"] == pytest.approx(144.0)      # 90 × 1,6
    assert payload["route"]["metrics"]["budget"] == 90                                    # rien n'est appliqué
    assert advice["stop_inside_move"] is False                                            # stop ≈ 5,3 % > 2,5 %
    assert "non appliqué" in size_advice_text(advice)


@pytest.mark.parametrize("risk", [
    None, {"available": True, "pairs": ["BTCUSDT"]},
    {"available": True, "pairs": {"BTCUSDT": {"move_24h_pct": float("nan"), "relative_size": 1.2}}},
    {"available": True, "pairs": {"BTCUSDT": {"move_24h_pct": "x", "relative_size": 1.2}}},
], ids=["vide", "paires-en-liste", "nan", "texte"])
def test_a_malformed_csi_answer_never_holds_a_signal(tmp_path, risk):
    outcome, payload = _queued_metrics(tmp_path, enabled_preferences(), csi_client=RiskCsi(risk))
    assert outcome == ["QUEUED"] and payload["route"]["metrics"]["csi_size"]["available"] is False


def test_an_unavailable_csi_never_blocks_a_signal_and_is_asked_once_a_minute(tmp_path):
    csi = RiskCsi(fail=True)
    inbox = SignalInbox(tmp_path / "signals.db")
    for message in (1, 2):
        inbox.receive("demo", SIMPLE.replace("84000", str(84000 - message)), source="telegram",
                      external_id=telegram_id(message), source_timestamp=995)
    worker, commands = executor(tmp_path, inbox, enabled_preferences(signal_risk_sizing_enabled=False),
                                csi_client=csi)
    assert worker.process_pending() == ["QUEUED"]                        # une panne de CSI ne retient rien
    worker.process_pending()                                             # second signal, même minute
    assert csi.risk_calls == 1                                           # conseil mis en cache une minute
    assert commands.list_recent("demo")[-1]["payload"]["route"]["metrics"]["csi_size"]["available"] is False


def test_size_advice_reads_only_known_pairs():
    assert size_advice(None, "BTCUSDT", 90, 3.0)["available"] is False
    assert "absente" in size_advice(ADVICE, "ETHUSDT", 90, 3.0)["reason"]
    stale = {"available": False, "reason": "prévision périmée"}
    assert size_advice(stale, "BTCUSDT", 90, 3.0)["reason"] == "prévision périmée"
    inside = size_advice(ADVICE, "btcusdt", 90, 1.5)
    assert inside["stop_inside_move"] is True and "à l'intérieur" in size_advice_text(inside)


# -- raison de clôture --------------------------------------------------------------------------------------------

def test_a_stuck_close_keeps_the_reason_it_was_asked_for(setup):  # noqa: F811
    """Ancienne relecture (mineur 1) : un stop franchi dont le prix devient illisible après les annulations ne
    finit plus en « tous les TP »."""
    position, _, store, execution, _, _ = setup
    with_dust(position)

    def no_price(_symbol):
        raise TimeoutError("prix indisponible")

    execution.client.get_price = no_price
    with pytest.raises(TimeoutError):
        close_market(position, execution, store, reason=CloseReason.STOP_CROSSED)
    position = store.load(position.position_id)
    assert position.status is PositionStatus.CLOSING and position.closing_reason is CloseReason.STOP_CROSSED
    assert poll_market_close(position, execution, 84000)
    assert not position.is_open and position.close_reason is CloseReason.STOP_CROSSED


# -- réglages ------------------------------------------------------------------------------------------------------

def test_settings_save_the_risk_size_and_the_trader_rule(monkeypatch, tmp_path):
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
    app.toggle(key="signal_risk_sizing_toggle").set_value(True)
    app.number_input(key="signal_risk_percent_input").set_value(0.5)
    next(b for b in app.button if b.label == "Enregistrer la taille selon le risque").click().run()
    assert not app.exception
    app.toggle(key="signal_channel_review_toggle").set_value(True)
    app.radio(key="signal_channel_action_choice").set_value("REDUCE")
    app.number_input(key="signal_channel_min_loss_input").set_value(20.0)
    next(b for b in app.button if b.label == "Enregistrer la règle du trader ou canal").click().run()
    assert not app.exception
    saved = store.load()
    assert saved["signal_risk_sizing_enabled"] is True and saved["signal_risk_percent"] == 0.5
    assert saved["signal_channel_action"] == "REDUCE" and saved["signal_channel_min_loss"] == 20.0


# -- page Signaux : même calcul que l'exécution automatique ------------------------------------------------------

def _open_signals_page(monkeypatch, tmp_path, text, preferences, positions=()):
    from types import SimpleNamespace

    from streamlit.testing.v1 import AppTest

    import binance_spot_manager.config as config
    import binance_spot_manager.position_store as position_store
    import binance_spot_manager.signal_inbox as signal_inbox
    import ui_common
    from binance_spot_manager.command_store import CommandStore, account_scope
    from binance_spot_manager.config import RunMode, Settings
    from test_signals_ui import PAGE, RULES, page_service

    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    scope = account_scope(settings)
    inbox = SignalInbox(tmp_path / "inbox.db")
    row = inbox.receive(scope, text, source="telegram", external_id=f"{'ab' * 32}:-100:1", source_timestamp=995,
                        origin="Canal A")
    service = page_service(scope, CommandStore(tmp_path / "commands.db"), tmp_path)
    service.positions = SimpleNamespace(list_all=lambda: list(positions), read_errors=[])
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(signal_inbox, "SignalInbox", lambda: inbox)
    monkeypatch.setattr(ui_common, "get_service", lambda: service)
    monkeypatch.setattr(ui_common, "load_rules", lambda symbol: (RULES, ""))
    monkeypatch.setattr(ui_common, "sidebar_status", lambda settings: None)
    monkeypatch.setattr(position_store, "get_settings_store", lambda: SimpleNamespace(load=lambda: preferences))
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception
    return app, row


def test_the_page_proposes_the_risk_size_reduced_for_a_losing_trader(monkeypatch, tmp_path, rules):  # noqa: F811
    losers = [labelled(losing_position(rules), "Canal A") for _ in range(10)]
    preferences = {"signal_risk_sizing_enabled": True, "signal_channel_review_enabled": True,
                   "signal_channel_review_min_trades": 10, "signal_channel_action": "REDUCE"}
    app, row = _open_signals_page(monkeypatch, tmp_path, SIMPLE, preferences, losers)
    assert app.number_input(key=f"budget_{row['id']}").value == pytest.approx(42.0)     # 84 réduit de moitié
    assert any("« Canal A » perdant" in w.value for w in app.warning)


def test_the_page_sizes_a_candle_stop_on_its_backup(monkeypatch, tmp_path):
    app, row = _open_signals_page(monkeypatch, tmp_path, CANDLE, {"signal_risk_sizing_enabled": True})
    backup = 82320 * 0.97
    budget = app.number_input(key=f"budget_{row['id']}").value
    assert budget * (84000 - backup) / 84000 == pytest.approx(4.0, abs=0.01)


# -- relecture de contrôle --------------------------------------------------------------------------------------

def test_the_risk_check_measures_a_candle_stop_at_its_backup(tmp_path):
    """Hors réglage de taille : avec un budget fixe, le contrôle « risque au stop » (R1) d'un SL à la clôture se
    mesure au stop de secours, là où la vente peut se faire (avant : au niveau nominal, perte sous-estimée)."""
    folders = {}
    for name, backup in (("secours", 3.0), ("sans", 0.0)):
        folder = tmp_path / name
        folder.mkdir()
        inbox = SignalInbox(folder / "signals.db")
        inbox.receive("demo", CANDLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
        worker, commands = executor(folder, inbox, enabled_preferences(signal_fixed_budget=200,
                                                                       signal_candle_backup_percent=backup))
        folders[name] = (worker.process_pending(), inbox.recent("demo")[0])
    outcome, saved = folders["secours"]
    assert outcome == ["REVIEW"] and '"R1_RISK_AT_STOP"' in saved["route"]           # ≈ 1 % au secours > 0,5 %
    assert folders["sans"][0] == ["QUEUED"]                                           # nominal 2 % : ≈ 0,4 %


def test_the_automatic_touch_setting_sizes_on_the_nominal_stop(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", CANDLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    worker, commands = executor(tmp_path, inbox, enabled_preferences(signal_risk_sizing_enabled=True,
                                                                     signal_auto_touch_stop=True))
    assert worker.process_pending() == ["QUEUED"]
    sized = commands.list_recent("demo")[0]["payload"]["route"]["metrics"]["risk_sizing"]
    assert sized["sizing_stop"] == pytest.approx(82320)                                # stop au toucher


def test_nested_metrics_are_cleaned_for_strict_json():
    import json

    from binance_spot_manager.signal_routing import clean_metrics

    cleaned = clean_metrics({"csi_size": {"available": True, "origin": float("nan"), "inner": {"x": float("inf")}}})
    assert cleaned["csi_size"]["origin"] is None and cleaned["csi_size"]["inner"]["x"] is None
    json.dumps(cleaned, allow_nan=False)


def test_flags_must_be_real_booleans_and_bad_entries_give_no_size():
    assert not RiskSizingPolicy.from_mapping({"signal_risk_sizing_enabled": "false"}).enabled
    assert not ChannelPolicy.from_mapping({"signal_channel_review_enabled": "true"}).enabled
    assert average_entry_price([100.0, 0.0], [50, 50]) == 0.0
    assert risk_based_budget(ON, entries=[100.0, float("nan")], stop=90.0, total_capital=1000, usable_quote=800) is None


def test_the_page_recalculates_the_budget_when_the_stop_trigger_changes(monkeypatch, tmp_path):
    app, row = _open_signals_page(monkeypatch, tmp_path, CANDLE, {"signal_risk_sizing_enabled": True})
    on_backup = app.number_input(key=f"budget_{row['id']}").value
    app.radio(key=f"stop_mode_{row['id']}").set_value(app.radio(key=f"stop_mode_{row['id']}").options[1]).run()
    assert not app.exception
    at_touch = app.number_input(key=f"budget_{row['id']}").value
    assert at_touch > on_backup
    assert at_touch * (84000 - 82320) / 84000 == pytest.approx(4.0, abs=0.01)         # sur le stop au toucher
