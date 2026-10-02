"""Exercise the user path without credentials, network, or real commands."""
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from binance_spot_manager.command_store import CommandStore, account_scope
from binance_spot_manager.config import Settings, RunMode
from binance_spot_manager.event_store import EventStore
from binance_spot_manager.risk_engine import RiskLimits
from binance_spot_manager.csi_client import CsiOpinion
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.symbol_rules import parse_symbol_rules
import binance_spot_manager.config as config
import binance_spot_manager.csi_client as csi_client
import binance_spot_manager.signal_inbox as signal_inbox
import binance_spot_manager.position_store as position_store
import ui_common


PAGE = Path(__file__).resolve().parents[1] / "pages" / "8_Signaux.py"
RULES = parse_symbol_rules({"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
    "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"}, {"filterType": "NOTIONAL", "minNotional": "5"}]})


def page_service(scope, commands, tmp_path, *, price=84500):
    """Service minimal de la page : mêmes lectures que le worker, sans réseau."""
    return SimpleNamespace(
        find_by_symbol=lambda symbol: None, commands=commands,
        client=SimpleNamespace(get_balances=lambda: {"USDT": {"free": 1000}},
                               get_prices=lambda: {"BTCUSDT": price}),
        positions=SimpleNamespace(list_all=lambda: [], read_errors=[]),
        events=EventStore(tmp_path / "events.jsonl"),
        current_price=lambda symbol: price, risk_limits=lambda: RiskLimits(),
        runtime=lambda: SimpleNamespace(command_capabilities=["signal_v1", "independent_positions_v1"]),
        worker_status=lambda: SimpleNamespace(running=True, heartbeat_age=0),
        submit_command=lambda action, payload, request_key: commands.enqueue(scope, action, payload, request_key=request_key),
    )
SIGNAL = "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000"


def test_manual_signal_preview_confirmation_and_deduplication(monkeypatch, tmp_path):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    scope = account_scope(settings)
    inbox = SignalInbox(tmp_path / "inbox.db")
    commands = CommandStore(tmp_path / "commands.db")
    rules = parse_symbol_rules({"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"}, {"filterType": "NOTIONAL", "minNotional": "5"}]})
    service = page_service(scope, commands, tmp_path)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(signal_inbox, "SignalInbox", lambda: inbox)
    monkeypatch.setattr(ui_common, "get_service", lambda: service)
    monkeypatch.setattr(ui_common, "load_rules", lambda symbol: (rules, ""))
    monkeypatch.setattr(ui_common, "sidebar_status", lambda settings: None)
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception
    app.text_area[0].set_value(SIGNAL)
    next(b for b in app.button if b.label == "Analyser et enregistrer").click().run()
    assert not app.exception
    assert len(inbox.recent(scope)) == 1
    assert commands.list_recent(scope) == []
    app.number_input[0].set_value(200)
    next(c for c in app.checkbox if "date source" in c.label).check().run()
    next(b for b in app.button if "simuler" in b.label).click().run()
    assert not app.exception
    assert not app.error
    assert len(app.dataframe) == 3  # entrées, TP et panneau de risque
    assert commands.list_recent(scope) == []
    # 200 USDT ≈ 1 % au stop frais compris : risque élevé, acquittement obligatoire.
    transmit = next(b for b in app.button if b.label == "Transmettre au worker Demo")
    next(c for c in app.checkbox if c.label.startswith("Je confirme ces achats")).check().run()
    assert next(b for b in app.button if b.label == "Transmettre au worker Demo").disabled
    assert transmit is not None
    next(c for c in app.checkbox if c.label.startswith("Je confirme malgré un risque élevé")).check().run()
    next(b for b in app.button if b.label == "Transmettre au worker Demo").click().run()
    assert not app.exception
    assert len(commands.list_recent(scope)) == 1
    assert commands.list_recent(scope)[0]["state"] == "PENDING"
    next(b for b in app.button if b.label == "Analyser et enregistrer").click().run()
    assert not app.exception
    assert len(commands.list_recent(scope)) == 1
    assert not any(b.label == "Transmettre au worker Demo" for b in app.button)


