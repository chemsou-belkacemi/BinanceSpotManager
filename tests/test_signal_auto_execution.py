from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from binance_spot_manager.command_store import CommandStore
from binance_spot_manager.csi_client import CsiOpinion, CsiUnavailable
from binance_spot_manager.event_store import EventStore
from binance_spot_manager.signal_auto_execution import AutomaticSignalExecutor
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.symbol_rules import SymbolRulesCache, parse_symbol_rules


SIMPLE = "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000"


class FakeCsi:
    """Client CSI factice : un verdict fixe, ou une panne."""

    def __init__(self, verdict="FAVORABLE", *, fail=False):
        self.verdict, self.fail, self.calls = verdict, fail, []

    def evaluate(self, text, *, source, record=True, user_validated=False):
        assert user_validated is False                     # le worker ne valide jamais une paire
        self.calls.append((text, source, record))
        if self.fail:
            raise CsiUnavailable("CSI injoignable sur http://csi-api:8503 (ConnectionError)")
        return CsiOpinion(verdict=self.verdict, summary=f"résumé {self.verdict}", source=source,
                          evaluated_at="2026-09-30T10:00:00+00:00")


def executor(tmp_path, inbox, preferences, *, now=1000, csi_client=None):
    rules = parse_symbol_rules({
        "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
        "status": "TRADING", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "100"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ],
    })
    client = SimpleNamespace(
        get_symbol_info=lambda symbol: rules.raw,
        get_price=lambda symbol: 84500,
        get_prices=lambda: {"BTCUSDT": 84500},
        get_balances=lambda: {"USDT": {"free": 1000, "locked": 0}},
    )
    commands = CommandStore(tmp_path / "commands.db")
    worker = AutomaticSignalExecutor(
        "demo", inbox, commands, client, SymbolRulesCache(client),
        lambda: SimpleNamespace(min_reserve_percent=20),
        lambda: preferences, EventStore(tmp_path / "events.jsonl"),
        clock=lambda: now, csi_client=csi_client,
    )
    return worker, commands


def enabled_preferences(**overrides):
    return {
        "signal_telegram_enabled": True,
        "signal_telegram_auto_enabled": True,
        "signal_auto_execute_enabled": True,
        "signal_auto_execute_enabled_since": 900,
        "signal_auto_max_age_minutes": 5,
        "signal_auto_touch_stop": False,
        "signal_sizing_mode": "FIXED",
        "signal_fixed_budget": 200,
        # Les tests historiques n'ont pas de client CSI : contrôle désactivé, sauf mention contraire.
        "signal_csi_gate_enabled": False,
    } | overrides


