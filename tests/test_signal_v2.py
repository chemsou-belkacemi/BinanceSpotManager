"""Contrat V2 du dépôt (CryptoSignalIntelligence) : parseur strict, dépôt TXT, idempotence,
fenêtre de validité, écart d'entrée, poids de TP, positions expirées et fichier de retour."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from binance_spot_manager.command_store import CommandStore
from binance_spot_manager.models import (
    CloseReason, Commission, Entry, EntryStatus, OrderType, Position, PositionStatus, SLStatus,
    TakeProfit, utcnow,
)
from binance_spot_manager.position_engine import PositionEngine, finish_position, recompute_position
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.signal_drop import SignalDropImporter
from binance_spot_manager.signal_feedback import (
    FeedbackContractError, SignalFeedbackWriter, build_event_id, decimal_text, validate_event,
)
from binance_spot_manager.signal_inbox import DuplicateSignal, SignalInbox
from binance_spot_manager.signal_parser import ParsedSignal, parse_signal, parse_signal_v2
from binance_spot_manager.signal_plan import prepare_signal, tp_sell_percents
from binance_spot_manager.symbol_rules import parse_symbol_rules
from test_automation import FakeClient, build_automation, build_execution, events, rules, settings  # noqa: F401
from test_commands import processor  # noqa: F401
from test_signal_auto_execution import enabled_preferences, executor
from test_signals import ABK, BICO, GALA, SIMPLE

# Exemple synthétique de la spécification producteur (tests/test_signals.py de CSI), verbatim.
SPEC_EXAMPLE = """SIGNAL_VERSION=2
SIGNAL_ID=EXAMPLE_ONLY_001
IDEMPOTENCY_KEY=EXAMPLE_ONLY_SETUP_001
CREATED_AT=2026-09-29T12:00:05Z
DATA_AS_OF=2026-09-29T12:00:00Z
VALID_FROM=2026-09-29T12:00:05Z
EXPIRES_AT=2026-09-29T12:15:00Z
MARKET_DATA_SOURCE=SYNTHETIC_FIXTURE
INTENDED_EXECUTION_ENVIRONMENT=DEMO
MARKET_TYPE=SPOT
SYMBOL=TESTUSDT
ACTION=BUY
STRATEGY=EMA_PULLBACK_CONTINUATION
STRATEGY_VERSION=1
TIMEFRAME_SETUP=15m
ENTRY_MODE=LIMIT
ENTRY_1=100.00
ENTRY_2=NONE
ENTRY_WEIGHTS=1.0
WEIGHT_BASIS=BASE_QUANTITY
STOP_LOSS=98.00
TP_COUNT=4
TP_1=102.00
TP_2=103.00
TP_3=104.00
TP_4=106.00
TP_WEIGHTS=0.25,0.25,0.25,0.25
EXIT_POLICY_ID=FIXED_SL_FOUR_TP_V1
RR_TP1_GROSS=1.0
RR_TP2_GROSS=1.5
RR_TP3_GROSS=2.0
RR_TP4_GROSS=3.0
TECHNICAL_SCORE=NONE
ML_PROBABILITY=NONE
MODEL_ID=NONE
TREND_REGIME=BULL
VOLATILITY_REGIME=NORMAL
MAX_ENTRY_DEVIATION_BPS=20
VALIDATION_STATUS=SCHEMA_EXAMPLE_ONLY
STATUS=NEW
---ANALYSIS---
REASONS=EXEMPLE_SYNTHETIQUE_NE_PAS_EXECUTER
"""
# Adapté à une paire Demo réelle et à un statut exécutable.
ADAPTED = (SPEC_EXAMPLE.replace("SYMBOL=TESTUSDT", "SYMBOL=BTCUSDT")
           .replace("VALIDATION_STATUS=SCHEMA_EXAMPLE_ONLY", "VALIDATION_STATUS=DEMO_ELIGIBLE"))


def epoch(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


CREATED = epoch("2026-09-29T12:00:05Z")
EXPIRES = epoch("2026-09-29T12:15:00Z")
NOW = epoch("2026-09-29T12:05:00Z")
SIGNAL_ID = "CSI-20260929T120005Z-TEST0001"
KEY = "SPOT:BTCUSDT:DONCHIAN_VOLUME_BREAKOUT:v1:20260929T120000Z:FIXED_SL_ONE_TP_V1"


def v2_text(**overrides):
    """Signal V2 BTCUSDT cohérent ; les RR sont recalculés sauf s'ils sont imposés."""
    fields = dict(
        SIGNAL_VERSION="2", SIGNAL_ID=SIGNAL_ID, IDEMPOTENCY_KEY=KEY,
        CREATED_AT="2026-09-29T12:00:05Z", DATA_AS_OF="2026-09-29T12:00:00Z",
        VALID_FROM="2026-09-29T12:00:05Z", EXPIRES_AT="2026-09-29T12:15:00Z",
        MARKET_DATA_SOURCE="BINANCE_SPOT_PUBLIC", INTENDED_EXECUTION_ENVIRONMENT="DEMO",
        MARKET_TYPE="SPOT", SYMBOL="BTCUSDT", ACTION="BUY", STRATEGY="DONCHIAN_VOLUME_BREAKOUT",
        STRATEGY_VERSION="1", TIMEFRAME_SETUP="15m", ENTRY_MODE="LIMIT", ENTRY_1="84000.00",
        ENTRY_2="NONE", ENTRY_WEIGHTS="1.0", WEIGHT_BASIS="BASE_QUANTITY", STOP_LOSS="80000.00",
        TP_COUNT="1", TP_1="90000.00", TP_2="NONE", TP_3="NONE", TP_4="NONE", TP_WEIGHTS="1.0",
        EXIT_POLICY_ID="FIXED_SL_ONE_TP_V1", RR_TP1_GROSS=None, RR_TP2_GROSS=None,
        RR_TP3_GROSS=None, RR_TP4_GROSS=None, TECHNICAL_SCORE="NONE", ML_PROBABILITY="NONE",
        MODEL_ID="NONE", TREND_REGIME="BULL", VOLATILITY_REGIME="NORMAL",
        MAX_ENTRY_DEVIATION_BPS="100", VALIDATION_STATUS="DEMO_ELIGIBLE",
        INTEGRATION_STATUS="INTEGRATION_UNVERIFIED", STATUS="NEW",
    )
    fields.update(overrides)
    entry, stop = Decimal(fields["ENTRY_1"]), Decimal(fields["STOP_LOSS"])
    for index in range(1, 5):
        if fields[f"RR_TP{index}_GROSS"] is None:
            target = fields[f"TP_{index}"]
            fields[f"RR_TP{index}_GROSS"] = "NONE" if target == "NONE" else str(
                ((Decimal(target) - entry) / (entry - stop)).quantize(Decimal("0.001")))
    return "\n".join(f"{key}={value}" for key, value in fields.items()) + "\n---ANALYSIS---\nREASONS=test\n"


