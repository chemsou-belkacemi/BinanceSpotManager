"""Dépôt direct de signaux (générateur ML) : import, rejets, idempotence, gating."""
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from binance_spot_manager.position_store import JsonFileStore

from binance_spot_manager.models import SLRuleAfterTP
from binance_spot_manager.signal_drop import MAX_FILE_BYTES, SignalDropImporter
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.signal_parser import parse_signal
from binance_spot_manager.signal_plan import TRAIL_STOP_KEY, prepare_signal, signal_sl_after_tp
from binance_spot_manager.symbol_rules import parse_symbol_rules
from test_signal_auto_execution import SIMPLE, enabled_preferences, executor, telegram_id


TEXT = "PAIR: BTC/USDT\nPLATFORM: BINANCE\nENTRY 1: 84000\nT1: 86000\nT2: 88000\nSL: 82000"


def document(**overrides):
    return {"version": 1, "id": "ml-abc123", "producer": "mlsignals",
            "created_at": 995.0, "text": TEXT,
            "meta": {"probability": 0.63, "model_version": "v1"}} | overrides


def drop(directory, name, content):
    incoming = directory / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    path = incoming / name
    path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
    return path


def importer(tmp_path, preferences=None, inbox=None):
    inbox = inbox or SignalInbox(tmp_path / "signals.db")
    prefs = {"signal_drop_enabled": True} if preferences is None else preferences
    return SignalDropImporter(inbox, "demo", lambda: prefs, directory=tmp_path / "drop",
                              clock=lambda: 1000), inbox


def test_valid_file_is_imported_as_api_signal_and_moved(tmp_path):
    worker, inbox = importer(tmp_path)
    drop(tmp_path / "drop", "ml-abc123.json", document())
    drop(tmp_path / "drop", "ml-next.tmp", b"{partial")
    drop(tmp_path / "drop", "CSI-next.txt.tmp", b"SIGNAL_VERSION=2\npartial")
    drop(tmp_path / "drop", "notes.md", b"ignored")

    rows = worker.import_pending()

    assert len(rows) == 1
    row = inbox.recent("demo")[0]
    assert row["source"] == "api"
    assert row["external_id"] == "drop:ml-abc123"
    assert row["source_timestamp"] == 995.0
    assert row["parsed"]["symbol"] == "BTCUSDT"
    assert not row["parsed"]["errors"]
    assert (tmp_path / "drop" / "processed" / "ml-abc123.json").exists()
    # Écritures en cours (*.tmp, *.txt.tmp) et autres extensions : jamais lues.
    assert sorted(p.name for p in (tmp_path / "drop" / "incoming").iterdir()) == [
        "CSI-next.txt.tmp", "ml-next.tmp", "notes.md",
    ]
    snapshot = worker.snapshot()
    assert snapshot["state"] == "ACTIVE"
    assert snapshot["imported_total"] == 1
    assert snapshot["last_import_at"] == 1000


@pytest.mark.parametrize("content, reason", [
    (b"{not json", "JSON invalide"),
    (json.dumps([1, 2]).encode(), "Objet JSON"),
    (document(version=2), "Version"),
    (document(version=True), "Version"),
    (document(id="ml abc"), "Identifiant"),
    (document(id="x" * 101), "Identifiant"),
    (document(created_at="1790000000"), "created_at"),
    (document(created_at=float("inf")), "created_at"),
    (document(text=""), "Texte"),
    (b"{" + b" " * (MAX_FILE_BYTES + 1) + b"}", "64 Ko"),
], ids=["json", "array", "version", "bool-version", "id-space", "id-long", "created-str",
        "created-inf", "text", "oversize"
])
def test_invalid_files_are_rejected_with_reason(tmp_path, content, reason):
    worker, inbox = importer(tmp_path)
    if isinstance(content, dict):
        content = json.dumps(content).encode()
    drop(tmp_path / "drop", "bad.json", content)

    assert worker.import_pending() == []

    rejected = tmp_path / "drop" / "rejected"
    assert (rejected / "bad.json").exists()
    assert reason in (rejected / "bad.json.reason.txt").read_text(encoding="utf-8")
    assert not (tmp_path / "drop" / "incoming" / "bad.json").exists()
    assert inbox.recent("demo") == []
    assert worker.snapshot()["rejected_total"] == 1


def test_disabled_importer_leaves_files_untouched(tmp_path):
    worker, inbox = importer(tmp_path, preferences={})
    path = drop(tmp_path / "drop", "ml-abc123.json", document())

    assert worker.import_pending() == []
    assert path.exists()
    assert inbox.recent("demo") == []
    assert worker.snapshot()["state"] == "DISABLED"
    # Arborescence créée dès la construction pour le producteur.
    assert (tmp_path / "drop" / "processed").is_dir()
    assert (tmp_path / "drop" / "rejected").is_dir()