def test_signal_refused_by_an_older_parser_is_re_read_when_the_page_opens(monkeypatch, tmp_path):
    import json
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    inbox = SignalInbox(tmp_path / "inbox.db")
    raw = "#GALA/USDT\nEntry1: 0.002116\nTP1: 0.002210 (4.44%)\nStop: 0.002032(1h)"
    row = inbox.receive(account_scope(settings), raw)
    legacy = dict(row["parsed"], template="simple", direction="", entries=[], stop=None,
                  errors=["Direction d'achat non identifiable."])
    with inbox.connect() as db, db:
        db.execute("UPDATE signals SET parsed=? WHERE id=?", (json.dumps(legacy), row["id"]))
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(signal_inbox, "SignalInbox", lambda: inbox)
    monkeypatch.setattr(ui_common, "get_service", lambda: SimpleNamespace())
    monkeypatch.setattr(ui_common, "sidebar_status", lambda settings: None)
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception
    assert not app.error
    stop_mode = app.radio[0]
    assert stop_mode.label == "Déclenchement du SL"
    assert "clôture d'une bougie 1h" in stop_mode.value  # par défaut : comme le signal
    next(b for b in app.button if b.label == "Réanalyser ce signal").click().run()
    assert not app.exception
    assert not app.error
    saved = inbox.recent(account_scope(settings))
    assert len(saved) == 1
    assert saved[0]["id"] == row["id"]
    assert saved[0]["payload"] is None


def test_automatic_telegram_reader_hides_competing_manual_getupdates(monkeypatch, tmp_path):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    inbox = SignalInbox(tmp_path / "inbox.db")
    preferences = {
        "signal_telegram_enabled": True,
        "signal_telegram_auto_enabled": True,
        "signal_telegram_chats": "99",
    }
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(signal_inbox, "SignalInbox", lambda: inbox)
    monkeypatch.setattr(
        position_store, "get_settings_store",
        lambda: SimpleNamespace(load=lambda: preferences),
    )
    monkeypatch.setattr(
        ui_common, "get_service",
        lambda: SimpleNamespace(runtime=lambda: SimpleNamespace(
            telegram_diagnostics={"state": "POLLING", "last_error": ""},
        )),
    )
    monkeypatch.setattr(ui_common, "sidebar_status", lambda settings: None)

    app = AppTest.from_file(str(PAGE)).run()

    assert not app.exception
    labels = [button.label for button in app.button]
    assert "Actualiser la boîte de réception" in labels
    assert "Relever les messages Telegram" not in labels



# ==========================================================================
# Routage : « À confirmer », motifs, acquittements, panneau de risque
# ==========================================================================


def open_page(monkeypatch, tmp_path, inbox, *, preferences=None):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    scope = account_scope(settings)
    commands = CommandStore(tmp_path / "commands.db")
    service = page_service(scope, commands, tmp_path)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(signal_inbox, "SignalInbox", lambda: inbox)
    monkeypatch.setattr(ui_common, "get_service", lambda: service)
    monkeypatch.setattr(ui_common, "load_rules", lambda symbol: (RULES, ""))
    monkeypatch.setattr(ui_common, "sidebar_status", lambda settings: None)
    monkeypatch.setattr(position_store, "get_settings_store",
                        lambda: SimpleNamespace(load=lambda: preferences or {}))
    return AppTest.from_file(str(PAGE)).run(), scope, commands, service


def review(inbox, scope, text, reasons):
    import json
    row = inbox.receive(scope, text, source="telegram", external_id=f"{'ab' * 32}:-100:{len(text)}",
                        source_timestamp=995)
    inbox.set_auto_state(scope, row["id"], "REVIEW", " · ".join(r["message"] for r in reasons),
                         route=json.dumps({"outcome": "REVIEW", "reasons": reasons}))
    return row


def simulate(app, budget):
    app.number_input[0].set_value(budget)
    next(c for c in app.checkbox if "date source" in c.label).check().run()
    next(b for b in app.button if "simuler" in b.label).click().run()


def test_review_filter_reasons_acknowledgements_and_manual_payload(monkeypatch, tmp_path):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    scope = account_scope(settings)
    inbox = SignalInbox(tmp_path / "inbox.db")
    inbox.receive(scope, SIGNAL.replace("T1: 90000", "T1: 95000"))  # collage manuel, non routé
    row = review(inbox, scope, SIGNAL, [
        {"code": "C_SOURCE_UNDECLARED", "category": "CONFIANCE", "message": "Groupe Telegram non déclaré de confiance"}])
    app, scope, commands, service = open_page(monkeypatch, tmp_path, inbox)
    assert not app.exception
    toggle = app.toggle(key="signal_review_filter")
    assert toggle.label == "Seulement à confirmer (1)"
    toggle.set_value(True).run()
    chooser = next(s for s in app.selectbox if s.label == "Signal à examiner")
    assert len(chooser.options) == 1 and chooser.value == row["id"]
    assert chooser.options[0].startswith("À confirmer · BTCUSDT")
    assert any("Groupe Telegram non déclaré de confiance" in m.value for m in app.markdown)

    simulate(app, 90)
    assert not app.exception
    next(c for c in app.checkbox if c.label.startswith("Je confirme ces achats")).check().run()
    assert next(b for b in app.button if b.label == "Transmettre au worker Demo").disabled
    next(c for c in app.checkbox if c.label.startswith("Je confirme malgré une confiance faible")).check().run()
    next(b for b in app.button if b.label == "Transmettre au worker Demo").click().run()
    assert not app.exception
    queued = commands.list_recent(scope)
    assert len(queued) == 1 and queued[0]["state"] == "PENDING"
    payload = queued[0]["payload"]
    assert payload["confirmation_mode"] == "MANUAL"
    assert payload["acknowledged_reason_codes"] == ["C_SOURCE_UNDECLARED"]
    assert any(e["event"] == "SIGNAL_MANUAL_CONFIRMED" for e in service.events.tail(10))


