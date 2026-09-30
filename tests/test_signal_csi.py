"""Contrat CSI du dépôt (TXT V3, politiques hachées, retour d'exécution v2) : parseur strict,
dépôt TXT, idempotence, deux expirations, écart d'entrée, statut, poids de TP, report des
tranches sous minimums, positions expirées et fichier de retour."""
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
    CloseReason, Commission, Entry, EntryStatus, ManualExit, OrderType, Position, PositionStatus, SLStatus,
    TakeProfit, TPStatus, utcnow,
)
from binance_spot_manager.position_engine import PositionEngine, finish_position, recompute_position
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.signal_drop import SignalDropImporter
from binance_spot_manager.signal_feedback import (
    FeedbackContractError, SignalFeedbackWriter, build_event_id, decimal_text, dominant_fee, validate_event,
)
from binance_spot_manager.signal_inbox import DuplicateSignal, SignalInbox
from binance_spot_manager.signal_parser import (
    BSM_EXIT_POLICIES, BSM_EXIT_POLICY_HASHES, CSI_POLICY_REGISTRY, ParsedSignal, exit_policy_hash,
    parse_csi_signal, parse_signal,
)
from binance_spot_manager.signal_plan import prepare_signal, tp_sell_percents
from binance_spot_manager.symbol_rules import parse_symbol_rules
from test_automation import (  # noqa: F401
    FakeClient, build_automation, build_execution, events, make_position, rules, settings,
)
from test_commands import processor  # noqa: F401
from test_signal_auto_execution import enabled_preferences, executor
from test_signals import ABK, BICO, GALA, SIMPLE

FIXED = "BSM_MARKET_TP_FIXED_SL_V2"
BREAK_EVEN = "BSM_MARKET_TP_BREAK_EVEN_V2"
REGISTRY = json.loads(CSI_POLICY_REGISTRY.read_text(encoding="utf-8"))["policies"]

# Exemple synthétique de la spécification producteur (tests/test_signals.py de CSI), verbatim.
SPEC_EXAMPLE = """SIGNAL_VERSION=3
SIGNAL_ID=EXAMPLE_ONLY_001
IDEMPOTENCY_KEY=EXAMPLE_ONLY_SETUP_001
DATA_AS_OF=2026-09-29T12:00:00Z
DECISION_AT=2026-09-29T12:00:00Z
CREATED_AT=2026-09-29T12:00:05Z
VALID_FROM=2026-09-29T12:00:05Z
EXPIRES_AT=2026-09-29T12:15:00Z
ENTRY_EXPIRES_AT=2026-09-29T13:00:00Z
MARKET_DATA_SOURCE=SYNTHETIC_FIXTURE
ENVIRONMENT=DEMO
MARKET_TYPE=SPOT
SYMBOL=TESTUSDT
ACTION=BUY
STRATEGY=EMA_PULLBACK_CONTINUATION
STRATEGY_VERSION=1
TIMEFRAME_SETUP=15m
ENTRY_MODE=LIMIT
ENTRY_COUNT=1
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
EXIT_POLICY_HASH=3734d9fcba294de1
MAX_HOLD_MINUTES=1440
RR_REFERENCE=ENTRY_1
RR_TP1_GROSS=1.0
RR_TP2_GROSS=1.5
RR_TP3_GROSS=2.0
RR_TP4_GROSS=3.0
TECHNICAL_SCORE=NONE
ML_PROBABILITY=NONE
MODEL_ID=NONE
ML_TARGET_ID=NONE
ML_HORIZON_MINUTES=NONE
ML_CALIBRATION_ID=NONE
TREND_REGIME=BULL
VOLATILITY_REGIME=NORMAL
NEWS_STATUS=OFF
MAX_ENTRY_DEVIATION_BPS=20
VALIDATION_STATUS=SCHEMA_EXAMPLE_ONLY
STATUS=NEW
---ANALYSIS---
REASONS=EXEMPLE_SYNTHETIQUE_NE_PAS_EXECUTER
"""
# Adapté à une paire Demo réelle, à une politique exécutée par BSM et à un statut exécutable.
ADAPTED = (SPEC_EXAMPLE.replace("SYMBOL=TESTUSDT", "SYMBOL=BTCUSDT")
           .replace("EXIT_POLICY_ID=FIXED_SL_FOUR_TP_V1", f"EXIT_POLICY_ID={FIXED}")
           .replace("EXIT_POLICY_HASH=3734d9fcba294de1", f"EXIT_POLICY_HASH={BSM_EXIT_POLICY_HASHES[FIXED]}")
           .replace("MAX_HOLD_MINUTES=1440", "MAX_HOLD_MINUTES=NONE")
           .replace("VALIDATION_STATUS=SCHEMA_EXAMPLE_ONLY", "VALIDATION_STATUS=DEMO_ELIGIBLE"))