def test_same_file_twice_and_crash_before_move_are_idempotent(tmp_path):
    worker, inbox = importer(tmp_path)
    drop(tmp_path / "drop", "ml-abc123.json", document())
    first = worker.import_pending()[0]
    inbox.set_auto_state("demo", first["id"], "REJECTED", "Refus initial")

    # Le producteur rejoue le même fichier ; ou le worker s'arrête avant le déplacement.
    drop(tmp_path / "drop", "ml-abc123.json", document())
    second = worker.import_pending()[0]

    assert second["id"] == first["id"]
    assert len(inbox.recent("demo")) == 1
    # Réimportation du même identifiant : le refus n'est pas réinitialisé.
    assert inbox.recent("demo")[0]["auto_state"] == "REJECTED"
    processed = sorted(p.name for p in (tmp_path / "drop" / "processed").iterdir())
    assert len(processed) == 2 and "ml-abc123.json" in processed


def test_simulated_crash_between_receive_and_move(tmp_path, monkeypatch):
    worker, inbox = importer(tmp_path)
    path = drop(tmp_path / "drop", "ml-abc123.json", document())

    def crash(*args, **kwargs):
        raise OSError("arrêt brutal")

    monkeypatch.setattr(SignalDropImporter, "_move", staticmethod(crash))
    assert worker.import_pending() == []
    assert path.exists()
    assert worker.snapshot()["state"] == "ERROR"
    monkeypatch.undo()

    rows = worker.import_pending()
    assert len(rows) == 1
    assert len(inbox.recent("demo")) == 1
    assert not path.exists()


def queue_rows(tmp_path, preferences):
    inbox = SignalInbox(tmp_path / "signals.db")
    api = inbox.receive("demo", SIMPLE, source="api", external_id="drop:ml-1", source_timestamp=995)
    telegram = inbox.receive(
        "demo", SIMPLE.replace("T1: 90000", "T1: 91000"),
        source="telegram", external_id=telegram_id(1), source_timestamp=995,
    )
    worker, commands = executor(tmp_path, inbox, preferences)
    result = worker.process_pending()
    states = {row["id"]: row["auto_state"] for row in inbox.recent("demo")}
    processed = {row_id for row_id, state in states.items() if state}
    return api, telegram, processed, result, worker, states


def test_api_rows_need_drop_auto_even_when_telegram_auto_is_on(tmp_path):
    api, telegram, processed, _, _, states = queue_rows(tmp_path, enabled_preferences(signal_drop_enabled=True))

    assert processed == {telegram["id"]}
    assert states[telegram["id"]] == "QUEUED" and states[api["id"]] == ""


def test_api_rows_are_routed_to_review_when_drop_auto_is_authorized(tmp_path):
    preferences = enabled_preferences(
        signal_telegram_enabled=False, signal_telegram_auto_enabled=False,
        signal_drop_enabled=True, signal_drop_auto_enabled=True,
        signal_drop_auto_enabled_since=900,
    )
    api, telegram, processed, result, worker, states = queue_rows(tmp_path, preferences)

    # JSON v1 : aucune source déclarée, toujours « À confirmer », jamais automatique.
    assert result == ["REVIEW"]
    assert processed == {api["id"]} and states[api["id"]] == "REVIEW"
    assert "JSON v1" in next(r for r in worker.inbox.recent("demo") if r["id"] == api["id"])["auto_detail"]
    events = worker.events.tail(10)
    assert any("Signal ML/dépôt à confirmer" in event["message"] for event in events)
    assert worker.commands.list_recent("demo") == []


def test_api_rows_wait_for_drop_authorization_timestamp(tmp_path):
    preferences = enabled_preferences(
        signal_drop_enabled=True, signal_drop_auto_enabled=True,
        signal_drop_auto_enabled_since=0,
    )
    api, telegram, processed, _, _, _ = queue_rows(tmp_path, preferences)
    assert processed == {telegram["id"]}

    later = enabled_preferences(
        signal_drop_enabled=True, signal_drop_auto_enabled=True,
        signal_drop_auto_enabled_since=2000,
    )
    api, telegram, processed, _, _, _ = queue_rows(tmp_path / "later", later)
    assert processed == {telegram["id"]}


def test_drop_auto_does_not_require_or_change_telegram_gating(tmp_path):
    both = enabled_preferences(
        signal_drop_enabled=True, signal_drop_auto_enabled=True,
        signal_drop_auto_enabled_since=900,
    )
    api, telegram, processed, result, _, states = queue_rows(tmp_path, both)
    assert processed == {api["id"], telegram["id"]}
    assert result == ["REVIEW", "QUEUED"]
    assert states[api["id"]] == "REVIEW" and states[telegram["id"]] == "QUEUED"

    master_off = both | {"signal_auto_execute_enabled": False}
    _, _, processed, result, _, _ = queue_rows(tmp_path / "off", master_off)
    assert processed == set() and result == []


