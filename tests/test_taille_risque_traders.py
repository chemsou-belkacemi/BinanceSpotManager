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


def test_a_signal_without_stop_is_held_when_risk_sizing_is_on(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000", source="telegram",
                  external_id=telegram_id(1), source_timestamp=995)
    worker, commands = executor(tmp_path, inbox, enabled_preferences(signal_risk_sizing_enabled=True))
    assert worker.process_pending() != ["QUEUED"] and commands.list_recent("demo") == []


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


def test_an_unavailable_csi_never_blocks_a_signal(tmp_path):
    outcome, payload = _queued_metrics(tmp_path, enabled_preferences(), csi_client=RiskCsi(fail=True))
    assert outcome == ["QUEUED"] and payload["route"]["metrics"]["csi_size"]["available"] is False


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