def test_fresh_authorized_signal_is_frozen_and_queued_once(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive(
        "demo", SIMPLE, source="telegram", external_id="bot:1",
        source_timestamp=995,
    )
    worker, commands = executor(tmp_path, inbox, enabled_preferences())

    assert worker.process_pending() == ["QUEUED"]
    saved = inbox.recent("demo")[0]
    assert saved["id"] == row["id"]
    assert saved["payload"] is not None
    assert saved["auto_state"] == "QUEUED"
    command = commands.get_by_request_key("demo", f"signal:{row['id']}")
    assert command["state"] == "PENDING"
    assert command["action"] == "SUBMIT_POSITION"
    assert worker.process_pending() == []


def test_old_or_pre_authorization_messages_are_never_queued(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", SIMPLE, source="telegram", external_id="old", source_timestamp=600)
    worker, commands = executor(tmp_path, inbox, enabled_preferences())

    assert worker.process_pending() == []
    assert commands.list_recent("demo") == []
    assert inbox.recent("demo")[0]["auto_state"] == ""


def test_resending_old_unconfirmed_text_refreshes_telegram_age_and_queues_once(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    old = inbox.receive("demo", SIMPLE)
    resent = inbox.receive(
        "demo", SIMPLE, source="telegram", external_id="new-message",
        source_timestamp=995,
    )
    assert resent["id"] == old["id"]
    assert resent["source"] == "telegram"
    assert resent["source_timestamp"] == 995

    worker, commands = executor(tmp_path, inbox, enabled_preferences())
    assert worker.process_pending() == ["QUEUED"]
    assert commands.get_by_request_key("demo", f"signal:{old['id']}") is not None

    # A further identical Telegram message cannot refresh or create another order.
    frozen_timestamp = inbox.recent("demo")[0]["source_timestamp"]
    inbox.receive(
        "demo", SIMPLE, source="telegram", external_id="third-message",
        source_timestamp=999,
    )
    assert inbox.recent("demo")[0]["source_timestamp"] == frozen_timestamp
    assert len(commands.list_recent("demo")) == 1


def test_conditional_stop_requires_saved_touch_authorization(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive(
        "demo", SIMPLE.replace("SL: 80000", "SL: 80000 (1h)"),
        source="telegram", external_id="conditional", source_timestamp=995,
    )
    worker, commands = executor(tmp_path, inbox, enabled_preferences())

    assert worker.process_pending() == ["REJECTED"]
    saved = inbox.recent("demo")[0]
    assert saved["id"] == row["id"]
    assert saved["auto_state"] == "REJECTED"
    assert "SL conditionnel" in saved["auto_detail"]
    assert commands.list_recent("demo") == []


def test_saved_policy_limits_entries_and_targets_with_early_sales(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    text = (
        "PAIR: BTC/USDT\nENTRY 1: 84000\nENTRY 2: 83000\n"
        "T1: 90000\nT2: 95000\nT3: 100000\nSL: 80000 (4h)"
    )
    row = inbox.receive(
        "demo", text, source="telegram", external_id="policy", source_timestamp=995,
    )
    preferences = enabled_preferences(
        signal_auto_touch_stop=True,
        signal_auto_entry_count=1,
        signal_auto_tp_count=2,
        signal_auto_tp_distribution="EARLY",
    )
    worker, commands = executor(tmp_path, inbox, preferences)

    assert worker.process_pending() == ["QUEUED"]
    command = commands.get_by_request_key("demo", f"signal:{row['id']}")
    position = command["payload"]["position"]
    assert len(position["entries"]) == 1
    assert position["entries"][0]["resolved_price"] == 84000
    assert [tp["target_price"] for tp in position["take_profits"]] == [90000, 95000]
    assert [tp["sell_percent"] for tp in position["take_profits"]] == pytest.approx([70, 100])


def test_saved_custom_policy_applies_to_entries_and_targets(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    text = (
        "PAIR: BTC/USDT\nENTRY 1: 84000\nENTRY 2: 83000\n"
        "T1: 90000\nT2: 95000\nSL: 80000"
    )
    row = inbox.receive(
        "demo", text, source="telegram", external_id="custom", source_timestamp=995,
    )
    preferences = enabled_preferences(
        signal_auto_entry_count=2,
        signal_auto_entry_distribution="CUSTOM",
        signal_auto_entry_custom_percentages="30;70",
        signal_auto_tp_count=2,
        signal_auto_tp_distribution="CUSTOM",
        signal_auto_tp_custom_percentages="80;20",
    )
    worker, commands = executor(tmp_path, inbox, preferences)

    assert worker.process_pending() == ["QUEUED"]
    position = commands.get_by_request_key(
        "demo", f"signal:{row['id']}"
    )["payload"]["position"]
    assert [entry["capital_percent"] for entry in position["entries"]] == [30, 70]
    assert [tp["sell_percent"] for tp in position["take_profits"]] == pytest.approx([80, 100])


def test_telegram_timestamp_is_authoritative_over_text_timezone(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    now = datetime(2026, 9, 29, 1, 15, tzinfo=timezone.utc).timestamp()
    dated = SIMPLE + "\nDate: Monday - 2026-09-28\nIndicatorTime :- 23:56 GMT+2"
    row = inbox.receive(
        "demo", dated, source="telegram", external_id="fresh-telegram",
        source_timestamp=now - 5,
    )
    assert row["parsed"]["published_at"] == "2026-09-28T21:56:00+00:00"
    worker, commands = executor(tmp_path, inbox, enabled_preferences(), now=now)

    assert worker.process_pending() == ["QUEUED"]
    assert commands.get_by_request_key("demo", f"signal:{row['id']}") is not None


def test_fresh_resend_can_retry_rejected_signal_without_existing_payload(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive(
        "demo", SIMPLE, source="telegram", external_id="first", source_timestamp=990,
    )
    inbox.set_auto_state("demo", row["id"], "REJECTED", "Ancienne règle de date")

    resent = inbox.receive(
        "demo", SIMPLE, source="telegram", external_id="second", source_timestamp=998,
    )
    assert resent["id"] == row["id"]
    assert resent["source_timestamp"] == 998
    assert resent["auto_state"] == ""
    assert resent["auto_detail"] == ""


def test_csi_gate_holds_unfavourable_signals_for_manual_confirmation(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive(
        "demo", SIMPLE, source="telegram", external_id="bothash:-100123:7", source_timestamp=995,
    )
    csi = FakeCsi("DEFAVORABLE")
    preferences = enabled_preferences(
        signal_csi_gate_enabled=True, signal_csi_source_names="-100123=Suhaib",
    )
    worker, commands = executor(tmp_path, inbox, preferences, csi_client=csi)

    assert worker.process_pending() == ["REJECTED"]
    saved = inbox.recent("demo")[0]
    assert saved["id"] == row["id"] and saved["auto_state"] == "REJECTED"
    assert "Avis CSI Défavorable" in saved["auto_detail"] and "retenue" in saved["auto_detail"]
    assert saved["csi_verdict"] == "DEFAVORABLE" and saved["csi_detail"] == "résumé DEFAVORABLE"
    assert csi.calls == [(SIMPLE, "Suhaib", True)]
    assert commands.list_recent("demo") == []          # aucun ordre : confirmation manuelle possible


def test_csi_gate_lets_favourable_signals_through_and_keeps_the_opinion(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", SIMPLE, source="telegram", external_id="bothash:-100123:8", source_timestamp=995)
    csi = FakeCsi("FAVORABLE")
    worker, commands = executor(tmp_path, inbox, enabled_preferences(signal_csi_gate_enabled=True), csi_client=csi)

    assert worker.process_pending() == ["QUEUED"]
    saved = inbox.recent("demo")[0]
    assert saved["csi_verdict"] == "FAVORABLE" and saved["auto_state"] == "QUEUED"
    assert csi.calls == [(SIMPLE, "telegram -100123", True)]
    assert commands.get_by_request_key("demo", f"signal:{row['id']}") is not None


def test_csi_gate_holds_when_csi_is_unreachable_unless_allowed_by_setting(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", SIMPLE, source="telegram", external_id="bothash:-1:1", source_timestamp=995)
    worker, commands = executor(
        tmp_path, inbox, enabled_preferences(signal_csi_gate_enabled=True), csi_client=FakeCsi(fail=True),
    )
    assert worker.process_pending() == ["REJECTED"]
    saved = inbox.recent("demo")[0]
    assert "Avis CSI indisponible" in saved["auto_detail"] and saved["csi_verdict"] == ""
    assert commands.list_recent("demo") == []

    lenient = SignalInbox(tmp_path / "lenient.db")
    lenient.receive("demo", SIMPLE, source="telegram", external_id="bothash:-1:2", source_timestamp=995)
    worker, commands = executor(
        tmp_path / "lenient", lenient,
        enabled_preferences(signal_csi_gate_enabled=True, signal_csi_when_unavailable="ALLOW"),
        csi_client=FakeCsi(fail=True),
    )
    assert worker.process_pending() == ["QUEUED"]
    assert len(commands.list_recent("demo")) == 1


def test_csi_gate_disabled_never_calls_csi(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", SIMPLE, source="telegram", external_id="bothash:-1:3", source_timestamp=995)
    csi = FakeCsi("REFUSE")
    worker, commands = executor(tmp_path, inbox, enabled_preferences(), csi_client=csi)

    assert worker.process_pending() == ["QUEUED"]
    assert csi.calls == [] and len(commands.list_recent("demo")) == 1