def test_hard_limit_refusal_disables_transmission(monkeypatch, tmp_path):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    inbox = SignalInbox(tmp_path / "inbox.db")
    inbox.receive(account_scope(settings), SIGNAL)
    app, scope, commands, _ = open_page(monkeypatch, tmp_path, inbox)
    simulate(app, 250)  # 1,19 % hors frais > 1 % : le worker refuserait
    assert any("le worker refusera" in e.value for e in app.error)
    for box in app.checkbox:
        if box.label.startswith("Je confirme"):
            box.check()
    app.run()
    assert next(b for b in app.button if b.label == "Transmettre au worker Demo").disabled
    assert commands.list_recent(scope) == []


def test_legacy_rejected_row_stays_confirmable(monkeypatch, tmp_path):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    scope = account_scope(settings)
    inbox = SignalInbox(tmp_path / "inbox.db")
    row = inbox.receive(scope, SIGNAL, source="telegram", external_id="bot:1", source_timestamp=995)
    inbox.set_auto_state(scope, row["id"], "REJECTED", "Ancien refus automatique")
    app, scope, commands, _ = open_page(monkeypatch, tmp_path, inbox)
    assert any("Ancien refus automatique" in w.value for w in app.warning)
    simulate(app, 90)
    next(c for c in app.checkbox if c.label.startswith("Je confirme ces achats")).check().run()
    next(b for b in app.button if b.label == "Transmettre au worker Demo").click().run()
    assert not app.exception
    assert len(commands.list_recent(scope)) == 1


def test_csi_row_past_expiry_cannot_be_prepared(monkeypatch, tmp_path):
    from test_signal_csi import csi_text, receive_csi

    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    inbox = SignalInbox(tmp_path / "inbox.db")
    receive_csi(inbox, csi_text(VALIDATION_STATUS="RESEARCH"), scope=account_scope(settings))
    app, scope, commands, _ = open_page(monkeypatch, tmp_path, inbox)
    assert not app.exception
    assert any("EXPIRES_AT dépassé" in e.value for e in app.error)
    assert not any("simuler" in b.label for b in app.button)


def test_signal_page_asks_csi_opinion_and_keeps_it_without_any_order(monkeypatch, tmp_path):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    scope = account_scope(settings)
    inbox = SignalInbox(tmp_path / "inbox.db")
    commands = CommandStore(tmp_path / "commands.db")
    inbox.receive(scope, SIGNAL, source="telegram", external_id="bothash:-100123:7", source_timestamp=1)
    calls = []

    class FakeClient:
        @classmethod
        def from_env(cls):
            return cls()

        def evaluate(self, text, *, source, record=True, user_validated=False):
            calls.append((text, source, record, user_validated))
            return CsiOpinion(verdict="INDETERMINE", summary="Indéterminé : pas assez d'éléments.",
                              source=source, evaluated_at="2026-09-30T10:00:00+00:00")

    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(signal_inbox, "SignalInbox", lambda: inbox)
    monkeypatch.setattr(csi_client, "CsiClient", FakeClient)
    monkeypatch.setattr(position_store, "get_settings_store",
                        lambda: SimpleNamespace(load=lambda: {"signal_csi_source_names": "-100123=Suhaib"}))
    monkeypatch.setattr(ui_common, "get_service", lambda: SimpleNamespace(commands=commands))
    monkeypatch.setattr(ui_common, "sidebar_status", lambda settings: None)

    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception
    assert any("Pas encore d'avis CSI" in c.value for c in app.caption)
    next(b for b in app.button if b.label == "Demander l'avis de CSI").click().run()
    assert not app.exception
    assert calls == [(SIGNAL, "Suhaib", True, False)]           # reçu par Telegram : pas une validation
    saved = inbox.recent(scope)[0]
    assert saved["csi_verdict"] == "INDETERMINE" and "pas assez" in saved["csi_detail"]
    assert any("Indéterminé" in m.value for m in app.markdown)
    assert any(b.label == "Actualiser l'avis de CSI" for b in app.button)
    assert commands.list_recent(scope) == []
