"""Routage des signaux : AUTO seulement sans motif de revue, sinon « À confirmer » (REVIEW)."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from binance_spot_manager import signal_routing
from binance_spot_manager.command_store import CommandStore
from binance_spot_manager.models import (
    Entry, EntryStatus, OrderType, Position, PositionStatus, SLStatus, TakeProfit,
)
from binance_spot_manager.risk_engine import RiskLimits
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.signal_parser import parse_signal
from binance_spot_manager.signal_routing import signal_risk_report
from test_signal_auto_execution import (
    SIMPLE, TRUSTED_CHAT, enabled_preferences, executor, positions_stub, telegram_id,
)
from test_signal_csi import NOW, csi_text, drop_preferences, receive_csi
from test_commands import processor, proposed  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def routed(tmp_path, text=SIMPLE, *, prefs=None, external_id=None, source="telegram", stamp=995,
           **kwargs):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", text, source=source, external_id=external_id or telegram_id(1),
                        source_timestamp=stamp)
    worker, commands = executor(tmp_path, inbox, prefs or enabled_preferences(), **kwargs)
    return inbox, worker, commands, row


def saved(inbox, row):
    return next(item for item in inbox.recent("demo") if item["id"] == row["id"])


def codes(item):
    return [reason["code"] for reason in json.loads(item["route"] or "{}").get("reasons", [])]


def refusing_client():
    def fail(*args, **kwargs):
        raise AssertionError("aucun appel Binance attendu")
    return SimpleNamespace(get_symbol_info=fail, get_price=fail, get_prices=fail, get_balances=fail)


# ==========================================================================
# 1. AUTO : confiance déclarée et risque modéré
# ==========================================================================


def test_auto_when_declared_and_low_risk(tmp_path):
    inbox, worker, commands, row = routed(tmp_path)

    assert worker.process_pending() == ["QUEUED"]
    payload = commands.get_by_request_key("demo", f"signal:{row['id']}")["payload"]
    assert payload["confirmation_mode"] == "AUTO" and payload["route"]["decision"] == "AUTO"
    # Calcul à la main : q = 0,00107 ; (84000 − 80000)·q = 4,28 ; frais d'achat 0,001·q·84000 = 0,08988 ;
    # sortie q·80000·(1 − 0,997·0,999) = 0,3421432 ; total 4,7120232 sur 1000 USDT = 0,4712 %.
    assert payload["route"]["metrics"]["risk_pct_with_costs"] == pytest.approx(0.47120232, abs=1e-6)
    assert payload["route"]["metrics"]["stop_distance_pct"] == pytest.approx(4000 / 84000 * 100)
    assert saved(inbox, row)["route"] == ""  # aucune revue enregistrée


# ==========================================================================
# 2. Confiance déclarée
# ==========================================================================


@pytest.mark.parametrize("case, code, no_call", [
    ("undeclared_chat", "C_SOURCE_UNDECLARED", False),
    ("malformed_id", "C_SOURCE_UNDECLARED", False),
    ("asset_not_listed", "C_UNIVERSE", False),
    ("empty_asset_list", "C_UNIVERSE", False),
    ("json_v1", "C_SOURCE_UNDECLARED", False),
    ("stale", "C_STALE", True),
    ("no_timestamp", "C_STALE", True),
    ("candle_stop", "C_SL_CANDLE", False),
    ("demo_manual", "C_DEMO_MANUAL", True),
])
def test_confidence_reasons(tmp_path, case, code, no_call):
    prefs = enabled_preferences()
    text, external_id, source, stamp, run_mode = SIMPLE, telegram_id(1), "telegram", 995, "DEMO_AUTO"
    if case == "undeclared_chat":
        prefs |= {"signal_telegram_chats": f"{TRUSTED_CHAT}, -100999"}
        external_id = telegram_id(1, chat=-100999)
    elif case == "malformed_id":
        external_id = "bot:1"
    elif case == "asset_not_listed":
        prefs |= {"signal_auto_base_assets": ["ETH"]}
    elif case == "empty_asset_list":
        prefs |= {"signal_auto_base_assets": []}
    elif case == "json_v1":
        prefs |= {"signal_drop_enabled": True, "signal_drop_auto_enabled": True,
                  "signal_drop_auto_enabled_since": 900}
        source, external_id = "api", "drop:ml-1"
    elif case == "stale":
        stamp = 600
    elif case == "no_timestamp":
        stamp = 0
    elif case == "candle_stop":
        text = SIMPLE.replace("SL: 80000", "SL: 80000 (1h)")
    elif case == "demo_manual":
        run_mode = "DEMO_MANUAL"
    inbox, worker, commands, row = routed(
        tmp_path, text, prefs=prefs, external_id=external_id, source=source, stamp=stamp,
        run_mode=run_mode, client=refusing_client() if no_call else None)

    worker.process_pending()

    item = saved(inbox, row)
    assert item["auto_state"] == "REVIEW" and item["payload"] is None
    assert code in codes(item)
    assert commands.list_recent("demo") == []
    reviews = [e for e in worker.events.tail(20) if e["event"] == "SIGNAL_REVIEW_REQUIRED"]
    assert len(reviews) == 1 and reviews[0]["level"] == "WARNING"


# ==========================================================================
# 3. Risque
# ==========================================================================


def eth_position(*, entry_status=EntryStatus.SUBMITTED, qty=0.0, status=PositionStatus.ACTIVE):
    position = Position(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT", status=status)
    if qty:
        position.entries.append(Entry(order_type=OrderType.LIMIT, status=entry_status, binance_qty=qty,
                                      resolved_price=3000.0))
        position.stop_loss.resolved_price = 2000.0
        position.stop_loss.status = SLStatus.PLANNED
    return position


@pytest.mark.parametrize("case, code, absent", [
    ("risk_051", "R1_RISK_AT_STOP", None),
    ("hard_limit", "R2_HARD_LIMIT", None),
    ("clamped_threshold", "R1_RISK_AT_STOP", "R2_HARD_LIMIT"),
    ("last_slot", "R3_RISK_WARNING", None),
    ("resting_entries", "R4_TOTAL_RISK", None),
    ("same_asset_queued", "R5_SAME_ASSET", None),
    ("stop_too_close", "R6_STOP_DISTANCE", None),
    ("stop_too_far", "R6_STOP_DISTANCE", None),
    ("entry_passed", "R7_ENTRY_MARKETABLE", None),
])
def test_risk_reasons(tmp_path, case, code, absent):
    prefs, text, positions = enabled_preferences(), SIMPLE, positions_stub()
    if case == "risk_051":
        prefs |= {"signal_fixed_budget": 98}   # 0,51 % frais compris
    elif case == "hard_limit":
        prefs |= {"signal_fixed_budget": 250}  # 1,19 % hors frais : le worker refusera
    elif case == "clamped_threshold":
        # Réglage à 5 % : ramené à la limite dure de 1 % ; 1,08 % frais compris (0,98 % hors frais).
        prefs |= {"signal_fixed_budget": 206, "signal_review_max_risk_percent": 5}
    elif case == "last_slot":
        positions = positions_stub([eth_position() for _ in range(4)])
    elif case == "resting_entries":
        positions = positions_stub([eth_position(qty=0.04)])  # 40 USDT au repos : 4,47 % projetés
    elif case == "same_asset_queued":
        CommandStore(tmp_path / "commands.db").enqueue("demo", "SUBMIT_POSITION", {
            "position": {"symbol": "BTCUSDT", "quote_asset": "USDT", "entries": [], "stop_loss": {}},
            "entry_ids": []}, request_key="signal:other")
    elif case == "stop_too_close":
        text = SIMPLE.replace("SL: 80000", "SL: 83500")
    elif case == "stop_too_far":
        text = SIMPLE.replace("SL: 80000", "SL: 74760")
    elif case == "entry_passed":
        text = SIMPLE.replace("ENTRY 1: 84000", "ENTRY 1: 86000")
    inbox, worker, commands, row = routed(tmp_path, text, prefs=prefs, positions=positions)

    assert worker.process_pending() == ["REVIEW"]
    item = saved(inbox, row)
    assert code in codes(item)
    if absent:
        assert absent not in codes(item)
    if case == "hard_limit":
        assert "le worker refusera" in item["auto_detail"] and "1.19 % > 1.00 %" in item["auto_detail"]
    if case == "clamped_threshold":
        route = json.loads(item["route"])
        assert next(r for r in route["reasons"] if r["code"] == code)["threshold"] == 1.0


@pytest.mark.parametrize("regime, expected", [("HIGH", "REVIEW"), ("UNKNOWN", "QUEUED")])
def test_csi_high_volatility_is_reviewed(tmp_path, regime, expected):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = receive_csi(inbox, csi_text(VOLATILITY_REGIME=regime))
    worker, _ = executor(tmp_path, inbox, drop_preferences(), now=NOW)

    assert worker.process_pending() == [expected]
    if expected == "REVIEW":
        assert "R8_CSI_HIGH_VOLATILITY" in codes(saved(inbox, row))


def test_same_marketable_geometry_is_rejected_for_csi_by_its_contract(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = receive_csi(inbox, csi_text(ENTRY_1="86000.00"))  # 174 bps > MAX_ENTRY_DEVIATION_BPS 100
    worker, _ = executor(tmp_path, inbox, drop_preferences(), now=NOW)

    assert worker.process_pending() == ["REJECTED"]
    assert "MAX_ENTRY_DEVIATION_BPS" in saved(inbox, row)["auto_detail"]


# ==========================================================================
# 4. Coupe-circuits
# ==========================================================================


def closed_signal_position(row_id, pnl, closed_at):
    position = Position(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT", status=PositionStatus.CLOSED,
                        tags=["signal", row_id])
    position.pnl.realized = pnl
    position.closed_at = datetime.fromtimestamp(closed_at, tz=timezone.utc)
    return position


def auto_command(store, row_id, *, mode="AUTO", finish=True):
    command = store.enqueue("demo", "SUBMIT_POSITION", {
        "confirmation_mode": mode, "position": {"symbol": "ETHUSDT", "quote_asset": "USDT", "entries": []},
        "entry_ids": []}, request_key=f"signal:{row_id}")
    if finish:
        store.claim("demo")
        store.finish("demo", command["id"], "SUCCEEDED", {"message": "ok"})


def test_breakers(tmp_path):
    # 4 ordres automatiques sur 24 h.
    store = CommandStore(tmp_path / "count" / "commands.db")
    for index in range(4):
        auto_command(store, f"a{index}")
    inbox, worker, _, row = routed(tmp_path / "count")
    assert worker.process_pending() == ["REVIEW"] and "R9_AUTO_COUNT" in codes(saved(inbox, row))

    # −2,1 % réalisé depuis 00:00 UTC sur des positions de signaux.
    positions = positions_stub([closed_signal_position("x", -21.0, 940)])
    inbox, worker, _, row = routed(tmp_path / "day", positions=positions)
    assert worker.process_pending() == ["REVIEW"] and "R9_DAILY_LOSS" in codes(saved(inbox, row))

    # 3 pertes automatiques consécutives, jusqu'au réarmement.
    store = CommandStore(tmp_path / "streak" / "commands.db")
    for name in ("r1", "r2", "r3"):
        auto_command(store, name)
    losses = [closed_signal_position(name, -1.0, 900 + i) for i, name in enumerate(("r1", "r2", "r3"))]
    inbox, worker, _, row = routed(tmp_path / "streak", positions=positions_stub(losses))
    assert worker.process_pending() == ["REVIEW"] and "R9_LOSS_STREAK" in codes(saved(inbox, row))
    rearmed = enabled_preferences(signal_auto_breaker_reset_at=950)
    inbox, worker, _, row = routed(tmp_path / "rearmed", prefs=rearmed, positions=positions_stub(losses))
    for name in ("r1", "r2", "r3"):
        auto_command(CommandStore(tmp_path / "rearmed" / "commands.db"), name)
    assert worker.process_pending() == ["QUEUED"]

    # Des pertes d'origine manuelle ne comptent pas.
    store = CommandStore(tmp_path / "manual" / "commands.db")
    for name in ("r1", "r2", "r3"):
        auto_command(store, name, mode="MANUAL")
    inbox, worker, _, row = routed(tmp_path / "manual", positions=positions_stub(losses))
    assert worker.process_pending() == ["QUEUED"]


# ==========================================================================
# 5. Parité avec le worker (command_processor.py, contrôle du risque)
# ==========================================================================


@pytest.mark.parametrize("qty, accepted", [(0.05, False), (0.001, True)])
def test_hard_limit_parity(processor, qty, accepted):  # noqa: F811
    worker, calls = processor
    position = proposed()
    position.entries[0].binance_qty = qty
    position.entries[0].quote_amount = qty * 84000
    entry_ids = [e.entry_id for e in position.entries]
    report, *_ = signal_risk_report(
        position, entry_ids, price=84000, balances=worker.execution.client.get_balances(),
        prices=worker.execution.client.get_prices(), positions=worker.positions.list_all(), limits=RiskLimits())
    worker.store.enqueue(worker.scope, "SUBMIT_POSITION", {
        "position": position.model_dump(mode="json"), "entry_ids": entry_ids, "reference_price": 84000,
    }, request_key=f"parity-{qty}")
    state = worker.run_one()
    message = worker.store.list_recent(worker.scope)[0]["result"]["message"]
    assert report.accepted is accepted
    if accepted:
        assert state == "SUCCEEDED" and len(calls) == 1
    else:
        assert len(report.refusals) >= 2
        assert state == "FAILED" and message == " ; ".join(report.refusals) and calls == []


# ==========================================================================
# 6. Refus de contrat inchangés
# ==========================================================================


def test_reject_cases_unchanged(tmp_path):
    inbox, worker, commands, row = routed(tmp_path, "bonjour, rien à acheter")
    assert worker.process_pending() == ["REJECTED"]
    assert "parseur" in saved(inbox, row)["auto_detail"]

    inbox, worker, commands, row = routed(tmp_path / "frozen")
    inbox.freeze("demo", row["id"], {"signal_confirmation_expires_at": 1, "position": {"position_id": "p"}})
    assert worker.process_pending() == ["REJECTED"]
    assert "expirée" in saved(inbox, row)["auto_detail"]
    assert commands.list_recent("demo") == []


# ==========================================================================
# 7. Données non mesurables : revue, jamais refus définitif
# ==========================================================================


def test_data_errors_route_to_review(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("réseau coupé")

    client = SimpleNamespace(get_symbol_info=lambda s: executor_rules().raw, get_price=lambda s: 84500,
                             get_prices=lambda: {"BTCUSDT": 84500}, get_balances=broken)
    inbox, worker, _, row = routed(tmp_path / "net", client=client)
    assert worker.process_pending() == ["REVIEW"] and "D_UNAVAILABLE" in codes(saved(inbox, row))

    client = SimpleNamespace(get_symbol_info=lambda s: executor_rules().raw, get_price=lambda s: 84500,
                             get_prices=lambda: {"BTCUSDT": 84500},
                             get_balances=lambda: {"USDT": {"free": 1000}, "XYZ": {"free": 1}})
    inbox, worker, _, row = routed(tmp_path / "unpriced", client=client)
    assert worker.process_pending() == ["REVIEW"] and "D_VALUATION" in codes(saved(inbox, row))

    client = SimpleNamespace(get_symbol_info=lambda s: executor_rules().raw, get_price=lambda s: 79000,
                             get_prices=lambda: {"BTCUSDT": 79000},
                             get_balances=lambda: {"USDT": {"free": 1000}})
    inbox, worker, _, row = routed(tmp_path / "plan", client=client)  # SL déjà atteint
    assert worker.process_pending() == ["REVIEW"] and "D_PLAN" in codes(saved(inbox, row))

    inbox, worker, _, row = routed(tmp_path / "store", positions=positions_stub(read_errors=["x.json"]))
    assert worker.process_pending() == ["REVIEW"] and "D_RISK" in codes(saved(inbox, row))

    def explode(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(signal_routing, "decide", explode)
    inbox, worker, commands, row = routed(tmp_path / "bug")
    assert worker.process_pending() == ["REVIEW"] and "D_ROUTER_ERROR" in codes(saved(inbox, row))
    assert commands.list_recent("demo") == []


def executor_rules():
    from binance_spot_manager.symbol_rules import parse_symbol_rules
    return parse_symbol_rules({
        "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "100"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "NOTIONAL", "minNotional": "5"}]})


# ==========================================================================
# 8. REVIEW est définitif pour le routage
# ==========================================================================


def test_review_is_sticky(tmp_path):
    prefs = enabled_preferences(signal_auto_trusted_chats=[])
    inbox, worker, commands, row = routed(tmp_path, prefs=prefs)
    assert worker.process_pending() == ["REVIEW"]

    assert inbox.auto_candidates("demo", enabled_since=0, oldest_source_timestamp=0, now=1000,
                                 sources=("telegram",)) == []
    assert inbox.claim_auto("demo", row["id"]) is False
    inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(2), source_timestamp=999)
    assert saved(inbox, row)["auto_state"] == "REVIEW"
    # Une déclaration ajoutée ensuite ne re-route jamais une ligne déjà décidée.
    worker.preferences_loader = lambda: enabled_preferences()
    assert worker.process_pending() == []
    assert saved(inbox, row)["auto_state"] == "REVIEW" and commands.list_recent("demo") == []


# ==========================================================================
# 9. Une seule nouvelle commande AUTO par cycle
# ==========================================================================


def test_one_enqueue_per_tick(tmp_path):
    prefs = enabled_preferences(signal_review_same_asset=False)
    inbox, worker, commands, first = routed(tmp_path, prefs=prefs)
    second = inbox.receive("demo", SIMPLE.replace("T1: 90000", "T1: 91000"), source="telegram",
                           external_id=telegram_id(2), source_timestamp=996)

    assert worker.process_pending() == ["QUEUED"]
    assert saved(inbox, first)["auto_state"] == "QUEUED" and saved(inbox, second)["auto_state"] == ""
    assert worker.process_pending() == ["QUEUED"]
    assert len(commands.list_recent("demo")) == 2


# ==========================================================================
# 10. Notification « À confirmer »
# ==========================================================================


def test_notify(tmp_path):
    sent = []
    prefs = enabled_preferences(signal_auto_trusted_chats=[], signal_review_notify=True)
    inbox, worker, _, row = routed(tmp_path, prefs=prefs, notify=sent.append)
    assert worker.process_pending() == ["REVIEW"]
    assert len(sent) == 1
    body = sent[0].body
    assert not any(line.strip().upper().startswith(("ENTRY", "TP", "SL")) for line in body.splitlines())
    assert parse_signal(body).errors
    assert sent[0].position_id == row["id"]

    sent.clear()
    inbox, worker, _, _ = routed(tmp_path / "auto", prefs=enabled_preferences(signal_review_notify=True),
                                 notify=sent.append)
    assert worker.process_pending() == ["QUEUED"] and sent == []
    inbox, worker, _, _ = routed(tmp_path / "reject", "texte sans signal", prefs=prefs, notify=sent.append)
    assert worker.process_pending() == ["REJECTED"] and sent == []

    def broken(notification):
        raise RuntimeError("canal indisponible")

    inbox, worker, _, row = routed(tmp_path / "broken", prefs=prefs, notify=broken)
    assert worker.process_pending() == ["REVIEW"] and saved(inbox, row)["auto_state"] == "REVIEW"

    # Désactivée par défaut.
    sent.clear()
    inbox, worker, _, _ = routed(tmp_path / "off", prefs=enabled_preferences(signal_auto_trusted_chats=[]),
                                 notify=sent.append)
    assert worker.process_pending() == ["REVIEW"] and sent == []


# ==========================================================================
# 18. Vocabulaire : jamais présenté comme une probabilité
# ==========================================================================


def test_no_probability_wording():
    for path in (ROOT / "binance_spot_manager" / "signal_routing.py", ROOT / "pages" / "8_Signaux.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "probabilit" not in text and "chance" not in text, path.name


def test_policy_defaults_and_bounds():
    policy = signal_routing.RoutingPolicy.from_mapping({}, RiskLimits())
    assert policy.base_assets == signal_routing.CSI_UNIVERSE_BASE_ASSETS and len(policy.base_assets) == 16
    assert policy.trusted_chats == frozenset() and policy.notify_review is False and policy.honor_demo_manual
    assert (policy.max_risk_percent, policy.total_risk_threshold) == (0.5, 4.0)
    assert (policy.min_stop_percent, policy.max_stop_percent, policy.marketable_gap_percent) == (1.0, 10.0, 1.0)
    assert (policy.max_auto_per_24h, policy.daily_loss_percent, policy.loss_streak) == (4, 2.0, 3)
    # Liste enregistrée vide : aucun actif ; groupe de confiance hors réception : ignoré.
    empty = signal_routing.RoutingPolicy.from_mapping(
        {"signal_auto_base_assets": [], "signal_auto_trusted_chats": [5], "signal_telegram_chats": "7"})
    assert empty.base_assets == () and empty.trusted_chats == frozenset()
    assert signal_routing.RoutingPolicy.from_mapping(
        {"signal_review_max_risk_percent": 5}, RiskLimits(max_risk_per_position_percent=1)).max_risk_percent == 1
    assert policy.widened_by(empty) == [] and "actifs ajoutés" in empty.widened_by(policy)