def four_tp(**overrides):
    fields = dict(TP_COUNT="4", TP_1="90000.00", TP_2="92000.00", TP_3="94000.00", TP_4="96000.00",
                  TP_WEIGHTS="0.25,0.25,0.25,0.25", EXIT_POLICY_ID="FIXED_SL_FOUR_TP_V1")
    return v2_text(**(fields | overrides))


def receive_v2(inbox, text, scope="demo"):
    parsed = parse_signal_v2(text)
    assert not parsed.errors, parsed.errors
    return inbox.receive(scope, text, source="api", external_id=f"csi:{parsed.signal_id}",
                         source_timestamp=CREATED, idempotency_key=parsed.idempotency_key,
                         producer_signal_id=parsed.signal_id)


def drop_preferences(**overrides):
    return enabled_preferences(signal_telegram_enabled=False, signal_telegram_auto_enabled=False,
                               signal_drop_enabled=True, signal_drop_auto_enabled=True,
                               signal_drop_auto_enabled_since=900, **overrides)


# ==========================================================================
# Parseur strict
# ==========================================================================


def test_spec_example_parses_once_adapted_to_a_demo_pair():
    result = parse_signal(ADAPTED)
    assert result.errors == [] and result.warnings == []
    assert result.template == "v2" and result.is_v2 and result.direction == "BUY"
    assert result.symbol == "BTCUSDT"
    assert result.entries == [100.0] and result.targets == [102.0, 103.0, 104.0, 106.0] and result.stop == 98.0
    assert result.published_at == "2026-09-29T12:00:05+00:00"
    assert result.signal_id == "EXAMPLE_ONLY_001" and result.idempotency_key == "EXAMPLE_ONLY_SETUP_001"
    assert result.valid_from == CREATED and result.expires_at == EXPIRES
    assert result.max_entry_deviation_bps == 20.0 and result.tp_weights == [0.25] * 4
    assert result.exit_policy_id == "FIXED_SL_FOUR_TP_V1"
    # L'exemple brut reste un exemple de schéma : conforme mais jamais exécutable.
    assert any("SCHEMA_EXAMPLE_ONLY" in error for error in parse_signal(SPEC_EXAMPLE).errors)
    assert parse_signal(ADAPTED.replace("\n", "\r\n")).errors == []


@pytest.mark.parametrize("old, new, message", [
    ("TP_1=102.00", "TP_1=102.00\nTP_1=102.00", "dupliquée"),
    ("STATUS=NEW", "STATUS=NEW\nFOO=1", "inconnue"),
    ("SIGNAL_VERSION=2", "SIGNAL_VERSION=3", "version"),
    ("RR_TP1_GROSS=1.0", "RR_TP1_GROSS=1.2", "RR_TP1_GROSS attendu"),
    ("TP_3=104.00", "TP_3=102.50", "croissants"),
    ("STOP_LOSS=98.00", "STOP_LOSS=101.00", "STOP_LOSS < ENTRY_1"),
    ("EXPIRES_AT=2026-09-29T12:15:00Z", "EXPIRES_AT=2026-09-29T12:00:00Z", "DATA_AS_OF <= CREATED_AT"),
    ("INTENDED_EXECUTION_ENVIRONMENT=DEMO", "INTENDED_EXECUTION_ENVIRONMENT=LIVE", "DEMO"),
    ("MARKET_TYPE=SPOT", "MARKET_TYPE=FUTURES", "SPOT"),
    ("ACTION=BUY", "ACTION=SELL", "BUY"),
    ("ENTRY_MODE=LIMIT", "ENTRY_MODE=MARKET", "LIMIT"),
    ("ENTRY_2=NONE", "ENTRY_2=99.00", "ENTRY_2"),
    ("SYMBOL=BTCUSDT", "SYMBOL=BTCEUR", "USDT/USDC"),
    ("SYMBOL=BTCUSDT", "SYMBOL = BTCUSDT", "CLE=VALEUR"),
    ("ENTRY_1=100.00", "ENTRY_1=100,00", "décimal"),
    ("ENTRY_1=100.00", "ENTRY_1=NaN", "décimal"),
    ("TP_WEIGHTS=0.25,0.25,0.25,0.25", "TP_WEIGHTS=0.3,0.25,0.25,0.25", "sommer"),
    ("TP_WEIGHTS=0.25,0.25,0.25,0.25", "TP_WEIGHTS=0.5,0.5", "un poids"),
    ("TP_COUNT=4", "TP_COUNT=3", "au-delà de TP_COUNT"),
    ("TP_4=106.00", "TP_4=NONE", "TP_COUNT incohérent"),
    ("CREATED_AT=2026-09-29T12:00:05Z", "CREATED_AT=2026-09-29 12:00:05", "format"),
    ("STOP_LOSS=98.00", "STOP_LOSS=NONE", "obligatoire"),
    ("MAX_ENTRY_DEVIATION_BPS=20", "MAX_ENTRY_DEVIATION_BPS=501", "MAX_ENTRY_DEVIATION_BPS"),
    ("ML_PROBABILITY=NONE", "ML_PROBABILITY=0.6", "MODEL_ID"),
    ("STATUS=NEW", "STATUS=FILLED", "NEW"),
    ("SYMBOL=BTCUSDT\n", "", "manquantes"),
], ids=["duplicate-key", "unknown-key", "version", "rr", "tp-order", "stop-above-entry", "dates",
        "non-demo", "market-type", "sell", "entry-mode", "entry-2", "quote-asset", "spaces", "comma",
        "nan", "weights-sum", "weights-count", "tp-count-low", "tp-count-high", "time-format",
        "none-mandatory", "deviation-range", "ml-without-model", "status", "missing-key"])
def test_strict_parser_rejects(old, new, message):
    result = parse_signal(ADAPTED.replace(old, new))
    assert result.errors, "le texte muté devait être refusé"
    assert any(message in error for error in result.errors), result.errors


