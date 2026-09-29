from types import SimpleNamespace
from datetime import datetime, timezone

from binance_spot_manager.command_store import CommandStore
from binance_spot_manager.event_store import EventStore
from binance_spot_manager.signal_auto_execution import AutomaticSignalExecutor
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.symbol_rules import SymbolRulesCache, parse_symbol_rules


SIMPLE = "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000"


def executor(tmp_path, inbox, preferences, *, now=1000):
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
        clock=lambda: now,
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