def test_old_api_signal_goes_to_review_with_drop_label(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", SIMPLE, source="api", external_id="drop:old", source_timestamp=995)
    preferences = enabled_preferences(
        signal_drop_enabled=True, signal_drop_auto_enabled=True,
        signal_drop_auto_enabled_since=900,
    )
    inbox.claim_auto("demo", row["id"])  # reprise après arrêt : ligne PROCESSING, âge dépassé
    worker, commands = executor(tmp_path, inbox, preferences, now=5000)

    assert worker.process_pending() == ["REVIEW"]
    assert "Signal ML/dépôt trop ancien" in inbox.recent("demo")[0]["auto_detail"]
    assert commands.list_recent("demo") == []


@pytest.fixture
def rules():
    return parse_symbol_rules({
        "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
        "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "100"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ],
    })


def prepare(rules, **overrides):
    # La règle « SL après TP » ne s'applique que si le suivi du SL (actif par défaut) est coupé.
    kwargs = dict(budget=300, available_quote=1000, reserve_percent=20, current_price=84500,
                  signal_id="sig", source="api", validity_confirmed=True, trail_stop=False)
    return prepare_signal(parse_signal(TEXT), rules, **(kwargs | overrides))


def test_sl_after_tp_defaults_to_no_change(rules):
    _, payload = prepare(rules)
    assert [tp["sl_rule_after_hit"] for tp in payload["position"]["take_profits"]] == [
        "NO_CHANGE", "NO_CHANGE",
    ]
    assert payload["position"]["source_groups"][0]["source"] == "api"


def test_trailing_stop_takes_precedence_over_sl_after_tp(rules):
    _, payload = prepare(rules, trail_stop=True, sl_after_tp="BREAK_EVEN")
    assert [tp["sl_rule_after_hit"] for tp in payload["position"]["take_profits"]] == [
        "FIXED_PRICE", "NO_CHANGE",
    ]


@pytest.mark.parametrize("rule", ["BREAK_EVEN", "BREAK_EVEN_WITH_FEES", "PREVIOUS_TP"])
def test_sl_after_tp_applies_to_every_tp_except_last(rules, rule):
    _, payload = prepare(rules, sl_after_tp=rule)
    assert [tp["sl_rule_after_hit"] for tp in payload["position"]["take_profits"]] == [
        rule, "NO_CHANGE",
    ]


def test_sl_after_tp_rejects_rules_requiring_a_value(rules):
    with pytest.raises(ValueError):
        prepare(rules, sl_after_tp=SLRuleAfterTP.FIXED_PRICE)
    assert signal_sl_after_tp("CUSTOM_PERCENT") is SLRuleAfterTP.NO_CHANGE
    assert signal_sl_after_tp(None) is SLRuleAfterTP.NO_CHANGE
    assert signal_sl_after_tp("BREAK_EVEN") is SLRuleAfterTP.BREAK_EVEN


def test_auto_executor_passes_saved_sl_rule(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    text = SIMPLE.replace("T1: 90000", "T1: 90000\nT2: 92000")
    row = inbox.receive("demo", text, source="telegram", external_id=telegram_id(2), source_timestamp=995)
    worker, commands = executor(
        tmp_path, inbox,
        enabled_preferences(signal_sl_after_tp="BREAK_EVEN", **{TRAIL_STOP_KEY: False}),
    )

    assert worker.process_pending() == ["QUEUED"]
    payload = commands.get_by_request_key("demo", f"signal:{row['id']}")["payload"]
    assert [tp["sl_rule_after_hit"] for tp in payload["position"]["take_profits"]] == [
        "BREAK_EVEN", "NO_CHANGE",
    ]


def test_settings_authorize_drop_auto_execution_and_sl_rule(monkeypatch, tmp_path):
    store = JsonFileStore(tmp_path / "settings.json")
    store.save({"signal_drop_enabled": True})
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

    def save():
        next(b for b in app.button if b.label == "Enregistrer l'exécution automatique").click().run()

    app.get_by_key("signal_auto_execute_toggle").set_value(True).run()
    app.get_by_key("signal_auto_execute_authorization").set_value(True).run()
    app.get_by_key("signal_drop_auto_toggle").set_value(True).run()
    save()
    assert not app.exception
    # Autorisation séparée des dépôts non cochée : rien n'est enregistré.
    assert "signal_drop_auto_enabled" not in store.load()

    app.get_by_key("signal_drop_auto_authorization").set_value(True).run()
    save()
    assert not app.exception
    saved = store.load()
    assert saved["signal_auto_execute_enabled"] is True
    assert saved["signal_drop_auto_enabled"] is True
    assert saved["signal_drop_auto_enabled_since"] > 0

    app.get_by_key("signal_sl_after_tp_select").set_value("BREAK_EVEN").run()
    next(b for b in app.button if b.label == "Enregistrer la règle SL des signaux").click().run()
    assert not app.exception
    assert store.load()["signal_sl_after_tp"] == "BREAK_EVEN"