def test_analysis_section_cannot_change_the_contract():
    result = parse_signal(ADAPTED + "STOP_LOSS=1\nSIGNAL_VERSION=9\nFOO=BAR\n")
    assert result.errors == [] and result.stop == 98.0


def test_v2_and_legacy_templates_never_cross():
    assert parse_signal(SIMPLE, "v2").errors
    assert parse_signal(ADAPTED, "structured").errors
    assert parse_signal(ADAPTED, "v2").errors == [] and parse_signal(ADAPTED, "auto").errors == []
    stray = "PAIR: BTC/USDT\nSIGNAL_VERSION=2\nENTRY 1: 84000\nT1: 90000\nSL: 80000"
    assert any("SIGNAL_VERSION" in error for error in parse_signal(stray).errors)
    assert parse_signal_v2(SIMPLE).errors
    for legacy in (SIMPLE, BICO, GALA, ABK):
        parsed = parse_signal(legacy)
        assert parsed.errors == [] and parsed.signal_version == 1 and not parsed.is_v2


def test_rows_parsed_before_v2_still_load():
    legacy = {"template": "simple", "symbol": "BTCUSDT", "direction": "BUY", "exchange": "", "entries": [84000.0],
              "targets": [90000.0], "stop": 80000.0, "stop_timeframe": "", "published_at": "", "errors": [],
              "warnings": []}
    parsed = ParsedSignal(**legacy)
    assert parsed.signal_version == 1 and parsed.expires_at == 0.0 and parsed.tp_weights == []
    assert ParsedSignal(**parse_signal(ADAPTED).to_dict()).is_v2


# ==========================================================================
# Dépôt TXT et idempotence
# ==========================================================================


def drop_importer(tmp_path, *, now=NOW, feedback=None):
    inbox = SignalInbox(tmp_path / "signals.db")
    importer = SignalDropImporter(inbox, "demo", lambda: {"signal_drop_enabled": True},
                                  directory=tmp_path / "drop", clock=lambda: now, feedback=feedback)
    return importer, inbox


def drop_file(tmp_path, name, text):
    incoming = tmp_path / "drop" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    (incoming / name).write_bytes(text.encode("utf-8"))


def reason_of(tmp_path, name):
    return (tmp_path / "drop" / "rejected" / f"{name}.reason.txt").read_text(encoding="utf-8")


def test_txt_v2_file_is_imported_with_csi_identity(tmp_path):
    importer, inbox = drop_importer(tmp_path)
    drop_file(tmp_path, f"{SIGNAL_ID}.txt", v2_text())
    drop_file(tmp_path, "CSI-later.txt.tmp", "SIGNAL_VERSION=2\n")

    rows = importer.import_pending()

    assert len(rows) == 1
    row = inbox.recent("demo")[0]
    assert row["source"] == "api" and row["external_id"] == f"csi:{SIGNAL_ID}"
    assert row["source_timestamp"] == CREATED and row["expires_at"] == EXPIRES
    assert row["parsed"]["signal_version"] == 2 and row["parsed"]["errors"] == []
    assert (tmp_path / "drop" / "processed" / f"{SIGNAL_ID}.txt").exists()
    assert [p.name for p in (tmp_path / "drop" / "incoming").iterdir()] == ["CSI-later.txt.tmp"]
    assert (tmp_path / "drop" / "outgoing").is_dir()
    keys = inbox.signal_keys("demo")
    assert len(keys) == 1 and keys[0]["signal_id"] == SIGNAL_ID and keys[0]["row_id"] == row["id"]
    assert importer.snapshot()["imported_total"] == 1


def test_txt_expired_at_reception_is_rejected(tmp_path):
    importer, inbox = drop_importer(tmp_path, now=EXPIRES)
    drop_file(tmp_path, "late.txt", v2_text())

    assert importer.import_pending() == []
    assert "expiré à la réception" in reason_of(tmp_path, "late.txt")
    assert inbox.recent("demo") == [] and inbox.signal_keys("demo") == []
    assert importer.snapshot()["rejected_total"] == 1


@pytest.mark.parametrize("text, message", [
    (v2_text(STATUS="NEW\nFOO=1"), "clé inconnue"),
    (v2_text(RR_TP1_GROSS="9.999"), "RR_TP1_GROSS"),
    ("PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000\n", "CLE=VALEUR"),
    (SPEC_EXAMPLE, "SCHEMA_EXAMPLE_ONLY"),
], ids=["unknown-key", "rr", "legacy-text", "schema-example"])
def test_txt_parse_errors_are_rejected_with_reason(tmp_path, text, message):
    importer, inbox = drop_importer(tmp_path)
    drop_file(tmp_path, "bad.txt", text)

    assert importer.import_pending() == []
    assert message in reason_of(tmp_path, "bad.txt")
    assert inbox.recent("demo") == []


def test_duplicate_idempotency_key_and_replays(tmp_path):
    importer, inbox = drop_importer(tmp_path)
    drop_file(tmp_path, "first.txt", v2_text())
    assert len(importer.import_pending()) == 1
    first = inbox.recent("demo")[0]

    # Autre SIGNAL_ID, même clé logique : refusé, jamais enregistré.
    drop_file(tmp_path, "second.txt", v2_text(SIGNAL_ID="CSI-20260929T120005Z-TEST0002"))
    assert importer.import_pending() == []
    assert "Doublon" in reason_of(tmp_path, "second.txt")
    # Même SIGNAL_ID, autre clé : refusé aussi.
    drop_file(tmp_path, "third.txt", v2_text(IDEMPOTENCY_KEY=KEY + ":B"))
    assert importer.import_pending() == []
    assert "Doublon" in reason_of(tmp_path, "third.txt")
    # Rejeu strict du même fichier (arrêt avant déplacement) : idempotent, classé.
    drop_file(tmp_path, "first.txt", v2_text())
    replay = importer.import_pending()
    assert len(replay) == 1 and replay[0]["id"] == first["id"]
    assert len(inbox.recent("demo")) == 1 and len(inbox.signal_keys("demo")) == 1
    assert len(list((tmp_path / "drop" / "processed").iterdir())) == 2