def epoch(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


CREATED = epoch("2026-09-29T12:00:05Z")
EXPIRES = epoch("2026-09-29T12:15:00Z")
ENTRY_EXPIRES = epoch("2026-09-29T13:00:00Z")
NOW = epoch("2026-09-29T12:05:00Z")
SIGNAL_ID = "CSI-20260929T120005Z-TEST0001"
KEY = f"SPOT:BTCUSDT:DONCHIAN_VOLUME_BREAKOUT:v1:20260929T120000Z:{FIXED}"


def reference(entries, weights, basis, rr_reference):
    """Calcul indépendant du prix de référence des RR (schema.reference_price du producteur)."""
    if rr_reference == "ENTRY_1":
        return entries[0]
    if basis == "BASE_QUANTITY":
        return sum(w * p for w, p in zip(weights, entries))
    return 1 / sum(w / p for w, p in zip(weights, entries))


def csi_text(**overrides):
    """Signal V3 BTCUSDT cohérent ; empreinte et RR recalculés sauf s'ils sont imposés."""
    fields = dict(
        SIGNAL_VERSION="3", SIGNAL_ID=SIGNAL_ID, IDEMPOTENCY_KEY=KEY,
        DATA_AS_OF="2026-09-29T12:00:00Z", DECISION_AT="2026-09-29T12:00:00Z",
        CREATED_AT="2026-09-29T12:00:05Z", VALID_FROM="2026-09-29T12:00:05Z",
        EXPIRES_AT="2026-09-29T12:15:00Z", ENTRY_EXPIRES_AT="2026-09-29T13:00:00Z",
        MARKET_DATA_SOURCE="BINANCE_SPOT_PUBLIC", ENVIRONMENT="DEMO", MARKET_TYPE="SPOT", SYMBOL="BTCUSDT",
        ACTION="BUY", STRATEGY="DONCHIAN_VOLUME_BREAKOUT", STRATEGY_VERSION="1", TIMEFRAME_SETUP="15m",
        ENTRY_MODE="LIMIT", ENTRY_COUNT="1", ENTRY_1="84000.00", ENTRY_2="NONE", ENTRY_WEIGHTS="1.0",
        WEIGHT_BASIS="BASE_QUANTITY", STOP_LOSS="80000.00", TP_COUNT="1", TP_1="90000.00", TP_2="NONE",
        TP_3="NONE", TP_4="NONE", TP_WEIGHTS="1.0", EXIT_POLICY_ID=FIXED, EXIT_POLICY_HASH=None,
        MAX_HOLD_MINUTES="NONE", RR_REFERENCE="ENTRY_1", RR_TP1_GROSS=None, RR_TP2_GROSS=None,
        RR_TP3_GROSS=None, RR_TP4_GROSS=None, TECHNICAL_SCORE="NONE", ML_PROBABILITY="NONE", MODEL_ID="NONE",
        ML_TARGET_ID="NONE", ML_HORIZON_MINUTES="NONE", ML_CALIBRATION_ID="NONE", TREND_REGIME="BULL",
        VOLATILITY_REGIME="NORMAL", NEWS_STATUS="OFF", MAX_ENTRY_DEVIATION_BPS="100",
        VALIDATION_STATUS="DEMO_ELIGIBLE", INTEGRATION_STATUS="INTEGRATION_UNVERIFIED", STATUS="NEW",
    )
    fields.update(overrides)
    if fields["EXIT_POLICY_HASH"] is None:
        policy = REGISTRY.get(fields["EXIT_POLICY_ID"])
        fields["EXIT_POLICY_HASH"] = policy["hash"] if policy else "0123456789abcdef"
    entries = [Decimal(fields["ENTRY_1"])] + ([Decimal(fields["ENTRY_2"])] if fields["ENTRY_2"] != "NONE" else [])
    weights = [Decimal(w) for w in fields["ENTRY_WEIGHTS"].split(",")]
    ref = reference(entries, weights, fields["WEIGHT_BASIS"], fields["RR_REFERENCE"])
    stop = Decimal(fields["STOP_LOSS"])
    for index in range(1, 5):
        if fields[f"RR_TP{index}_GROSS"] is None:
            target = fields[f"TP_{index}"]
            fields[f"RR_TP{index}_GROSS"] = "NONE" if target == "NONE" else str(
                ((Decimal(target) - ref) / (ref - stop)).quantize(Decimal("0.001")))
    return "\n".join(f"{key}={value}" for key, value in fields.items()) + "\n---ANALYSIS---\nREASONS=test\n"


def four_tp(**overrides):
    fields = dict(TP_COUNT="4", TP_1="90000.00", TP_2="92000.00", TP_3="94000.00", TP_4="96000.00",
                  TP_WEIGHTS="0.25,0.25,0.25,0.25")
    return csi_text(**(fields | overrides))


def receive_csi(inbox, text, scope="demo"):
    parsed = parse_csi_signal(text)
    assert not parsed.errors, parsed.errors
    return inbox.receive(scope, text, source="api", external_id=f"csi:{parsed.signal_id}",
                         source_timestamp=CREATED, idempotency_key=parsed.idempotency_key,
                         producer_signal_id=parsed.signal_id)


def drop_preferences(**overrides):
    return enabled_preferences(signal_telegram_enabled=False, signal_telegram_auto_enabled=False,
                               signal_drop_enabled=True, signal_drop_auto_enabled=True,
                               signal_drop_auto_enabled_since=900, **overrides)


# ==========================================================================
# Parseur strict V3
# ==========================================================================


def test_spec_example_parses_once_adapted_to_a_demo_pair_and_bsm_policy():
    result = parse_signal(ADAPTED)
    assert result.errors == [] and result.warnings == []
    assert result.template == "csi" and result.is_csi and result.signal_version == 3
    assert result.symbol == "BTCUSDT" and result.direction == "BUY"
    assert result.entries == [100.0] and result.targets == [102.0, 103.0, 104.0, 106.0] and result.stop == 98.0
    assert result.published_at == "2026-09-29T12:00:05+00:00"
    assert result.decision_at == epoch("2026-09-29T12:00:00Z") and result.valid_from == CREATED
    assert result.expires_at == EXPIRES and result.entry_expires_at == ENTRY_EXPIRES
    assert result.entry_count == 1 and result.rr_reference == "ENTRY_1"
    assert result.exit_policy_id == FIXED and result.exit_policy_hash == "0c49448ca40d2a8f"
    assert result.max_hold_minutes is None and result.news_status == "OFF"
    assert result.validation_status == "DEMO_ELIGIBLE"
    assert result.max_entry_deviation_bps == 20.0 and result.tp_weights == [0.25] * 4
    assert parse_signal(ADAPTED.replace("\n", "\r\n")).errors == []
    # L'exemple brut : exemple de schéma ET politique non exécutée par BSM.
    raw = parse_signal(SPEC_EXAMPLE).errors
    assert any("SCHEMA_EXAMPLE_ONLY" in e for e in raw) and any("EXIT_POLICY" in e for e in raw)


@pytest.mark.parametrize("old, new, message", [
    ("TP_1=102.00", "TP_1=102.00\nTP_1=102.00", "dupliquée"),
    ("STATUS=NEW", "STATUS=NEW\nFOO=1", "inconnue"),
    ("ENVIRONMENT=DEMO", "INTENDED_EXECUTION_ENVIRONMENT=DEMO", "inconnue"),
    ("SIGNAL_VERSION=3", "SIGNAL_VERSION=2", "version 2 retirée"),
    ("SIGNAL_VERSION=3", "SIGNAL_VERSION=4", "version non prise en charge"),
    ("RR_TP1_GROSS=1.0", "RR_TP1_GROSS=1.2", "RR_TP1_GROSS attendu"),
    ("TP_3=104.00", "TP_3=102.50", "croissants"),
    ("STOP_LOSS=98.00", "STOP_LOSS=101.00", "STOP_LOSS < ENTRY_1"),
    ("DECISION_AT=2026-09-29T12:00:00Z", "DECISION_AT=2026-09-29T12:00:10Z", "DECISION_AT <= CREATED_AT"),
    ("EXPIRES_AT=2026-09-29T12:15:00Z", "EXPIRES_AT=2026-09-29T12:00:05Z", "VALID_FROM < EXPIRES_AT"),
    ("ENTRY_EXPIRES_AT=2026-09-29T13:00:00Z", "ENTRY_EXPIRES_AT=2026-09-29T12:10:00Z", "EXPIRES_AT <= ENTRY_EXPIRES_AT"),
    ("ENVIRONMENT=DEMO", "ENVIRONMENT=LIVE", "DEMO"),
    ("MARKET_TYPE=SPOT", "MARKET_TYPE=FUTURES", "SPOT"),
    ("ACTION=BUY", "ACTION=SELL", "BUY"),
    ("ENTRY_MODE=LIMIT", "ENTRY_MODE=MARKET", "LIMIT"),
    ("ENTRY_2=NONE", "ENTRY_2=99.00", "ENTRY_COUNT incohérent"),
    ("ENTRY_COUNT=1", "ENTRY_COUNT=3", "ENTRY_COUNT"),
    ("ENTRY_WEIGHTS=1.0", "ENTRY_WEIGHTS=0.5,0.5", "ENTRY_WEIGHTS"),
    ("ENTRY_WEIGHTS=1.0", "ENTRY_WEIGHTS=0.9", "ENTRY_WEIGHTS doit sommer"),
    ("RR_REFERENCE=ENTRY_1", "RR_REFERENCE=WEIGHTED_ENTRY", "RR_REFERENCE=ENTRY_1 requis"),
    ("RR_REFERENCE=ENTRY_1", "RR_REFERENCE=AVERAGE", "RR_REFERENCE"),
    ("SYMBOL=BTCUSDT", "SYMBOL=BTCEUR", "USDT/USDC"),
    ("SYMBOL=BTCUSDT", "SYMBOL = BTCUSDT", "CLE=VALEUR"),
    ("ENTRY_1=100.00", "ENTRY_1=100,00", "décimal"),
    ("ENTRY_1=100.00", "ENTRY_1=NaN", "décimal"),
    ("TP_WEIGHTS=0.25,0.25,0.25,0.25", "TP_WEIGHTS=0.3,0.25,0.25,0.25", "sommer"),
    ("TP_COUNT=4", "TP_COUNT=3", "au-delà de TP_COUNT"),
    ("TP_4=106.00", "TP_4=NONE", "TP_COUNT incohérent"),
    ("CREATED_AT=2026-09-29T12:00:05Z", "CREATED_AT=2026-09-29 12:00:05", "format"),
    ("STOP_LOSS=98.00", "STOP_LOSS=NONE", "obligatoire"),
    ("MAX_ENTRY_DEVIATION_BPS=20", "MAX_ENTRY_DEVIATION_BPS=501", "MAX_ENTRY_DEVIATION_BPS"),
    ("MAX_ENTRY_DEVIATION_BPS=20", "MAX_ENTRY_DEVIATION_BPS=NONE", "obligatoire"),
    ("MAX_HOLD_MINUTES=NONE", "MAX_HOLD_MINUTES=1440", "MAX_HOLD_MINUTES"),
    ("NEWS_STATUS=OFF", "NEWS_STATUS=NO_BAD_NEWS", "NEWS_STATUS"),
    ("ML_PROBABILITY=NONE", "ML_PROBABILITY=0.6", "vont ensemble"),
    ("EXIT_POLICY_HASH=0c49448ca40d2a8f", "EXIT_POLICY_HASH=0123456789abcdef", "EXIT_POLICY_HASH attendu"),
    ("EXIT_POLICY_HASH=0c49448ca40d2a8f", "EXIT_POLICY_HASH=XYZ", "EXIT_POLICY_HASH"),
    ("STATUS=NEW", "STATUS=FILLED", "NEW"),
    ("SYMBOL=BTCUSDT\n", "", "manquantes"),
], ids=lambda value: value if isinstance(value, str) and len(value) < 40 else None)
def test_strict_parser_rejects(old, new, message):
    assert old in ADAPTED, old
    result = parse_signal(ADAPTED.replace(old, new, 1))
    assert result.errors, "le texte muté devait être refusé"
    assert any(message in error for error in result.errors), result.errors


def test_ml_fields_go_together_and_probability_is_bounded():
    ml = dict(ML_PROBABILITY="0.62", MODEL_ID="LGBM_1", ML_TARGET_ID="TP1_BEFORE_SL", ML_HORIZON_MINUTES="240",
              ML_CALIBRATION_ID="ISOTONIC_1")
    assert parse_csi_signal(csi_text(**ml)).errors == []
    assert any("[0, 1]" in e for e in parse_csi_signal(csi_text(**(ml | {"ML_PROBABILITY": "1.2"}))).errors)
    assert any("vont ensemble" in e for e in parse_csi_signal(csi_text(**(ml | {"ML_TARGET_ID": "NONE"}))).errors)
    assert parse_csi_signal(csi_text(NEWS_STATUS="GATE_CLEAR")).errors == []


@pytest.mark.parametrize("policy_id", sorted(set(REGISTRY) - {FIXED, BREAK_EVEN}))
def test_policies_not_executed_by_bsm_are_refused(policy_id):
    text = csi_text(EXIT_POLICY_ID=policy_id, MAX_HOLD_MINUTES="1440")
    errors = parse_csi_signal(text).errors
    assert any("EXIT_POLICY" in e and "non exécutée" in e for e in errors), errors
    assert any("EXIT_POLICY" in e for e in parse_csi_signal(csi_text(EXIT_POLICY_ID="UNKNOWN_V9")).errors)


def test_two_entries_are_refused_by_policy_after_a_correct_weighted_rr():
    base = dict(ENTRY_COUNT="2", ENTRY_1="84000.00", ENTRY_2="82000.00", ENTRY_WEIGHTS="0.5,0.5",
                RR_REFERENCE="WEIGHTED_ENTRY")
    for basis in ("BASE_QUANTITY", "QUOTE_BUDGET"):
        errors = parse_csi_signal(csi_text(WEIGHT_BASIS=basis, **base)).errors
        # RR recalculés sur le prix moyen prévu : seule la politique refuse.
        assert len(errors) == 1 and "EXIT_POLICY" in errors[0] and "au plus 1" in errors[0], errors
    wrong_rr = csi_text(RR_TP1_GROSS="1.500", **base)
    assert any("RR_TP1_GROSS attendu" in e for e in parse_csi_signal(wrong_rr).errors)
    disordered = csi_text(**(base | {"ENTRY_2": "85000.00"}))
    assert any("STOP_LOSS < ENTRY_2 < ENTRY_1" in e for e in parse_csi_signal(disordered).errors)


def test_every_non_example_status_is_accepted_by_the_parser():
    for status in ("RESEARCH", "VALIDATED_OOS", "SHADOW", "DEMO_ELIGIBLE"):
        assert parse_csi_signal(csi_text(VALIDATION_STATUS=status)).errors == []


def test_analysis_section_cannot_change_the_contract():
    result = parse_signal(ADAPTED + "STOP_LOSS=1\nSIGNAL_VERSION=9\nFOO=BAR\n")
    assert result.errors == [] and result.stop == 98.0


def test_csi_and_legacy_templates_never_cross():
    assert parse_signal(SIMPLE, "csi").errors
    for template in ("structured", "abk", "numbered", "simple"):
        assert parse_signal(ADAPTED, template).errors
    assert parse_signal(ADAPTED, "csi").to_dict() == parse_csi_signal(ADAPTED).to_dict()
    stray = "PAIR: BTC/USDT\nSIGNAL_VERSION=3\nENTRY 1: 84000\nT1: 90000\nSL: 80000"
    assert any("SIGNAL_VERSION" in error for error in parse_signal(stray).errors)
    assert parse_csi_signal(SIMPLE).errors
    for legacy in (SIMPLE, BICO, GALA, ABK):
        parsed = parse_signal(legacy)
        assert parsed.errors == [] and parsed.signal_version == 1 and not parsed.is_csi


def test_rows_parsed_before_v3_still_load():
    legacy = {"template": "simple", "symbol": "BTCUSDT", "direction": "BUY", "exchange": "", "entries": [84000.0],
              "targets": [90000.0], "stop": 80000.0, "stop_timeframe": "", "published_at": "", "errors": [],
              "warnings": []}
    parsed = ParsedSignal(**legacy)
    assert parsed.signal_version == 1 and parsed.entry_expires_at == 0.0 and parsed.exit_policy_hash == ""
    v2_row = legacy | {"signal_version": 2, "signal_id": "X", "expires_at": 1.0, "exit_policy_id": "FIXED_SL_ONE_TP_V1"}
    assert not ParsedSignal(**v2_row).is_csi
    assert ParsedSignal(**parse_signal(ADAPTED).to_dict()).is_csi


# ==========================================================================
# Registre des politiques
# ==========================================================================


def csi_root():
    candidates = [Path(os.environ["CSI_PATH"])] if os.environ.get("CSI_PATH") else []
    here = Path(__file__).resolve()
    candidates += [parent / "CryptoSignalIntelligence" for parent in list(here.parents)[:6]]
    return next((root for root in candidates if (root / "config" / "exit_policies.json").exists()), None)


def test_v2_policies_have_the_expected_hashes_and_v1_are_refused():
    assert BSM_EXIT_POLICY_HASHES == {FIXED: "0c49448ca40d2a8f", BREAK_EVEN: "b34dee623d3b61ed"}
    for policy_id in ("BSM_MARKET_TP_FIXED_SL_V1", "BSM_MARKET_TP_BREAK_EVEN_V1"):
        assert policy_id in REGISTRY  # connue du producteur, mais pas exactement BSM
        errors = parse_csi_signal(csi_text(EXIT_POLICY_ID=policy_id)).errors
        assert any("EXIT_POLICY" in e and "non exécutée" in e for e in errors), errors


def test_policy_copy_is_self_consistent_and_matches_bsm_rules():
    for policy_id, policy in REGISTRY.items():
        assert exit_policy_hash(policy["rules"]) == policy["hash"], policy_id
    for policy_id, rules in BSM_EXIT_POLICIES.items():
        # Les règles déclarées par BSM sont exactement celles du registre : même empreinte.
        assert REGISTRY[policy_id]["rules"] == rules
        assert BSM_EXIT_POLICY_HASHES[policy_id] == REGISTRY[policy_id]["hash"]


@pytest.mark.skipif(csi_root() is None, reason="dépôt CryptoSignalIntelligence introuvable : définir CSI_PATH")
def test_policy_copy_matches_the_producer_registry():
    producer = json.loads((csi_root() / "config" / "exit_policies.json").read_text(encoding="utf-8"))
    assert json.loads(CSI_POLICY_REGISTRY.read_text(encoding="utf-8")) == producer


# ==========================================================================
# Dépôt TXT et idempotence
# ==========================================================================


def drop_importer(tmp_path, *, now=NOW, feedback=None, inbox=None):
    inbox = inbox or SignalInbox(tmp_path / "signals.db")
    importer = SignalDropImporter(inbox, "demo", lambda: {"signal_drop_enabled": True},
                                  directory=tmp_path / "drop", clock=lambda: now, feedback=feedback)
    return importer, inbox


def drop_file(tmp_path, name, text):
    incoming = tmp_path / "drop" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    (incoming / name).write_bytes(text.encode("utf-8"))


def reason_of(tmp_path, name):
    return (tmp_path / "drop" / "rejected" / f"{name}.reason.txt").read_text(encoding="utf-8")


def test_txt_file_is_imported_with_csi_identity(tmp_path):
    importer, inbox = drop_importer(tmp_path)
    drop_file(tmp_path, f"{SIGNAL_ID}.txt", csi_text())
    drop_file(tmp_path, "CSI-later.txt.tmp", "SIGNAL_VERSION=3\n")

    rows = importer.import_pending()

    assert len(rows) == 1
    row = inbox.recent("demo")[0]
    assert row["source"] == "api" and row["external_id"] == f"csi:{SIGNAL_ID}"
    assert row["source_timestamp"] == CREATED and row["expires_at"] == EXPIRES
    assert row["parsed"]["signal_version"] == 3 and row["parsed"]["errors"] == []
    assert (tmp_path / "drop" / "processed" / f"{SIGNAL_ID}.txt").exists()
    assert [p.name for p in (tmp_path / "drop" / "incoming").iterdir()] == ["CSI-later.txt.tmp"]
    assert (tmp_path / "drop" / "outgoing").is_dir()
    keys = inbox.signal_keys("demo")
    assert len(keys) == 1 and keys[0]["signal_id"] == SIGNAL_ID and keys[0]["row_id"] == row["id"]


def test_txt_expired_at_reception_is_rejected_even_if_entry_is_still_valid(tmp_path):
    importer, inbox = drop_importer(tmp_path, now=EXPIRES + 60)  # < ENTRY_EXPIRES_AT
    drop_file(tmp_path, "late.txt", csi_text())

    assert importer.import_pending() == []
    assert "expiré à la réception" in reason_of(tmp_path, "late.txt")
    assert inbox.recent("demo") == [] and inbox.signal_keys("demo") == []


@pytest.mark.parametrize("text, message", [
    (csi_text(STATUS="NEW\nFOO=1"), "clé inconnue"),
    (csi_text(RR_TP1_GROSS="9.999"), "RR_TP1_GROSS"),
    (csi_text(EXIT_POLICY_ID="FIXED_SL_ONE_TP_V1", MAX_HOLD_MINUTES="1440"), "EXIT_POLICY"),
    ("SIGNAL_VERSION=2\nSIGNAL_ID=CSI-OLD\nSYMBOL=BTCUSDT\n", "version 2 retirée"),
    ("PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000\n", "CLE=VALEUR"),
    (SPEC_EXAMPLE, "SCHEMA_EXAMPLE_ONLY"),
], ids=["unknown-key", "rr", "policy", "v2", "legacy-text", "schema-example"])
def test_txt_parse_errors_are_rejected_with_reason(tmp_path, text, message):
    importer, inbox = drop_importer(tmp_path)
    drop_file(tmp_path, "bad.txt", text)

    assert importer.import_pending() == []
    assert message in reason_of(tmp_path, "bad.txt")
    assert inbox.recent("demo") == []


def test_duplicate_idempotency_key_and_replays(tmp_path):
    importer, inbox = drop_importer(tmp_path)
    drop_file(tmp_path, "first.txt", csi_text())
    assert len(importer.import_pending()) == 1
    first = inbox.recent("demo")[0]

    drop_file(tmp_path, "second.txt", csi_text(SIGNAL_ID="CSI-20260929T120005Z-TEST0002"))
    assert importer.import_pending() == []
    assert "Doublon" in reason_of(tmp_path, "second.txt")
    drop_file(tmp_path, "third.txt", csi_text(IDEMPOTENCY_KEY=KEY + ":B"))
    assert importer.import_pending() == []
    assert "Doublon" in reason_of(tmp_path, "third.txt")
    drop_file(tmp_path, "first.txt", csi_text())
    replay = importer.import_pending()
    assert len(replay) == 1 and replay[0]["id"] == first["id"]
    assert len(inbox.recent("demo")) == 1 and len(inbox.signal_keys("demo")) == 1


def test_inbox_registry_is_written_with_the_signal(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = receive_csi(inbox, csi_text())
    with pytest.raises(DuplicateSignal) as duplicate:
        receive_csi(inbox, csi_text(SIGNAL_ID="CSI-OTHER"))
    assert not duplicate.value.same_signal and duplicate.value.signal_id == SIGNAL_ID
    with pytest.raises(DuplicateSignal) as same:
        receive_csi(inbox, csi_text(IDEMPOTENCY_KEY=KEY + ":B"))
    assert same.value.same_signal
    assert receive_csi(inbox, csi_text())["id"] == row["id"]
    assert receive_csi(inbox, csi_text(), scope="other")["id"] != row["id"]
    with pytest.raises(ValueError, match="requis ensemble"):
        inbox.receive("demo", csi_text(), idempotency_key=KEY)


# ==========================================================================
# Exécution automatique
# ==========================================================================


def test_row_is_queued_within_window_with_both_expirations_frozen(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = receive_csi(inbox, csi_text())
    worker, commands = executor(tmp_path, inbox, drop_preferences(), now=CREATED + 600)  # 10 min > 5 min

    assert worker.process_pending() == ["QUEUED"]
    payload = commands.get_by_request_key("demo", f"signal:{row['id']}")["payload"]
    assert payload["signal_expires_at"] == EXPIRES and payload["entry_expires_at"] == ENTRY_EXPIRES
    assert payload["max_entry_deviation_bps"] == 100.0 and payload["signal_entry_price"] == 84000.0
    assert payload["exit_policy_id"] == FIXED and payload["exit_policy_hash"] == BSM_EXIT_POLICY_HASHES[FIXED]
    position = payload["position"]
    # L'ordre d'entrée vit jusqu'à ENTRY_EXPIRES_AT, pas jusqu'à EXPIRES_AT.
    assert datetime.fromisoformat(position["entries"][0]["expires_at"]).timestamp() == ENTRY_EXPIRES
    assert position["automation"]["merge_below_minimum_tp"] is True
    assert position["automation"]["cancel_remaining_entries_on_first_tp"] is True
    assert position["automation"]["tp_execution_policy"] == "MARKET_ON_TRIGGER"
    assert position["stop_loss"]["limit_offset_percent"] == 0.3
    assert position["tags"] == ["signal", row["id"]]
    assert worker.process_pending() == []


@pytest.mark.parametrize("status", ["RESEARCH", "VALIDATED_OOS", "SHADOW"])
def test_only_demo_eligible_is_executed_automatically(tmp_path, status):
    inbox = SignalInbox(tmp_path / "signals.db")
    receive_csi(inbox, csi_text(VALIDATION_STATUS=status))
    worker, commands = executor(tmp_path, inbox, drop_preferences(), now=NOW)

    assert worker.process_pending() == ["REJECTED"]
    assert f"VALIDATION_STATUS={status}" in inbox.recent("demo")[0]["auto_detail"]
    assert commands.list_recent("demo") == []


def test_expired_row_is_rejected_with_reason(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    receive_csi(inbox, csi_text())
    worker, commands = executor(tmp_path, inbox, drop_preferences(), now=EXPIRES)

    assert worker.process_pending() == ["REJECTED"]
    assert "expiré" in inbox.recent("demo")[0]["auto_detail"]
    assert commands.list_recent("demo") == []


def test_stored_v2_row_is_never_executed(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = receive_csi(inbox, csi_text())
    parsed = dict(row["parsed"], signal_version=2)
    with inbox.connect() as db, db:
        db.execute("UPDATE signals SET parsed=? WHERE id=?", (json.dumps(parsed), row["id"]))
    worker, commands = executor(tmp_path, inbox, drop_preferences(), now=NOW)

    assert worker.process_pending() == ["REJECTED"]
    assert "version 2 retiré" in inbox.recent("demo")[0]["auto_detail"]
    assert commands.list_recent("demo") == []


def test_not_yet_valid_row_waits_without_being_claimed(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    receive_csi(inbox, csi_text(VALID_FROM="2026-09-29T12:10:00Z"))
    worker, _ = executor(tmp_path, inbox, drop_preferences(), now=NOW)

    assert worker.process_pending() == ["WAITING"]
    assert inbox.recent("demo")[0]["auto_state"] == ""
    later, _ = executor(tmp_path / "later", inbox, drop_preferences(), now=epoch("2026-09-29T12:10:00Z"))
    assert later.process_pending() == ["QUEUED"]


def test_price_deviation_beyond_contract_is_rejected(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    receive_csi(inbox, csi_text(MAX_ENTRY_DEVIATION_BPS="20"))  # 84500 vs 84000 : 59,5 bps
    worker, commands = executor(tmp_path, inbox, drop_preferences(), now=NOW)

    assert worker.process_pending() == ["REJECTED"]
    detail = inbox.recent("demo")[0]["auto_detail"]
    assert "59.5 bps" in detail and "MAX_ENTRY_DEVIATION_BPS 20" in detail
    assert commands.list_recent("demo") == []


@pytest.mark.parametrize("policy, rules_after", [
    (FIXED, ["NO_CHANGE"] * 4),
    (BREAK_EVEN, ["BREAK_EVEN"] * 3 + ["NO_CHANGE"]),
])
def test_exit_policy_maps_to_sl_rule_over_saved_preference(tmp_path, policy, rules_after):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = receive_csi(inbox, four_tp(EXIT_POLICY_ID=policy, IDEMPOTENCY_KEY=f"{KEY}:{policy}"))
    worker, commands = executor(tmp_path, inbox, drop_preferences(signal_sl_after_tp="PREVIOUS_TP"), now=NOW)

    assert worker.process_pending() == ["QUEUED"]
    payload = commands.get_by_request_key("demo", f"signal:{row['id']}")["payload"]
    assert [tp["sl_rule_after_hit"] for tp in payload["position"]["take_profits"]] == rules_after


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


def prepare_csi(text, btc_rules, **overrides):
    kwargs = dict(budget=300, available_quote=1000, reserve_percent=20, current_price=84500,
                  signal_id="row", source="api", validity_confirmed=True)
    return prepare_signal(parse_signal(text), btc_rules, **(kwargs | overrides))


def test_tp_weights_become_remaining_percentages(btc_rules):
    text = csi_text(TP_COUNT="3", TP_1="90000.00", TP_2="92000.00", TP_3="94000.00", TP_WEIGHTS="0.5,0.3,0.2",
                    EXIT_POLICY_ID=BREAK_EVEN)
    plan, payload = prepare_csi(text, btc_rules)
    position = payload["position"]
    assert [tp.sell_percent for tp in plan.take_profits] == pytest.approx([50, 30, 20])
    assert [tp["sell_percent"] for tp in position["take_profits"]] == pytest.approx([50, 60, 100])
    assert [tp["sl_rule_after_hit"] for tp in position["take_profits"]] == ["BREAK_EVEN", "BREAK_EVEN", "NO_CHANGE"]
    assert tp_sell_percents([0.25] * 4) == pytest.approx([25, 100 / 3, 50, 100])


def test_minimum_tp_quantity_uses_the_smallest_weight(btc_rules):
    balanced = csi_text(TP_COUNT="2", TP_1="90000.00", TP_2="92000.00", TP_WEIGHTS="0.5,0.5")
    skewed = csi_text(TP_COUNT="2", TP_1="90000.00", TP_2="92000.00", TP_WEIGHTS="0.98,0.02")
    prepare_csi(balanced, btc_rules, budget=200)
    with pytest.raises(ValueError, match="tranches TP"):
        prepare_csi(skewed, btc_rules, budget=200)
    prepare_csi(skewed, btc_rules, budget=600)


def test_preparation_refuses_retired_or_tampered_contracts(btc_rules):
    parsed = parse_signal(csi_text())
    with pytest.raises(ValueError, match="retiré"):
        prepare_signal(ParsedSignal(**(parsed.to_dict() | {"signal_version": 2})), btc_rules, budget=300,
                       available_quote=1000, reserve_percent=20, current_price=84500, signal_id="r",
                       validity_confirmed=True)
    with pytest.raises(ValueError, match="EXIT_POLICY_HASH"):
        prepare_signal(ParsedSignal(**(parsed.to_dict() | {"exit_policy_hash": "0123456789abcdef"})), btc_rules,
                       budget=300, available_quote=1000, reserve_percent=20, current_price=84500, signal_id="r",
                       validity_confirmed=True)


def test_legacy_preparation_keeps_24h_expiry_and_equal_slices(btc_rules):
    _, payload = prepare_csi(SIMPLE.replace("T1: 90000", "T1: 90000\nT2: 95000\nT3: 100000"), btc_rules)
    position = payload["position"]
    assert [tp["sell_percent"] for tp in position["take_profits"]] == pytest.approx([100 / 3, 50, 100])
    assert position["automation"]["merge_below_minimum_tp"] is False
    expiry = datetime.fromisoformat(position["entries"][0]["expires_at"])
    assert timedelta(hours=23) < expiry - utcnow() <= timedelta(hours=24)
    assert "signal_expires_at" not in payload and "exit_policy_hash" not in payload


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
    } | payload_extra, request_key=f"csi-{len(worker.store.list_recent(worker.scope))}")
    return worker.run_one()


def test_worker_refuses_expired_signal_and_excess_deviation_before_any_order(processor):
    worker, calls = processor
    assert submit(worker, {"signal_expires_at": NOW}) == "FAILED"
    assert "EXPIRES_AT" in worker.store.list_recent(worker.scope)[0]["result"]["message"]
    future = utcnow().timestamp() + 3600
    assert submit(worker, {"signal_expires_at": future, "max_entry_deviation_bps": 5,
                           "signal_entry_price": 84000}, price=84100) == "FAILED"
    assert "bps" in worker.store.list_recent(worker.scope)[0]["result"]["message"]
    assert calls == []
    assert submit(worker, {"signal_expires_at": future, "max_entry_deviation_bps": 20,
                           "signal_entry_price": 84000}, price=84100) == "SUCCEEDED"
    assert len(calls) == 1


# ==========================================================================
# Politique : report des tranches sous minimums, positions expirées
# ==========================================================================


def small_position(rules, *, merge):
    position = make_position(rules)
    position.entries[0].executed_qty = 0.0001
    position.entries[0].commissions = []
    position.take_profits[0].sell_percent = 10.0  # 0.00001 BTC ≈ 0,87 USDT < minNotional 5
    position.take_profits[1].sell_percent = 50.0
    position.take_profits.append(TakeProfit(sequence_number=3, target_price=95000.0, sell_percent=100.0))
    position.automation.merge_below_minimum_tp = merge
    recompute_position(position)
    return position


def test_below_minimum_tranche_is_merged_into_next_tp(engine, events):  # noqa: F811
    execution, fake, rules = engine
    position = small_position(rules, merge=True)
    position.automation.cancel_remaining_entries_on_first_tp = True
    canceled = []
    execution.cancel_open_entries = lambda p: canceled.append(p.position_id) or []

    result = build_automation(execution, rules, events).run_cycle(position, 86600.0)

    tp1, tp2, tp3 = position.sorted_tps
    assert tp1.status is TPStatus.CANCELED and "reportee sur le TP 2" in tp1.last_error
    assert tp2.sell_percent == pytest.approx(55.0)  # 1 − 0,9 × 0,5 : TP2 reprend la part de TP1
    assert tp3.sell_percent == 100.0
    assert not any(order["type"] == "MARKET" for order in fake.created)
    assert canceled == [position.position_id] and not result.errors


def test_without_the_policy_flag_a_below_minimum_tp_keeps_failing(engine, events):  # noqa: F811
    execution, fake, rules = engine
    position = small_position(rules, merge=False)

    build_automation(execution, rules, events).run_cycle(position, 86600.0)

    assert position.sorted_tps[0].status is TPStatus.FAILED
    assert position.sorted_tps[1].sell_percent == 50.0


@pytest.fixture
def engine(settings, rules, events):  # noqa: F811
    fake = FakeClient(rules)
    fake.seed_stop_loss()
    return build_execution(settings, rules, events, fake), fake, rules


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


def test_expired_unfilled_entry_closes_the_position(settings, rules, events, tmp_path):  # noqa: F811
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


def test_partially_filled_expired_entry_keeps_the_position_open(settings, rules, events):  # noqa: F811
    fake = FakeClient(rules)
    seed_entry_order(fake, executed="0.001")
    execution = build_execution(settings, rules, events, fake)
    position = pending_position(executed_qty=0.001, status=EntryStatus.PARTIALLY_FILLED)

    result = build_automation(execution, rules, events).run_cycle(position, 84000.0)

    assert not result.position_finished
    assert position.is_open and position.metrics.net_qty == pytest.approx(0.001)


def test_already_terminal_entries_without_fill_are_closed_next_cycle(settings, rules, events):  # noqa: F811
    fake = FakeClient(rules)
    execution = build_execution(settings, rules, events, fake)
    position = pending_position(status=EntryStatus.CANCELED)

    result = build_automation(execution, rules, events).run_cycle(position, 84000.0)

    assert result.position_finished and position.close_reason is CloseReason.CANCELED_BEFORE_FILL
    assert fake.created == [] and fake.cancelled == []


# ==========================================================================
# Fichier de retour d'exécution v2
# ==========================================================================


def csi_feedback_model():
    """Modèle pydantic du producteur, chargé depuis son fichier si le dépôt est accessible."""
    root = csi_root()
    schema = root / "src" / "crypto_signal_intelligence" / "feedback" / "schema.py" if root else None
    if schema is None or not schema.exists():
        return None
    spec = importlib.util.spec_from_file_location("csi_feedback_schema", schema)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CSI_SCHEMA = csi_feedback_model()
FIELD_ORDER = ["event_id", "signal_id", "event_type", "occurred_at", "environment", "producer", "symbol",
               "quantity", "price", "quote_quantity", "fee", "fee_asset", "order_id", "target_index", "reason",
               "exit_policy_hash"]


def assert_contract(lines):
    """Chaque ligne satisfait le contrat, localement et (si disponible) chez le producteur."""
    events = [json.loads(line) for line in lines]
    for event in events:
        validate_event(event)
        assert list(event) == FIELD_ORDER
        assert event["environment"] == "DEMO" and event["producer"] == "BinanceSpotManager"
        for key in ("quantity", "price", "quote_quantity", "fee"):
            assert event[key] is None or (isinstance(event[key], str) and Decimal(event[key]).is_finite())
        assert event["fee"] != "0"
        if CSI_SCHEMA is not None:
            CSI_SCHEMA.parse_line(json.dumps(event))
    return events


class TradesClient:
    """myTrades Binance Demo simulé (lecture seule)."""

    def __init__(self):
        self.trades = {}
        self.calls = []

    def add(self, order_id, qty, price, commission, asset, stamp):
        self.trades.setdefault(order_id, []).append({
            "orderId": order_id, "qty": str(qty), "price": str(price), "commission": str(commission),
            "commissionAsset": asset, "time": int(stamp * 1000)})

    def get_my_trades(self, symbol, *, order_id=None, limit=500):
        self.calls.append(order_id)
        return list(self.trades.get(order_id, []))


def feedback_setup(tmp_path, *, enabled=True, now=NOW, client=None):
    inbox = SignalInbox(tmp_path / "signals.db")
    commands = CommandStore(tmp_path / "commands.db")
    store = PositionStore(tmp_path / "positions")
    writer = SignalFeedbackWriter("demo", inbox, commands, positions=store, client=client,
                                  directory=tmp_path / "outgoing", registry_path=tmp_path / "feedback.db",
                                  clock=lambda: now, enabled=enabled)
    return inbox, commands, store, writer


def lines_of(writer):
    return writer.path.read_text(encoding="utf-8").splitlines() if writer.path.exists() else []


def queue_signal(inbox, commands, writer, text, btc_rules):
    row = receive_csi(inbox, text)
    writer.register(parse_csi_signal(text).signal_id, row["id"], "BTCUSDT")
    _, payload = prepare_signal(parse_signal(text), btc_rules, budget=300, available_quote=1000,
                                reserve_percent=20, current_price=84500, signal_id=row["id"], source="api",
                                validity_confirmed=True)
    command = commands.enqueue("demo", "SUBMIT_POSITION", payload, request_key=f"signal:{row['id']}")
    position = Position.model_validate(payload["position"])
    return row, command, position


def succeed(commands, command):
    commands.claim("demo")
    commands.finish("demo", command["id"], "SUCCEEDED", {"message": "ok"})


def test_feedback_lifecycle_with_real_fees_from_my_trades(tmp_path, btc_rules):
    client = TradesClient()
    inbox, commands, store, writer = feedback_setup(tmp_path, client=client)
    row, command, position = queue_signal(inbox, commands, writer, four_tp(), btc_rules)
    assert writer.sync([]) == 1  # mise en file : RECEIVED avec l'empreinte vérifiée
    succeed(commands, command)
    entry = position.entries[0]
    entry.status, entry.order_id, entry.client_order_id = EntryStatus.SUBMITTED, 777, "BSM-D-BTC-x-E1"
    entry.submitted_at = datetime.fromtimestamp(NOW, tz=timezone.utc)
    store.save(position)
    assert writer.sync([position]) == 1  # ORDER_PLACED
    engine = PositionEngine(btc_rules)
    client.add(777, "0.001", "84000", "0.000001", "BTC", NOW + 10)
    engine.apply_entry_fill(position, entry.entry_id, executed_qty=0.001, average_price=84000, quote_spent=84,
                            order_id=777)
    assert writer.sync([position]) == 1 and writer.sync([position]) == 0
    remaining = round(entry.binance_qty - 0.001, 8)
    client.add(777, remaining, "84000", "0.0000025", "BTC", NOW + 20)
    engine.apply_entry_fill(position, entry.entry_id, executed_qty=entry.binance_qty, average_price=84000,
                            quote_spent=84000 * entry.binance_qty, order_id=777)
    assert writer.sync([position]) == 1
    tp1 = position.sorted_tps[0]
    client.add(778, "0.0003", "90000", "0.00002", "BNB", NOW + 30)
    client.add(778, "0.0002", "90000", "0.00001", "BNB", NOW + 30)
    client.add(778, "0.0001", "90000", "0.009", "USDT", NOW + 31)
    engine.apply_tp_fill(position, tp1.tp_id, executed_qty=0.0006, average_price=90000, quote_received=54,
                         commissions=[Commission(asset="BNB", amount=0.00003), Commission(asset="USDT", amount=0.009)],
                         order_id=778)
    assert writer.sync([position]) == 1
    position.stop_loss.order_id = 779
    left = position.metrics.net_qty
    client.add(779, left, "79900", "0.2", "USDT", NOW + 40)
    engine.apply_sl_fill(position, executed_qty=left, average_price=79900, quote_received=79900 * left)
    store.save(position)
    assert writer.sync([]) == 2  # STOP_FILLED + CLOSED, position rechargée par identifiant
    assert writer.sync([position]) == 0

    events = assert_contract(lines_of(writer))
    assert [e["event_type"] for e in events] == [
        "RECEIVED", "ORDER_PLACED", "ENTRY_PARTIAL", "ENTRY_FILLED", "TP_FILLED", "STOP_FILLED", "CLOSED"]
    received, placed, partial, filled, tp, stop, closed = events
    assert received["exit_policy_hash"] == BSM_EXIT_POLICY_HASHES[FIXED]
    assert received["event_id"] == f"BSM-{SIGNAL_ID}-RECEIVED-1"
    assert all(e["exit_policy_hash"] is None for e in events[1:])
    assert (placed["order_id"], placed["price"], placed["quantity"]) == ("777", "84000", decimal_text(entry.binance_qty))
    assert placed["occurred_at"] == "2026-09-29T12:05:00Z"
    assert (partial["quantity"], partial["price"], partial["fee"], partial["fee_asset"]) == ("0.001", "84000", "0.000001", "BTC")
    assert partial["occurred_at"] == "2026-09-29T12:05:10Z"  # heure de l'exécution Binance
    assert filled["fee"] == "0.0000025" and filled["occurred_at"] == "2026-09-29T12:05:20Z"
    # Deux devises sur un même remplissage : la plus fréquente (BNB, 2 exécutions sur 3).
    assert (tp["target_index"], tp["fee"], tp["fee_asset"]) == (1, "0.00003", "BNB")
    assert (stop["price"], stop["fee"], stop["fee_asset"]) == ("79900", "0.2", "USDT")
    assert closed["reason"] is None
    assert set(client.calls) == {777, 778, 779}
    assert writer.registry.pending() == []


def test_market_exit_outside_stop_and_tp_is_reported_with_reason(tmp_path, btc_rules):
    client = TradesClient()
    inbox, commands, store, writer = feedback_setup(tmp_path, client=client)
    row, command, position = queue_signal(inbox, commands, writer, csi_text(), btc_rules)
    succeed(commands, command)
    entry = position.entries[0]
    entry.order_id = 801
    client.add(801, entry.binance_qty, "84000", "0.08", "USDT", NOW + 5)
    PositionEngine(btc_rules).apply_entry_fill(position, entry.entry_id, executed_qty=entry.binance_qty,
                                               average_price=84000, quote_spent=84000 * entry.binance_qty,
                                               order_id=801)
    sale = ManualExit(client_order_id="BSM-D-BTC-x-MC1", order_id=802, requested_qty=entry.binance_qty,
                      executed_qty=entry.binance_qty, quote_received=79000 * entry.binance_qty,
                      average_fill_price=79000, status="FILLED", close_reason=CloseReason.STOP_CROSSED)
    position.manual_exits.append(sale)
    recompute_position(position)
    finish_position(position, CloseReason.STOP_CROSSED)
    # Aucune exécution visible pour 802 : attente des frais réels, puis écriture sans frais.
    assert writer.sync([position], now=NOW) == 3  # RECEIVED, ORDER_PLACED, ENTRY_FILLED ; sortie en attente
    assert writer.sync([position], now=NOW + 10) == 0
    assert writer.sync([position], now=NOW + 31) == 2  # MARKET_EXIT_FILLED sans frais + CLOSED

    events = assert_contract(lines_of(writer))
    assert [e["event_type"] for e in events] == [
        "RECEIVED", "ORDER_PLACED", "ENTRY_FILLED", "MARKET_EXIT_FILLED", "CLOSED"]
    exit_event = events[3]
    assert "stop déjà franchi" in exit_event["reason"] and exit_event["price"] == "79000"
    assert exit_event["fee"] is None and exit_event["fee_asset"] is None
    assert exit_event["order_id"] == "802"


def test_without_client_known_commissions_are_used_and_unknown_fees_omitted(tmp_path, btc_rules):
    inbox, commands, store, writer = feedback_setup(tmp_path)
    row, command, position = queue_signal(inbox, commands, writer, csi_text(), btc_rules)
    succeed(commands, command)
    entry = position.entries[0]
    PositionEngine(btc_rules).apply_entry_fill(position, entry.entry_id, executed_qty=0.001, average_price=84000,
                                               quote_spent=84, order_id=901)
    assert writer.sync([position]) == 3
    tp = position.sorted_tps[0]
    PositionEngine(btc_rules).apply_tp_fill(position, tp.tp_id, executed_qty=0.0005, average_price=90000,
                                            quote_received=45, commissions=[Commission(asset="USDT", amount=0.045)],
                                            order_id=902)
    assert writer.sync([position]) == 1
    events = assert_contract(lines_of(writer))
    partial, tp_event = events[2], events[3]
    assert partial["event_type"] == "ENTRY_PARTIAL" and partial["fee"] is None and partial["fee_asset"] is None
    assert (tp_event["fee"], tp_event["fee_asset"]) == ("0.045", "USDT")


def test_feedback_rejections_come_from_reception_auto_state_and_failed_commands(tmp_path, btc_rules):
    inbox, commands, store, writer = feedback_setup(tmp_path)
    assert writer.record_rejection("CSI-RECEPTION", "BTCUSDT", "Signal expiré à la réception", occurred_at=NOW)
    assert not writer.record_rejection("", "BTCUSDT", "sans identifiant")
    auto = receive_csi(inbox, csi_text(SIGNAL_ID="CSI-AUTO", IDEMPOTENCY_KEY=KEY + ":A",
                                       VALIDATION_STATUS="RESEARCH"))
    inbox.set_auto_state("demo", auto["id"], "REJECTED", "VALIDATION_STATUS=RESEARCH : seul DEMO_ELIGIBLE")
    row, command, _ = queue_signal(inbox, commands, writer,
                                   csi_text(SIGNAL_ID="CSI-FAILED", IDEMPOTENCY_KEY=KEY + ":F"), btc_rules)
    commands.claim("demo")
    commands.finish("demo", command["id"], "FAILED", {"message": "Prix modifie de plus de 1 %"})
    receive_csi(inbox, csi_text(SIGNAL_ID="CSI-NEVER", IDEMPOTENCY_KEY=KEY + ":N"))

    assert writer.sync([]) == 3
    late = SignalFeedbackWriter("demo", inbox, commands, positions=store, directory=writer.directory,
                                registry_path=writer.registry.path, clock=lambda: EXPIRES + 61)
    assert late.sync([]) == 1 and late.sync([]) == 0

    events = assert_contract(lines_of(writer))
    by_signal = {}
    for event in events:
        by_signal.setdefault(event["signal_id"], []).append(event)
    assert [e["event_type"] for e in by_signal["CSI-RECEPTION"]] == ["REJECTED"]
    assert "RESEARCH" in by_signal["CSI-AUTO"][0]["reason"]
    assert [e["event_type"] for e in by_signal["CSI-FAILED"]] == ["RECEIVED", "REJECTED"]
    assert by_signal["CSI-FAILED"][1]["reason"] == "Prix modifie de plus de 1 %"
    assert "avant tout traitement" in by_signal["CSI-NEVER"][0]["reason"]


def test_feedback_expired_and_cancelled_entries(tmp_path, btc_rules):
    inbox, commands, store, writer = feedback_setup(tmp_path)
    row, command, position = queue_signal(inbox, commands, writer, csi_text(), btc_rules)
    succeed(commands, command)
    position.entries[0].status = EntryStatus.SUBMITTED
    position.entries[0].order_id = 555
    assert writer.sync([position]) == 2  # RECEIVED, ORDER_PLACED
    position.entries[0].status = EntryStatus.EXPIRED
    finish_position(position, CloseReason.CANCELED_BEFORE_FILL)
    assert writer.sync([position]) == 1 and writer.sync([position]) == 0
    events = assert_contract(lines_of(writer))
    assert [e["event_type"] for e in events] == ["RECEIVED", "ORDER_PLACED", "EXPIRED"]

    other = csi_text(SIGNAL_ID="CSI-CANCEL", IDEMPOTENCY_KEY=KEY + ":C")
    row, command, position = queue_signal(inbox, commands, writer, other, btc_rules)
    succeed(commands, command)
    position.entries[0].status = EntryStatus.CANCELED
    position.entries[0].last_error = "Annulation manuelle"
    finish_position(position, CloseReason.MANUAL_CLOSE)
    assert writer.sync([position]) == 2
    cancelled = [e for e in assert_contract(lines_of(writer)) if e["signal_id"] == "CSI-CANCEL"]
    assert [e["event_type"] for e in cancelled] == ["RECEIVED", "CANCELLED"]
    assert "Annulation manuelle" in cancelled[1]["reason"]


def test_feedback_is_silent_when_disabled_or_for_legacy_rows(tmp_path):
    inbox, commands, store, writer = feedback_setup(tmp_path, enabled=False)
    writer.register("CSI-1", "row", "BTCUSDT")
    assert not writer.record_rejection("CSI-1", "BTCUSDT", "refus")
    inbox.receive("demo", SIMPLE, source="api", external_id="drop:ml-1", source_timestamp=995)
    assert writer.sync([]) == 0 and not writer.path.exists() and writer.snapshot()["state"] == "DISABLED"


def test_drop_importer_reports_reception_outcomes_to_feedback(tmp_path):
    inbox, commands, store, writer = feedback_setup(tmp_path)
    importer, _ = drop_importer(tmp_path, feedback=writer, inbox=inbox)
    drop_file(tmp_path, "01-ok.txt", csi_text())
    drop_file(tmp_path, "02-expired.txt", csi_text(SIGNAL_ID="CSI-LATE", IDEMPOTENCY_KEY=KEY + ":L",
                                                    EXPIRES_AT="2026-09-29T12:04:00Z"))
    drop_file(tmp_path, "03-dup.txt", csi_text(SIGNAL_ID="CSI-DUP"))
    drop_file(tmp_path, "04-policy.txt", csi_text(SIGNAL_ID="CSI-POLICY", IDEMPOTENCY_KEY=KEY + ":P",
                                                   EXIT_POLICY_ID="TRAIL_PREVIOUS_TP_V1", MAX_HOLD_MINUTES="60"))
    drop_file(tmp_path, "05-v2.txt", "SIGNAL_VERSION=2\nSIGNAL_ID=CSI-OLD\nSYMBOL=BTCUSDT\n")
    drop_file(tmp_path, "06-replay.txt", csi_text())

    rows = importer.import_pending()

    assert len(rows) == 2 and rows[0]["id"] == rows[1]["id"]
    events = assert_contract(lines_of(writer))
    reasons = {e["signal_id"]: e["reason"] for e in events}
    assert set(reasons) == {"CSI-LATE", "CSI-DUP", "CSI-POLICY", "CSI-OLD"}
    assert all(e["event_type"] == "REJECTED" for e in events)
    assert "expiré" in reasons["CSI-LATE"] and "Doublon" in reasons["CSI-DUP"]
    assert "EXIT_POLICY" in reasons["CSI-POLICY"] and "version 2 retirée" in reasons["CSI-OLD"]
    assert {r["signal_id"] for r in writer.registry.pending()} == {SIGNAL_ID}


def test_feedback_validation_replicates_producer_rules():
    base = {"event_id": "BSM-X-RECEIVED-1", "signal_id": "CSI-X", "event_type": "RECEIVED",
            "occurred_at": "2026-09-30T10:15:03Z", "environment": "DEMO", "producer": "BinanceSpotManager",
            "symbol": "SOLUSDT", "quantity": None, "price": None, "quote_quantity": None, "fee": None,
            "fee_asset": None, "order_id": None, "target_index": None, "reason": None,
            "exit_policy_hash": "26367cfb1c063bb4"}
    other = base | {"event_type": "CLOSED", "exit_policy_hash": None}
    validate_event(base)
    validate_event(other)
    for mutation in [
        {"exit_policy_hash": None}, {"exit_policy_hash": "XYZ"}, {"environment": "LIVE"},
        {"occurred_at": "2026-09-30T10:15:03"}, {"symbol": "BTCEUR"}, {"event_type": "FILLED"},
    ]:
        with pytest.raises(FeedbackContractError):
            validate_event(base | mutation)
    for mutation in [
        {"exit_policy_hash": "26367cfb1c063bb4"}, {"event_type": "REJECTED"}, {"event_type": "CANCELLED"},
        {"event_type": "ENTRY_FILLED"}, {"event_type": "ENTRY_FILLED", "quantity": "0", "price": "1"},
        {"event_type": "TP_FILLED", "quantity": "1", "price": "1"}, {"fee": "0.1"}, {"fee_asset": "USDT"},
        {"event_type": "ORDER_PLACED", "quantity": "1", "price": "1"},
        {"event_type": "ORDER_PLACED", "order_id": "1", "price": "1"},
        {"event_type": "MARKET_EXIT_FILLED", "quantity": "1", "price": "1"},
        {"quantity": 1.0}, {"quantity": "NaN"}, {"target_index": 5}, {"order_id": 12}, {"reason": "x" * 501},
    ]:
        with pytest.raises(FeedbackContractError):
            validate_event(other | mutation)
    validate_event(other | {"event_type": "ORDER_PLACED", "order_id": "12", "quantity": "0.5", "price": "119.15"})
    validate_event(other | {"event_type": "MARKET_EXIT_FILLED", "quantity": "0.5", "price": "110", "reason": "stop"})
    if CSI_SCHEMA is not None:
        CSI_SCHEMA.parse_line(json.dumps(base))
        CSI_SCHEMA.parse_line(json.dumps(other | {"event_type": "ORDER_PLACED", "order_id": "12",
                                                  "quantity": "0.5", "price": "119.15"}))
    assert decimal_text(0.001) == "0.001" and decimal_text(84000.0) == "84000" and decimal_text(1e-08) == "0.00000001"
    assert build_event_id("CSI-1", "CLOSED", 1) == "BSM-CSI-1-CLOSED-1"
    assert len(build_event_id("X" * 160, "ENTRY_PARTIAL", 12)) <= 160
    assert dominant_fee({"BNB": 0.1, "USDT": 0.5}, {"BNB": 2, "USDT": 1}) == ("BNB", 0.1)
    assert dominant_fee({"BNB": 0.0}, {"BNB": 1}) is None


@pytest.mark.skipif(CSI_SCHEMA is None, reason="dépôt CryptoSignalIntelligence introuvable : définir CSI_PATH")
def test_producer_feedback_model_v2_is_used_for_cross_validation():
    assert CSI_SCHEMA.FEEDBACK_VERSION == 2
    assert {"ORDER_PLACED", "MARKET_EXIT_FILLED"} <= set(CSI_SCHEMA.FILL_EVENTS | {"ORDER_PLACED"})
