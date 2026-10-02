"""Page CSI : vérifier un signal, signaux trouvés par CSI, « Tester en Demo », panne de CSI sans casse."""
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

import binance_spot_manager.config as config
import binance_spot_manager.csi_client as csi_client
import binance_spot_manager.signal_inbox as signal_inbox
import ui_common
from binance_spot_manager.command_store import CommandStore, account_scope
from binance_spot_manager.config import RunMode, Settings
from binance_spot_manager.csi_client import CsiOpinion
from binance_spot_manager.signal_inbox import SignalInbox

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "9_CSI.py"
SIGNAL = "PAIR: ETH/USDT\nENTRY 1: 2687\nT1: 2738\nT2: 2792\nSL: 2644"
FOUND = {
    "signal_id": "CSI-20260930T161530Z-88062BDC", "symbol": "BTCUSDT", "strategy": "EMA_PULLBACK_CONTINUATION",
    "created_at": "2026-09-30T16:15:30+00:00", "entry_expires_at": "2099-01-01T16:45:02+00:00", "expired": False,
    "entry": "84446.35", "stop_loss": "83598.12", "targets": ["86142.81"], "trend_regime": "BULL",
    "volatility_regime": "HIGH", "strategy_verdict": "REJECTED", "strategy_expectancy_r": -0.16,
    "bsm_text": "PAIR: BTC/USDT\nENTRY 1: 84446.35\nT1: 86142.81\nSL: 83598.12\nPLATFORM: Binance",
}


class FakeClient:
    base_url = "http://csi-api:8503"
    evaluations = []
    reachable = True
    found = []

    @classmethod
    def from_env(cls):
        return cls()

    def probe(self):
        return ({"ready": True, "detail": "prêt"}, "") if self.reachable \
            else (None, "CSI injoignable sur http://csi-api:8503 (ConnectionError)")

    def evaluate(self, text, *, source, record=True, user_validated=False):
        self.evaluations.append((text, source, record, user_validated))
        return CsiOpinion(verdict="DEFAVORABLE", summary="Défavorable : aucun veto, mais la même géométrie perd.",
                          source=source, evaluated_at="2026-09-30T10:00:00+00:00",
                          raw={"checks": [{"label": "lecture du signal", "ok": True, "detail": "structured"}]})

    def generated(self, limit=20):
        return list(self.found)

    def sources(self):
        return {"sources": [{"source": "Suhaib", "evaluated": 9, "resolved": 7, "edge_r": 0.1,
                             "conclusion": "trop peu de signaux résolus (7 < 20) : aucune conclusion"}],
                "min_resolved": 20, "min_days": 10,
                "rule": "aucune conclusion avant 20 signaux résolus sur au moins 10 jours"}

    def strategies(self):
        return [{"strategy": "EMA_PULLBACK_CONTINUATION", "verdict": "REJECTED", "expectancy_r": -0.16,
                 "trades_closed": 3672}]

    def recent(self, limit=20):
        return []


def patch(monkeypatch, tmp_path, *, reachable=True, found=()):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    inbox = SignalInbox(tmp_path / "inbox.db")
    commands = CommandStore(tmp_path / "commands.db")
    FakeClient.evaluations, FakeClient.reachable, FakeClient.found = [], reachable, list(found)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(csi_client, "CsiClient", FakeClient)
    monkeypatch.setattr(signal_inbox, "SignalInbox", lambda *a, **k: inbox)
    service = SimpleNamespace(
        commands=commands, client=SimpleNamespace(get_balances=lambda: {"USDT": {"free": 1000}},
                                                  get_prices=lambda: {"BTCUSDT": 84500}),
        current_price=lambda symbol: 84500, risk_limits=lambda: SimpleNamespace(min_reserve_percent=20),
        runtime=lambda: SimpleNamespace(command_capabilities=["signal_v1", "independent_positions_v1"]),
        worker_status=lambda: SimpleNamespace(running=True, heartbeat_age=0),
    )
    monkeypatch.setattr(ui_common, "get_service", lambda: service)
    monkeypatch.setattr(ui_common, "sidebar_status", lambda settings: None)
    return settings, inbox, commands


def test_verify_a_pasted_signal_in_one_click(monkeypatch, tmp_path):
    patch(monkeypatch, tmp_path)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    assert not app.exception
    assert any("CSI fonctionne" in s.value for s in app.success)
    app.text_area[0].set_value(SIGNAL)
    app.text_input[0].set_value("Suhaib")
    next(b for b in app.button if b.label == "Vérifier").click().run()
    assert not app.exception
    assert FakeClient.evaluations == [(SIGNAL, "Suhaib", True, True)]      # collé ici : validé par toi
    assert any("Déconseillé" in e.value for e in app.error)
    assert any("Aucun signal trouvé" in c.value for c in app.caption)
    groups = next(df.value for df in app.dataframe if "Groupe" in df.value.columns)
    assert groups.iloc[0]["Résolus"] == "7/20" and groups.iloc[0]["Vers une conclusion"] == 0.35


def test_found_signal_can_be_sent_to_the_signal_page_for_a_manual_demo_test(monkeypatch, tmp_path):
    settings, inbox, commands = patch(monkeypatch, tmp_path, found=[FOUND, FOUND | {"signal_id": "old", "expired": True}])
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    app.switch_page("pages/9_CSI.py").run()
    assert not app.exception
    assert any("non validées" in w.value for w in app.warning)
    buttons = [b for b in app.button if b.label == "Tester en Demo"]
    assert len(buttons) == 2 and not buttons[0].disabled and buttons[1].disabled      # expiré : grisé
    buttons[0].click().run()
    assert not app.exception
    rows = inbox.recent(account_scope(settings))
    assert len(rows) == 1 and rows[0]["parsed"]["symbol"] == "BTCUSDT" and rows[0]["source"] == "manual"
    assert app.session_state["selected_signal"] == rows[0]["id"]
    assert commands.list_recent(account_scope(settings)) == []                       # aucun ordre : à confirmer


def test_page_survives_an_unreachable_csi(monkeypatch, tmp_path):
    patch(monkeypatch, tmp_path, reachable=False)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    assert not app.exception
    assert any("ne répond pas" in e.value for e in app.error)
    assert not app.button
