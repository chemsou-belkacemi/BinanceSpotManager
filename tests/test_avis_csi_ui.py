"""Page Avis CSI : évaluation affichée, panne de CSI signalée sans casser la page, aucun ordre."""
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

import binance_spot_manager.config as config
import binance_spot_manager.csi_client as csi_client
import ui_common
from binance_spot_manager.config import RunMode, Settings
from binance_spot_manager.csi_client import CsiOpinion, CsiUnavailable

PAGE = Path(__file__).resolve().parents[1] / "pages" / "9_Avis_CSI.py"
SIGNAL = "PAIR: ETH/USDT\nENTRY 1: 2687\nT1: 2738\nT2: 2792\nSL: 2644"


class FakeClient:
    base_url = "http://csi-api:8503"
    evaluations = []
    reachable = True

    @classmethod
    def from_env(cls):
        return cls()

    def probe(self):
        return ({"ready": True, "detail": "prêt : cycle terminé il y a 60 s"}, "") if self.reachable \
            else (None, "CSI injoignable sur http://csi-api:8503 (ConnectionError)")

    def evaluate(self, text, *, source, record=True, user_validated=False):
        self.evaluations.append((text, source, record, user_validated))
        return CsiOpinion(
            verdict="DEFAVORABLE", summary="Défavorable : aucun veto, mais la même géométrie perd en moyenne.",
            source=source, evaluated_at="2026-09-30T10:00:00+00:00", record_id="EXT-1" if record else None,
            raw={"checks": [{"label": "lecture du signal", "ok": True, "detail": "modèle structured"}],
                 "base_rate": {"samples": 412, "tp_first": 0.37, "expectancy_r": -0.08,
                               "expectancy_r_ci95": [-0.2, 0.05], "regime": "BULL/NORMAL", "method": "LIMIT_ALIGNED_V2"},
                 "warnings": []},
        )

    def sources(self):
        return {"sources": [{"source": "Suhaib", "evaluated": 3, "resolved": 1, "tp1_real": 1.0, "tp1_base": 0.35,
                             "edge_r": 2.0, "edge_ci95": None, "conclusion": "trop peu de signaux résolus (1 < 20)"}],
                "rule": "aucune conclusion avant 20 signaux résolus sur au moins 10 jours"}

    def strategies(self):
        return [{"strategy": "DONCHIAN_VOLUME_BREAKOUT", "verdict": "REJECTED", "expectancy_r": -0.11,
                 "expectancy_r_ci95": [-0.19, -0.04], "trades_closed": 1920, "run_id": "WF-1"}]

    def recent(self, limit=20):
        return [{"received_at": "2026-09-30T09:00:00+00:00", "source": "Suhaib", "symbol": "ETHUSDT", "entry": 2687.0,
                 "stop": 2644.0, "tp1": 2738.0, "verdict": "DEFAVORABLE", "outcome": "PENDING", "outcome_r": None}]


def run_page(monkeypatch, *, reachable=True):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    FakeClient.evaluations = []
    FakeClient.reachable = reachable
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(csi_client.CsiClient, "from_env", classmethod(lambda cls: FakeClient()))
    monkeypatch.setattr(ui_common, "get_service", lambda: SimpleNamespace())
    monkeypatch.setattr(ui_common, "sidebar_status", lambda settings: None)
    return AppTest.from_file(str(PAGE), default_timeout=20).run()


def test_page_evaluates_a_pasted_signal_and_shows_the_verdict(monkeypatch):
    app = run_page(monkeypatch)
    assert not app.exception
    assert any("prête" in s.value for s in app.success)
    assert len(app.dataframe) == 3                       # groupes, stratégies, dernières évaluations
    app.text_input[0].set_value("Suhaib")
    app.text_area[0].set_value(SIGNAL)
    next(b for b in app.button if b.label == "Demander l'avis de CSI").click().run()
    assert not app.exception
    assert FakeClient.evaluations == [(SIGNAL, "Suhaib", True, True)]      # collé à la main : paire validée
    assert any("Défavorable" in e.value for e in app.error)
    assert any("probabilité" in c.value for c in [*app.caption, *app.info])


def test_page_survives_an_unreachable_csi(monkeypatch):
    app = run_page(monkeypatch, reachable=False)
    assert not app.exception
    assert any("injoignable" in w.value for w in app.warning)
    assert not app.dataframe and not app.button