def test_inbox_registry_is_written_with_the_signal(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = receive_v2(inbox, v2_text())
    with pytest.raises(DuplicateSignal) as duplicate:
        receive_v2(inbox, v2_text(SIGNAL_ID="CSI-OTHER"))
    assert not duplicate.value.same_signal and duplicate.value.signal_id == SIGNAL_ID
    with pytest.raises(DuplicateSignal) as same:
        receive_v2(inbox, v2_text(IDEMPOTENCY_KEY=KEY + ":B"))
    assert same.value.same_signal
    with pytest.raises(DuplicateSignal):
        inbox.receive("demo", v2_text(MAX_ENTRY_DEVIATION_BPS="30"), source="api", external_id="csi:x",
                      source_timestamp=CREATED, idempotency_key=KEY, producer_signal_id=SIGNAL_ID)
    assert receive_v2(inbox, v2_text())["id"] == row["id"]
    # Autre compte : registre séparé.
    assert receive_v2(inbox, v2_text(), scope="other")["id"] != row["id"]
    with pytest.raises(ValueError, match="requis ensemble"):
        inbox.receive("demo", v2_text(), idempotency_key=KEY)
    with inbox.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT scope, idempotency_key, signal_id FROM signal_keys ORDER BY scope")] == [
            ("demo", KEY, SIGNAL_ID), ("other", KEY, SIGNAL_ID)]
    assert inbox.get("demo", row["id"])["id"] == row["id"] and inbox.get("other", row["id"]) is None


# ==========================================================================
# Exécution automatique
# ==========================================================================


