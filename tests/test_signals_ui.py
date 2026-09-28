"""Exercise the user path without credentials, network, or real commands."""
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from binance_spot_manager.command_store import CommandStore, account_scope
from binance_spot_manager.config import Settings, RunMode
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.symbol_rules import parse_symbol_rules
import binance_spot_manager.config as config
import binance_spot_manager.signal_inbox as signal_inbox
import ui_common


PAGE = Path(__file__).resolve().parents[1] / "pages" / "8_Signaux.py"
SIGNAL = "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000"


def test_manual_signal_preview_confirmation_and_deduplication(monkeypatch, tmp_path):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    scope = account_scope(settings)
    inbox = SignalInbox(tmp_path / "inbox.db")
    commands = CommandStore(tmp_path / "commands.db")
    rules = parse_symbol_rules({"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"}, {"filterType": "NOTIONAL", "minNotional": "5"}]})
    service = SimpleNamespace(
        find_by_symbol=lambda symbol: None, commands=commands,
        client=SimpleNamespace(get_balances=lambda: {"USDT": {"free": 1000}}),
        current_price=lambda symbol: 84500, risk_limits=lambda: SimpleNamespace(min_reserve_percent=20),
        runtime=lambda: SimpleNamespace(command_capabilities=["signal_v1"]),
        worker_status=lambda: SimpleNamespace(running=True, heartbeat_age=0),
        submit_command=lambda action, payload, request_key: commands.enqueue(scope, action, payload, request_key=request_key),
    )
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
    assert len(app.dataframe) == 2
    assert commands.list_recent(scope) == []
    next(c for c in app.checkbox if "Je confirme" in c.label).check().run()
    next(b for b in app.button if b.label == "Transmettre au worker Demo").click().run()
    assert not app.exception
    assert len(commands.list_recent(scope)) == 1
    assert commands.list_recent(scope)[0]["state"] == "PENDING"
    next(b for b in app.button if b.label == "Analyser et enregistrer").click().run()
    assert not app.exception
    assert len(commands.list_recent(scope)) == 1
    assert not any(b.label == "Transmettre au worker Demo" for b in app.button)


def test_existing_unrecognized_signal_can_be_reanalysed_from_page(monkeypatch, tmp_path):
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
    assert app.error
    next(b for b in app.button if b.label == "Réanalyser ce signal").click().run()
    assert not app.exception
    assert not app.error
    assert any("clôture 1h" in c.label for c in app.checkbox)
    saved = inbox.recent(account_scope(settings))
    assert len(saved) == 1
    assert saved[0]["id"] == row["id"]
    assert saved[0]["payload"] is None