def test_v2_row_is_queued_within_validity_window_regardless_of_age(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = receive_v2(inbox, v2_text())
    worker, commands = executor(tmp_path, inbox, drop_preferences(), now=CREATED + 600)  # 10 min > 5 min

    assert worker.process_pending() == ["QUEUED"]
    payload = commands.get_by_request_key("demo", f"signal:{row['id']}")["payload"]
    assert payload["signal_expires_at"] == EXPIRES
    assert payload["max_entry_deviation_bps"] == 100.0
    assert payload["signal_entry_price"] == 84000.0
    assert payload["signal_external_id"] == SIGNAL_ID
    entry = payload["position"]["entries"][0]
    assert datetime.fromisoformat(entry["expires_at"]).timestamp() == EXPIRES
    assert payload["position"]["tags"] == ["signal", row["id"]]
    assert worker.process_pending() == []


def test_expired_v2_row_is_rejected_with_reason(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    receive_v2(inbox, v2_text())
    worker, commands = executor(tmp_path, inbox, drop_preferences(), now=EXPIRES)

    assert worker.process_pending() == ["REJECTED"]
    assert "expiré" in inbox.recent("demo")[0]["auto_detail"]
    assert commands.list_recent("demo") == []


def test_not_yet_valid_v2_row_waits_without_being_claimed(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    receive_v2(inbox, v2_text(VALID_FROM="2026-09-29T12:10:00Z"))
    worker, commands = executor(tmp_path, inbox, drop_preferences(), now=NOW)

    assert worker.process_pending() == ["WAITING"]
    assert inbox.recent("demo")[0]["auto_state"] == ""
    assert "VALID_FROM" in worker.snapshot()["last_detail"]
    later, _ = executor(tmp_path / "later", inbox, drop_preferences(), now=epoch("2026-09-29T12:10:00Z"))
    assert later.process_pending() == ["QUEUED"]


def test_price_deviation_beyond_contract_is_rejected(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    receive_v2(inbox, v2_text(MAX_ENTRY_DEVIATION_BPS="20"))  # prix 84500 vs 84000 : 59,5 bps
    worker, commands = executor(tmp_path, inbox, drop_preferences(), now=NOW)

    assert worker.process_pending() == ["REJECTED"]
    detail = inbox.recent("demo")[0]["auto_detail"]
    assert "59.5 bps" in detail and "MAX_ENTRY_DEVIATION_BPS 20" in detail
    assert commands.list_recent("demo") == []


@pytest.mark.parametrize("policy, rules_after", [
    ("FIXED_SL_FOUR_TP_V1", ["NO_CHANGE"] * 4),
    ("BREAK_EVEN_AFTER_TP1_V1", ["BREAK_EVEN"] * 3 + ["NO_CHANGE"]),
    ("TRAIL_PREVIOUS_TP_V1", ["PREVIOUS_TP"] * 3 + ["NO_CHANGE"]),
])
def test_exit_policy_maps_to_sl_rule_over_saved_preference(tmp_path, policy, rules_after):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = receive_v2(inbox, four_tp(EXIT_POLICY_ID=policy))
    worker, commands = executor(tmp_path, inbox, drop_preferences(signal_sl_after_tp="BREAK_EVEN_WITH_FEES"), now=NOW)

    assert worker.process_pending() == ["QUEUED"]
    payload = commands.get_by_request_key("demo", f"signal:{row['id']}")["payload"]
    assert [tp["sl_rule_after_hit"] for tp in payload["position"]["take_profits"]] == rules_after


def test_unknown_exit_policy_is_rejected(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    receive_v2(inbox, v2_text(EXIT_POLICY_ID="FIXED_SL_ONE_TP_V9"))
    worker, commands = executor(tmp_path, inbox, drop_preferences(), now=NOW)

    assert worker.process_pending() == ["REJECTED"]
    assert "EXIT_POLICY_ID inconnu" in inbox.recent("demo")[0]["auto_detail"]
    assert commands.list_recent("demo") == []


# ==========================================================================
# Préparation : poids des TP, expiration, gel
# ==========================================================================


@pytest.fixture
def btc_rules():
    return parse_symbol_rules({
        "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
        "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "100"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ],
    })


def prepare_v2(text, btc_rules, **overrides):
    kwargs = dict(budget=300, available_quote=1000, reserve_percent=20, current_price=84500,
                  signal_id="row", source="api", validity_confirmed=True)
    return prepare_signal(parse_signal(text), btc_rules, **(kwargs | overrides))


def test_tp_weights_become_remaining_percentages_and_entry_expires_at_contract(btc_rules):
    text = v2_text(TP_COUNT="3", TP_1="90000.00", TP_2="92000.00", TP_3="94000.00",
                   TP_WEIGHTS="0.5,0.3,0.2", EXIT_POLICY_ID="TRAIL_PREVIOUS_TP_V1")
    plan, payload = prepare_v2(text, btc_rules)
    position = payload["position"]
    assert [tp.sell_percent for tp in plan.take_profits] == pytest.approx([50, 30, 20])
    assert [tp["sell_percent"] for tp in position["take_profits"]] == pytest.approx([50, 60, 100])
    assert [tp["sl_rule_after_hit"] for tp in position["take_profits"]] == ["PREVIOUS_TP", "PREVIOUS_TP", "NO_CHANGE"]
    assert datetime.fromisoformat(position["entries"][0]["expires_at"]).timestamp() == EXPIRES
    assert payload["signal_expires_at"] == EXPIRES and payload["max_entry_deviation_bps"] == 100.0
    assert payload["signal_entry_price"] == 84000.0 and payload["signal_external_id"] == SIGNAL_ID
    assert tp_sell_percents([0.25] * 4) == pytest.approx([25, 100 / 3, 50, 100])
    assert tp_sell_percents([1 / 3] * 3) == pytest.approx([100 / 3, 50, 100])


def test_minimum_tp_quantity_uses_the_smallest_weight(btc_rules):
    balanced = v2_text(TP_COUNT="2", TP_1="90000.00", TP_2="92000.00", TP_WEIGHTS="0.5,0.5",
                       EXIT_POLICY_ID="FIXED_SL_FOUR_TP_V1")
    skewed = v2_text(TP_COUNT="2", TP_1="90000.00", TP_2="92000.00", TP_WEIGHTS="0.98,0.02",
                     EXIT_POLICY_ID="FIXED_SL_FOUR_TP_V1")
    prepare_v2(balanced, btc_rules, budget=200)
    with pytest.raises(ValueError, match="tranches TP"):
        prepare_v2(skewed, btc_rules, budget=200)
    prepare_v2(skewed, btc_rules, budget=600)


def test_legacy_preparation_keeps_24h_expiry_and_equal_slices(btc_rules):
    _, payload = prepare_v2(SIMPLE.replace("T1: 90000", "T1: 90000\nT2: 95000\nT3: 100000"), btc_rules)
    position = payload["position"]
    assert [tp["sell_percent"] for tp in position["take_profits"]] == pytest.approx([100 / 3, 50, 100])
    expiry = datetime.fromisoformat(position["entries"][0]["expires_at"])
    assert timedelta(hours=23) < expiry - utcnow() <= timedelta(hours=24)
    assert "signal_expires_at" not in payload and "max_entry_deviation_bps" not in payload


# ==========================================================================
# Worker : recontrôle du contrat gelé
# ==========================================================================


def submit(worker, payload_extra, price=84000):
    from test_commands import proposed
    worker.execution.client.get_price = lambda symbol: price
    position = proposed()
    worker.store.enqueue(worker.scope, "SUBMIT_POSITION", {
        "position": position.model_dump(mode="json"), "entry_ids": [e.entry_id for e in position.entries],
        "reference_price": 84000, "independent_position": True,
    } | payload_extra, request_key=f"v2-{len(worker.store.list_recent(worker.scope))}")
    return worker.run_one()


def test_worker_refuses_expired_signal_and_excess_deviation_before_any_order(processor):
    worker, calls = processor
    assert submit(worker, {"signal_expires_at": NOW}) == "FAILED"
    assert "EXPIRES_AT" in worker.store.list_recent(worker.scope)[0]["result"]["message"]
    # 84100 vs 84000 = 11,9 bps : refusé à 5 bps, accepté à 20 bps (et sous le seuil de 1 %).
    future = utcnow().timestamp() + 3600
    assert submit(worker, {"signal_expires_at": future, "max_entry_deviation_bps": 5,
                           "signal_entry_price": 84000}, price=84100) == "FAILED"
    assert "bps" in worker.store.list_recent(worker.scope)[0]["result"]["message"]
    assert calls == []
    assert submit(worker, {"signal_expires_at": future, "max_entry_deviation_bps": 20,
                           "signal_entry_price": 84000}, price=84100) == "SUCCEEDED"
    assert len(calls) == 1


# ==========================================================================
# Positions expirées sans achat
# ==========================================================================


def pending_position(*, executed_qty=0.0, status=EntryStatus.SUBMITTED):
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", environment="DEMO",
                        creation_price=84000.0, status=PositionStatus.PENDING_ENTRIES, tags=["signal", "row-1"])
    position.entries.append(Entry(
        sequence_number=1, order_type=OrderType.LIMIT, status=status, binance_qty=0.002,
        resolved_price=83000.0, order_id=4242, client_order_id="BSM-D-BTC-exp-E1",
        executed_qty=executed_qty, average_fill_price=83000.0 if executed_qty else 0.0,
        quote_spent=executed_qty * 83000.0, expires_at=utcnow() - timedelta(seconds=1),
    ))
    position.take_profits.append(TakeProfit(sequence_number=1, target_price=90000.0, sell_percent=100.0))
    position.stop_loss.resolved_price = 80000.0
    position.stop_loss.status = SLStatus.PLANNED
    recompute_position(position)
    return position


def seed_entry_order(fake, executed="0"):
    fake.orders["BSM-D-BTC-exp-E1"] = {
        "symbol": "BTCUSDT", "orderId": 4242, "clientOrderId": "BSM-D-BTC-exp-E1", "side": "BUY",
        "type": "LIMIT", "status": "NEW" if executed == "0" else "PARTIALLY_FILLED", "price": "83000",
        "stopPrice": "0", "origQty": "0.002", "executedQty": executed,
        "cummulativeQuoteQty": str(float(executed) * 83000), "fills": [],
    }


def test_expired_unfilled_entry_closes_the_position(settings, rules, events, tmp_path):
    fake = FakeClient(rules)
    seed_entry_order(fake)
    execution = build_execution(settings, rules, events, fake)
    position = pending_position()
    store = PositionStore(tmp_path / "positions")
    store.save(position)

    result = build_automation(execution, rules, events).run_cycle(position, 84000.0)

    assert result.position_finished and not result.errors
    assert position.entries[0].status is EntryStatus.EXPIRED
    assert position.status is PositionStatus.CLOSED and position.close_reason is CloseReason.CANCELED_BEFORE_FILL
    assert fake.cancelled == [4242] and fake.created == []
    store.save(position)
    assert store.list_open() == []
    assert any(e["event"] == "POSITION_FINISHED" for e in events.tail(20))


def test_partially_filled_expired_entry_keeps_the_position_open(settings, rules, events):
    fake = FakeClient(rules)
    seed_entry_order(fake, executed="0.001")
    execution = build_execution(settings, rules, events, fake)
    position = pending_position(executed_qty=0.001, status=EntryStatus.PARTIALLY_FILLED)

    result = build_automation(execution, rules, events).run_cycle(position, 84000.0)

    assert not result.position_finished
    assert position.is_open and position.metrics.net_qty == pytest.approx(0.001)


def test_already_terminal_entries_without_fill_are_closed_next_cycle(settings, rules, events):
    fake = FakeClient(rules)
    execution = build_execution(settings, rules, events, fake)
    position = pending_position(status=EntryStatus.CANCELED)

    result = build_automation(execution, rules, events).run_cycle(position, 84000.0)

    assert result.position_finished and position.close_reason is CloseReason.CANCELED_BEFORE_FILL
    assert fake.created == [] and fake.cancelled == []


# ==========================================================================
# Fichier de retour d'exécution
# ==========================================================================


def csi_feedback_model():
    """Modèle pydantic du producteur, chargé depuis son fichier si le dépôt est accessible."""
    candidates = [Path(os.environ["CSI_PATH"])] if os.environ.get("CSI_PATH") else []
    here = Path(__file__).resolve()
    candidates += [parent / "CryptoSignalIntelligence" for parent in list(here.parents)[:6]]
    for root in candidates:
        schema = root / "src" / "crypto_signal_intelligence" / "feedback" / "schema.py"
        if schema.exists():
            spec = importlib.util.spec_from_file_location("csi_feedback_schema", schema)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    return None


CSI_SCHEMA = csi_feedback_model()


def assert_contract(lines):
    """Chaque ligne satisfait le contrat, localement et (si disponible) chez le producteur."""
    events = [json.loads(line) for line in lines]
    for event in events:
        validate_event(event)
        assert list(event) == ["event_id", "signal_id", "event_type", "occurred_at", "environment", "producer",
                               "symbol", "quantity", "price", "quote_quantity", "fee", "fee_asset", "order_id",
                               "target_index", "reason"]
        assert event["environment"] == "DEMO" and event["producer"] == "BinanceSpotManager"
        for key in ("quantity", "price", "quote_quantity", "fee"):
            assert event[key] is None or (isinstance(event[key], str) and Decimal(event[key]).is_finite())
        if CSI_SCHEMA is not None:
            CSI_SCHEMA.parse_line(json.dumps(event))
    return events


def feedback_setup(tmp_path, *, enabled=True, now=NOW):
    inbox = SignalInbox(tmp_path / "signals.db")
    commands = CommandStore(tmp_path / "commands.db")
    store = PositionStore(tmp_path / "positions")
    writer = SignalFeedbackWriter("demo", inbox, commands, positions=store, directory=tmp_path / "outgoing",
                                  registry_path=tmp_path / "feedback.db", clock=lambda: now, enabled=enabled)
    return inbox, commands, store, writer


def lines_of(writer):
    return writer.path.read_text(encoding="utf-8").splitlines() if writer.path.exists() else []


def queue_signal(inbox, commands, writer, text, btc_rules):
    row = receive_v2(inbox, text)
    writer.register(parse_signal_v2(text).signal_id, row["id"], "BTCUSDT")
    _, payload = prepare_signal(parse_signal(text), btc_rules, budget=300, available_quote=1000,
                                reserve_percent=20, current_price=84500, signal_id=row["id"], source="api",
                                validity_confirmed=True)
    command = commands.enqueue("demo", "SUBMIT_POSITION", payload, request_key=f"signal:{row['id']}")
    position = Position.model_validate(payload["position"])
    return row, command, position


def test_feedback_file_follows_the_contract_over_a_full_lifecycle(tmp_path, btc_rules, monkeypatch):
    inbox, commands, store, writer = feedback_setup(tmp_path)
    assert writer.sync([]) == 0 and not writer.path.exists()
    row, command, position = queue_signal(inbox, commands, writer, four_tp(), btc_rules)
    assert writer.sync([]) == 1  # mise en file = signal accepté (RECEIVED), avant même l'achat
    commands.claim("demo")
    commands.finish("demo", command["id"], "SUCCEEDED", {"message": "ok"})
    entry = position.entries[0]
    entry.status, entry.order_id, entry.client_order_id = EntryStatus.SUBMITTED, 777, "BSM-D-BTC-x-E1"
    store.save(position)

    assert writer.sync([position]) == 0  # ordre déposé, aucun remplissage : rien de neuf
    engine = PositionEngine(btc_rules)
    engine.apply_entry_fill(position, entry.entry_id, executed_qty=0.001, average_price=84000, quote_spent=84,
                            commissions=[Commission(asset="USDT", amount=0.084)], order_id=777)
    assert entry.status is EntryStatus.PARTIALLY_FILLED
    assert writer.sync([position]) == 1
    assert writer.sync([position]) == 0  # même cumul : aucune seconde ligne
    engine.apply_entry_fill(position, entry.entry_id, executed_qty=entry.binance_qty, average_price=84000,
                            quote_spent=84000 * entry.binance_qty,
                            commissions=[Commission(asset="USDT", amount=0.084 * 2)], order_id=777)
    assert entry.status is EntryStatus.FILLED
    # Arrêt simulé entre l'écriture de la ligne et son enregistrement : même identifiant réémis.
    original = writer.registry.record_event

    def crash_once(*args, **kwargs):
        writer.registry.record_event = original
        raise RuntimeError("arrêt brutal")

    writer.registry.record_event = crash_once
    assert writer.sync([position]) == 0 and writer.snapshot()["state"] == "ERROR"
    assert writer.sync([position]) == 1 and writer.snapshot()["state"] == "ACTIVE"
    tp1 = position.sorted_tps[0]
    engine.apply_tp_fill(position, tp1.tp_id, executed_qty=0.0005, average_price=90000, quote_received=45,
                         commissions=[Commission(asset="BNB", amount=0.00001)], order_id=778)
    assert writer.sync([position]) == 1
    engine.apply_sl_fill(position, executed_qty=position.metrics.net_qty, average_price=80000,
                         quote_received=80000 * position.metrics.net_qty, commissions=[])
    assert position.status is PositionStatus.CLOSED
    store.save(position)
    assert writer.sync([]) == 2  # position rechargée par identifiant : STOP_FILLED + CLOSED
    assert writer.sync([position]) == 0

    lines = lines_of(writer)
    events = assert_contract(lines)
    ids = [event["event_id"] for event in events]
    assert len(lines) == len(set(ids)) + 1  # la ligne réémise porte le même identifiant
    by_type = {}
    for event in events:
        by_type.setdefault(event["event_type"], []).append(event)
    assert list(dict.fromkeys(e["event_type"] for e in events)) == [
        "RECEIVED", "ENTRY_PARTIAL", "ENTRY_FILLED", "TP_FILLED", "STOP_FILLED", "CLOSED"]
    assert by_type["RECEIVED"][0]["event_id"] == f"BSM-{SIGNAL_ID}-RECEIVED-1"
    partial, filled = by_type["ENTRY_PARTIAL"][0], by_type["ENTRY_FILLED"][0]
    assert (partial["quantity"], partial["price"], partial["quote_quantity"]) == ("0.001", "84000", "84")
    assert (partial["fee"], partial["fee_asset"], partial["order_id"]) == ("0.084", "USDT", "777")
    assert Decimal(filled["quantity"]) == Decimal(str(entry.binance_qty)) - Decimal("0.001")
    assert filled["fee"] == "0.084" and filled["event_id"] == f"BSM-{SIGNAL_ID}-ENTRY_FILLED-1"
    tp = by_type["TP_FILLED"][0]
    assert (tp["target_index"], tp["quantity"], tp["price"], tp["fee"], tp["fee_asset"]) == (1, "0.0005", "90000", "0.00001", "BNB")
    stop = by_type["STOP_FILLED"][0]
    assert stop["price"] == "80000" and stop["fee"] is None and stop["fee_asset"] is None
    assert Decimal(stop["quantity"]) + Decimal("0.0005") == Decimal(str(entry.binance_qty))
    closed = by_type["CLOSED"][0]
    assert closed["occurred_at"].endswith("Z") and closed["reason"] is None
    assert all(e["symbol"] == "BTCUSDT" and e["signal_id"] == SIGNAL_ID for e in events)
    assert writer.registry.pending() == []
    assert writer.snapshot()["events_total"] == len(set(ids))


def test_feedback_rejections_come_from_reception_auto_state_and_failed_commands(tmp_path, btc_rules):
    inbox, commands, store, writer = feedback_setup(tmp_path)
    # Refus à la réception (dépôt) : expiré, identifiant lu dans le fichier.
    assert writer.record_rejection("CSI-RECEPTION", "BTCUSDT", "Signal expiré à la réception", occurred_at=NOW)
    assert not writer.record_rejection("", "BTCUSDT", "sans identifiant")
    assert not writer.record_rejection("CSI-X", "BTC EUR", "paire invalide")
    # Refus de l'exécution automatique.
    auto = receive_v2(inbox, v2_text(SIGNAL_ID="CSI-AUTO", IDEMPOTENCY_KEY=KEY + ":A"))
    inbox.set_auto_state("demo", auto["id"], "REJECTED", "Écart de prix 59.5 bps > MAX_ENTRY_DEVIATION_BPS 20")
    # Commande refusée par le worker après mise en file.
    text = v2_text(SIGNAL_ID="CSI-FAILED", IDEMPOTENCY_KEY=KEY + ":F")
    row, command, _ = queue_signal(inbox, commands, writer, text, btc_rules)
    commands.claim("demo")
    commands.finish("demo", command["id"], "FAILED", {"message": "Prix modifie de plus de 1 %"})
    # Jamais traité (exécution automatique inactive) : refusé après EXPIRES_AT + délai.
    never = receive_v2(inbox, v2_text(SIGNAL_ID="CSI-NEVER", IDEMPOTENCY_KEY=KEY + ":N"))

    assert writer.sync([]) == 3  # découverte via signal_keys : AUTO, FAILED (RECEIVED + REJECTED)
    late = SignalFeedbackWriter("demo", inbox, commands, positions=store, directory=writer.directory,
                                registry_path=writer.registry.path, clock=lambda: EXPIRES + 61)
    assert late.sync([]) == 1 and late.sync([]) == 0

    events = assert_contract(lines_of(writer))
    by_signal = {e["signal_id"]: [x for x in events if x["signal_id"] == e["signal_id"]] for e in events}
    assert [e["event_type"] for e in by_signal["CSI-RECEPTION"]] == ["REJECTED"]
    assert by_signal["CSI-RECEPTION"][0]["reason"] == "Signal expiré à la réception"
    assert [e["event_type"] for e in by_signal["CSI-AUTO"]] == ["REJECTED"]
    assert "59.5 bps" in by_signal["CSI-AUTO"][0]["reason"]
    assert [e["event_type"] for e in by_signal["CSI-FAILED"]] == ["RECEIVED", "REJECTED"]
    assert by_signal["CSI-FAILED"][1]["reason"] == "Prix modifie de plus de 1 %"
    assert [e["event_type"] for e in by_signal["CSI-NEVER"]] == ["REJECTED"]
    assert "avant tout traitement" in by_signal["CSI-NEVER"][0]["reason"]
    assert late.registry.pending() == []
    assert never["auto_state"] == ""


def test_feedback_expired_and_cancelled_entries(tmp_path, btc_rules):
    inbox, commands, store, writer = feedback_setup(tmp_path)
    row, command, position = queue_signal(inbox, commands, writer, v2_text(), btc_rules)
    commands.claim("demo")
    commands.finish("demo", command["id"], "SUCCEEDED", {"message": "ok"})
    position.entries[0].status = EntryStatus.SUBMITTED
    assert writer.sync([position]) == 1  # RECEIVED
    position.entries[0].status = EntryStatus.EXPIRED
    finish_position(position, CloseReason.CANCELED_BEFORE_FILL)
    assert writer.sync([position]) == 1 and writer.sync([position]) == 0
    events = assert_contract(lines_of(writer))
    assert [e["event_type"] for e in events] == ["RECEIVED", "EXPIRED"]
    assert events[1]["event_id"] == f"BSM-{SIGNAL_ID}-EXPIRED-1"
    assert writer.registry.pending() == []

    other = v2_text(SIGNAL_ID="CSI-CANCEL", IDEMPOTENCY_KEY=KEY + ":C")
    row, command, position = queue_signal(inbox, commands, writer, other, btc_rules)
    commands.claim("demo")
    commands.finish("demo", command["id"], "SUCCEEDED", {"message": "ok"})
    position.entries[0].status = EntryStatus.CANCELED
    position.entries[0].last_error = "Annulation manuelle"
    finish_position(position, CloseReason.MANUAL_CLOSE)
    assert writer.sync([position]) == 2
    events = assert_contract(lines_of(writer))
    cancelled = [e for e in events if e["signal_id"] == "CSI-CANCEL"]
    assert [e["event_type"] for e in cancelled] == ["RECEIVED", "CANCELLED"]
    assert "Annulation manuelle" in cancelled[1]["reason"]


def test_feedback_is_silent_when_disabled_or_for_legacy_rows(tmp_path):
    inbox, commands, store, writer = feedback_setup(tmp_path, enabled=False)
    writer.register("CSI-1", "row", "BTCUSDT")
    assert not writer.record_rejection("CSI-1", "BTCUSDT", "refus")
    inbox.receive("demo", SIMPLE, source="api", external_id="drop:ml-1", source_timestamp=995)
    assert writer.sync([]) == 0 and not writer.path.exists() and writer.snapshot()["state"] == "DISABLED"
    active = SignalFeedbackWriter("demo", inbox, commands, positions=store, directory=tmp_path / "outgoing",
                                  registry_path=tmp_path / "feedback.db", clock=lambda: NOW)
    assert active.sync([]) == 0 and not active.path.exists()  # ligne v1 : hors contrat V2


def test_drop_importer_reports_reception_outcomes_to_feedback(tmp_path):
    inbox, commands, store, writer = feedback_setup(tmp_path)
    importer = SignalDropImporter(inbox, "demo", lambda: {"signal_drop_enabled": True},
                                  directory=tmp_path / "drop", clock=lambda: NOW, feedback=writer)
    # Lus dans l'ordre des noms : l'original précède son doublon et son rejeu.
    drop_file(tmp_path, "01-ok.txt", v2_text())
    drop_file(tmp_path, "02-expired.txt", v2_text(SIGNAL_ID="CSI-LATE", IDEMPOTENCY_KEY=KEY + ":L",
                                                   EXPIRES_AT="2026-09-29T12:04:00Z"))
    drop_file(tmp_path, "03-dup.txt", v2_text(SIGNAL_ID="CSI-DUP"))
    drop_file(tmp_path, "04-bad.txt", v2_text(SIGNAL_ID="CSI-BAD", IDEMPOTENCY_KEY=KEY + ":B", STATUS="NEW\nFOO=1"))
    drop_file(tmp_path, "05-replay.txt", v2_text())

    rows = importer.import_pending()

    assert len(rows) == 2 and rows[0]["id"] == rows[1]["id"]
    events = assert_contract(lines_of(writer))
    assert {(e["signal_id"], e["event_type"]) for e in events} == {
        ("CSI-LATE", "REJECTED"), ("CSI-DUP", "REJECTED"), ("CSI-BAD", "REJECTED")}
    reasons = {e["signal_id"]: e["reason"] for e in events}
    assert "expiré" in reasons["CSI-LATE"] and "Doublon" in reasons["CSI-DUP"] and "inconnue" in reasons["CSI-BAD"]
    assert {r["signal_id"] for r in writer.registry.pending()} == {SIGNAL_ID}


def test_feedback_validation_replicates_producer_rules():
    base = {"event_id": "BSM-X-RECEIVED-1", "signal_id": "CSI-X", "event_type": "RECEIVED",
            "occurred_at": "2026-09-30T10:15:03Z", "environment": "DEMO", "producer": "BinanceSpotManager",
            "symbol": "SOLUSDT", "quantity": None, "price": None, "quote_quantity": None, "fee": None,
            "fee_asset": None, "order_id": None, "target_index": None, "reason": None}
    validate_event(base)
    for mutation in [
        {"environment": "LIVE"}, {"occurred_at": "2026-09-30T10:15:03"}, {"occurred_at": "2026-09-30T10:15:03.5Z"},
        {"symbol": "BTCEUR"}, {"event_type": "FILLED"}, {"event_type": "REJECTED"}, {"event_type": "CANCELLED"},
        {"event_type": "ENTRY_FILLED"}, {"event_type": "ENTRY_FILLED", "quantity": "0", "price": "1"},
        {"event_type": "TP_FILLED", "quantity": "1", "price": "1"}, {"fee": "0.1"}, {"fee_asset": "USDT"},
        {"quantity": 1.0}, {"quantity": "NaN"}, {"quantity": "-1"}, {"target_index": 5}, {"target_index": True},
        {"order_id": 12}, {"reason": "x" * 501}, {"producer": "Binance Spot Manager"}, {"event_id": "bad id"},
    ]:
        with pytest.raises(FeedbackContractError):
            validate_event(base | mutation)
    with pytest.raises(FeedbackContractError):
        validate_event({key: value for key, value in base.items() if key != "reason"})
    validate_event(base | {"event_type": "TP_FILLED", "quantity": "0.5", "price": "119.15", "target_index": 4,
                           "fee": "0.01", "fee_asset": "USDT", "order_id": "123", "quote_quantity": "59.575"})
    validate_event(base | {"event_type": "REJECTED", "reason": "doublon"})
    if CSI_SCHEMA is not None:
        CSI_SCHEMA.parse_line(json.dumps(base | {"event_type": "REJECTED", "reason": "doublon"}))
    assert decimal_text(0.001) == "0.001" and decimal_text(84000.0) == "84000" and decimal_text(1e-08) == "0.00000001"
    assert decimal_text(84 / 0.001) == "84000" and decimal_text(0.1 + 0.2) == "0.3"
    assert build_event_id("CSI-1", "CLOSED", 1) == "BSM-CSI-1-CLOSED-1"
    long_id = build_event_id("X" * 160, "ENTRY_PARTIAL", 12)
    assert len(long_id) <= 160 and long_id == build_event_id("X" * 160, "ENTRY_PARTIAL", 12)


@pytest.mark.skipif(CSI_SCHEMA is None, reason="dépôt CryptoSignalIntelligence introuvable : définir CSI_PATH")
def test_producer_model_is_used_for_cross_validation():
    assert CSI_SCHEMA.ExecutionEvent.model_validate({
        "event_id": "BSM-CSI-1-RECEIVED-1", "signal_id": "CSI-1", "event_type": "RECEIVED",
        "occurred_at": "2026-09-30T10:15:03Z", "environment": "DEMO", "producer": "BinanceSpotManager",
        "symbol": "BTCUSDT"}).environment == "DEMO"
