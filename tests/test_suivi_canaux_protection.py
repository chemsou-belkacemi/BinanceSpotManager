"""Suivi par canal, rapport quotidien, protection en cas de chute du marche, BSM face au marche et
option « annuler l'achat si le TP1 est touche avant ».

- le canal d'origine d'un message Telegram est garde jusqu'a la position ; History, le rapport et le
  routage comptent par canal, sans les positions annulees avant tout achat ;
- un canal perdant (reglage, desactive par defaut) envoie ses signaux « A confirmer » ;
- une chute de BTC suspend les nouvelles entrees automatiques, sans jamais vendre ; en option, les
  stops des positions en gain montent au seuil de rentabilite, jamais a la baisse ;
- un TP atteint avant l'achat garde l'ordre d'achat d'un signal texte (marque pour la mesure), sauf
  reglage contraire ; un signal CSI annule toujours (contrat).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from binance_spot_manager.daily_report import DailyReport, build
from binance_spot_manager.market_comparison import btc_opens, compare, price_at
from binance_spot_manager.market_guard import CHECK_EVERY_SECONDS, MarketGuard, active_status, drop_over_window
from binance_spot_manager.models import (
    CloseReason,
    Entry,
    EntryStatus,
    OrderType,
    Position,
    PositionStatus,
    SignalSource,
    SLStatus,
    SourceGroup,
    TakeProfit,
    TPStatus,
)
from binance_spot_manager.notification_engine import NotificationEngine
from binance_spot_manager.performance import (
    LATE_FILL_TAG,
    UNKNOWN_TELEGRAM,
    channel_of,
    late_fill_split,
    losing_channel,
    stats_by,
)
from binance_spot_manager.position_engine import finish_position, recompute_position
from binance_spot_manager.signal_auto_execution import channel_name
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.signal_parser import parse_signal
from binance_spot_manager.signal_plan import prepare_signal
from binance_spot_manager.telegram_signals import origin_label
from test_automation import (  # noqa: F401 - fixtures partagees
    build_automation,
    build_execution,
    engine,
    events,
    journaled,
    make_position,
    rules,
    settings,
)
from test_journal_telegram_corrections import losing_position
from test_signal_auto_execution import SIMPLE, enabled_preferences, executor, positions_stub, telegram_id
from test_signal_csi import btc_rules, csi_text, prepare_csi  # noqa: F401 - fixture partagee

UTC = timezone.utc
NOW = datetime(2026, 10, 5, 20, 30, tzinfo=UTC)


def labelled(position, channel, source=SignalSource.TELEGRAM):
    position.source_groups = [SourceGroup(source=source, label=channel)]
    return position


def winning_position(rules):
    """Achat 0,006 BTC a 84 000, TP1 et TP2 executes : un gain."""
    position = make_position(rules)
    tp1, tp2 = position.sorted_tps
    tp1.status, tp1.executed_qty, tp1.average_fill_price = TPStatus.EXECUTED, 0.0036, 86520.0
    tp1.quote_received = 0.0036 * 86520.0
    tp2.status, tp2.executed_qty, tp2.average_fill_price = TPStatus.EXECUTED, 0.0024, 89040.0
    tp2.quote_received = 0.0024 * 89040.0
    position.stop_loss.status = SLStatus.CANCELED
    finish_position(position, CloseReason.ALL_TP_HIT)
    recompute_position(position)
    return position


def never_bought(rules):
    """Position terminee sans aucun achat (ordre d'achat annule)."""
    position = make_position(rules, entry_status=EntryStatus.CANCELED)
    entry = position.entries[0]
    entry.executed_qty = entry.net_qty = entry.binance_qty = entry.quote_spent = 0.0
    entry.average_fill_price = 0.0
    entry.commissions = []
    position.stop_loss.status = SLStatus.NONE
    finish_position(position, CloseReason.CANCELED_BEFORE_FILL)
    recompute_position(position)
    return position


# ==========================================================================
# Canal d'origine : Telegram → boîte des signaux → position
# ==========================================================================


@pytest.mark.parametrize("message, expected", [
    ({"forward_origin": {"type": "channel", "chat": {"title": "Crypto  Signals\nVIP"}},
      "chat": {"title": "Relais"}}, "Crypto Signals VIP"),
    ({"forward_origin": {"type": "chat", "sender_chat": {"title": "Groupe source"}}}, "Groupe source"),
    ({"forward_origin": {"type": "user", "sender_user": {"first_name": "Ali", "last_name": "B"}}}, "Ali B"),
    ({"forward_origin": {"type": "hidden_user", "sender_user_name": "Anonyme"}}, "Anonyme"),
    ({"forward_from_chat": {"title": "Ancien format"}, "chat": {"title": "Relais"}}, "Ancien format"),
    ({"chat": {"title": "Groupe direct"}}, "Groupe direct"),
    ({"chat": {"id": 5, "type": "private"}}, ""),
])
def test_the_origin_names_the_forwarded_channel_before_the_relay(message, expected):
    assert origin_label(message) == expected


def test_the_origin_is_bounded():
    assert len(origin_label({"chat": {"title": "x" * 300}})) == 80


def test_the_inbox_keeps_the_channel_and_a_fresh_resend_carries_its_own(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    first = inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1))
    assert first["origin"] == ""
    learned = inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(2), origin="Canal A")
    assert learned["id"] == first["id"] and learned["origin"] == "Canal A"
    # Même texte repris plus tard par un autre canal, jamais envoyé : le message qui pourra partir
    # porte son canal.
    resent = inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(3),
                           source_timestamp=995, origin="Canal B")
    assert resent["id"] == first["id"] and resent["origin"] == "Canal B"


def test_the_channel_name_falls_back_to_the_declared_group_name():
    row = {"source": "telegram", "external_id": telegram_id(1), "origin": ""}
    assert channel_name(row | {"origin": "Canal A"}, {}) == "Canal A"
    assert channel_name(row, {"signal_csi_source_names": "-100123=Suhaib"}) == "Suhaib"
    assert channel_name(row, {}) == "telegram -100123"
    assert channel_name({"source": "api", "external_id": "csi:X", "origin": ""}, {}) == ""


def test_a_text_signal_keeps_its_purchase_and_its_channel_unless_asked(rules):
    parsed = parse_signal(SIMPLE)
    kwargs = dict(budget=200, available_quote=1000, reserve_percent=20, current_price=84500,
                  signal_id="row", source="telegram", validity_confirmed=True, touch_stop=True)
    _, payload = prepare_signal(parsed, rules, source_name="Canal A", **kwargs)
    position = payload["position"]
    assert position["automation"]["keep_entry_if_tp_before_fill"] is True
    assert position["source_groups"][0]["label"] == "Canal A"
    _, payload = prepare_signal(parsed, rules, cancel_entry_if_tp1_first=True, **kwargs)
    assert payload["position"]["automation"]["keep_entry_if_tp_before_fill"] is False
    assert payload["position"]["source_groups"][0]["label"].startswith("Signal ")    # canal inconnu


def test_a_csi_signal_always_cancels_its_purchase_when_tp1_comes_first(btc_rules):  # noqa: F811
    _, payload = prepare_csi(csi_text(), btc_rules)
    assert payload["position"]["automation"]["keep_entry_if_tp_before_fill"] is False
    assert payload["position"]["automation"]["cancel_remaining_entries_on_first_tp"] is True


# ==========================================================================
# TP atteint avant l'achat
# ==========================================================================


def pending_purchase(*, keep: bool):
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", environment="DEMO",
                        creation_price=84000.0, status=PositionStatus.PENDING_ENTRIES, tags=["signal", "row-1"])
    position.entries.append(Entry(
        sequence_number=1, order_type=OrderType.LIMIT, status=EntryStatus.SUBMITTED, binance_qty=0.002,
        resolved_price=83000.0, order_id=4242, client_order_id="BSM-D-BTC-tp1-E1",
    ))
    position.take_profits.append(TakeProfit(sequence_number=1, target_price=90000.0, sell_percent=100.0))
    position.stop_loss.resolved_price = 80000.0
    position.stop_loss.status = SLStatus.PLANNED
    position.automation.cancel_remaining_entries_on_first_tp = True
    position.automation.keep_entry_if_tp_before_fill = keep
    recompute_position(position)
    return position


def seed_purchase(fake):
    fake.orders["BSM-D-BTC-tp1-E1"] = {
        "symbol": "BTCUSDT", "orderId": 4242, "clientOrderId": "BSM-D-BTC-tp1-E1", "side": "BUY",
        "type": "LIMIT", "status": "NEW", "price": "83000", "stopPrice": "0", "origQty": "0.002",
        "executedQty": "0", "cummulativeQuoteQty": "0", "fills": [],
    }


def test_by_default_a_text_signal_keeps_its_purchase_and_is_marked(engine, events):
    execution, fake, rules = engine
    seed_purchase(fake)
    position = pending_purchase(keep=True)
    result = build_automation(execution, rules, events).run_cycle(position, 90500.0)
    assert not result.errors and fake.cancelled == []
    assert position.entries[0].status is EntryStatus.SUBMITTED and position.is_open
    assert LATE_FILL_TAG in position.tags
    assert any("achat garde" in action for action in result.actions)


def test_the_setting_cancels_the_purchase_when_tp1_comes_first(engine, events):
    execution, fake, rules = engine
    seed_purchase(fake)
    position = pending_purchase(keep=False)
    result = build_automation(execution, rules, events).run_cycle(position, 90500.0)
    assert not result.errors and fake.cancelled == [4242]
    assert LATE_FILL_TAG not in position.tags


# ==========================================================================
# Statistiques par canal
# ==========================================================================


def test_channel_of_names_unknown_and_generic_sources(rules):
    position = make_position(rules)
    assert channel_of(position) == "manuel"
    assert channel_of(labelled(position, "Signal SIMPLE")) == UNKNOWN_TELEGRAM
    assert channel_of(labelled(position, "telegram")) == UNKNOWN_TELEGRAM
    assert channel_of(labelled(position, "Canal A")) == "Canal A"
    assert channel_of(labelled(position, "manual", SignalSource.MANUAL)) == "manuel"
    assert channel_of(labelled(position, "api", SignalSource.API)) == "api"


def test_stats_by_channel_sum_results_and_ignore_positions_never_bought(rules):
    loss = labelled(losing_position(rules), "Canal A")
    win = labelled(winning_position(rules), "Canal A")
    other = labelled(winning_position(rules), "Canal B")
    canceled = labelled(never_bought(rules), "Canal A")
    groups = {g.name: g for g in stats_by([loss, win, other, canceled])}
    a = groups["Canal A"]
    assert a.positions == 2 and a.wins == 1                     # la position sans achat ne compte pas
    assert a.net == pytest.approx(loss.pnl.realized + win.pnl.realized)
    assert a.profit_factor == pytest.approx(win.pnl.realized / abs(loss.pnl.realized))
    assert [g.name for g in stats_by([loss, win, other])][0] == "Canal B"   # trie du meilleur au pire


def test_late_fills_are_compared_to_the_others(rules):
    late = winning_position(rules)
    late.tags.append(LATE_FILL_TAG)
    split = late_fill_split([late, losing_position(rules)])
    assert split["achat apres TP1 deja touche"].positions == 1
    assert split["autres"].positions == 1


def test_a_channel_is_judged_losing_only_with_enough_closed_trades(rules):
    losers = [labelled(losing_position(rules), "Canal A") for _ in range(10)]
    detail = losing_channel(losers, "Canal A", min_trades=10)
    assert detail and "Canal « Canal A » perdant" in detail and "10 positions" in detail
    assert losing_channel(losers, "Canal A", min_trades=11) is None              # trop peu de recul
    assert losing_channel(losers, "Canal B", min_trades=1) is None
    assert losing_channel([labelled(losing_position(rules), UNKNOWN_TELEGRAM)], UNKNOWN_TELEGRAM,
                          min_trades=1) is None                                   # canal inconnu : jamais
    winners = [labelled(winning_position(rules), "Canal A") for _ in range(10)]
    assert losing_channel(winners + losers[:5], "Canal A", min_trades=10) is None   # canal gagnant
    still_open = labelled(make_position(rules), "Canal A")
    assert losing_channel(losers[:9] + [still_open], "Canal A", min_trades=10) is None


# ==========================================================================
# Routage : canal perdant et protection marché
# ==========================================================================


def test_a_losing_channel_sends_its_signals_to_review_only_when_enabled(tmp_path, rules):
    losers = [labelled(losing_position(rules), "Canal A") for _ in range(10)]
    for enabled, expected in ((True, ["REVIEW"]), (False, ["QUEUED"])):
        folder = tmp_path / str(enabled)
        folder.mkdir()
        inbox = SignalInbox(folder / "signals.db")
        row = inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1),
                            source_timestamp=995, origin="Canal A")
        preferences = enabled_preferences(signal_channel_review_enabled=enabled,
                                          signal_channel_review_min_trades=10)
        worker, commands = executor(folder, inbox, preferences, positions=positions_stub(losers))
        assert worker.process_pending() == expected
        saved = inbox.recent("demo")[0]
        if enabled:
            assert '"C_CHANNEL_LOSING"' in saved["route"] and "Canal « Canal A » perdant" in saved["auto_detail"]
            assert commands.list_recent("demo") == []
        else:
            payload = commands.get_by_request_key("demo", f"signal:{row['id']}")["payload"]
            assert payload["position"]["source_groups"][0]["label"] == "Canal A"


def test_another_channel_is_not_held_by_a_losing_one(tmp_path, rules):
    losers = [labelled(losing_position(rules), "Canal A") for _ in range(10)]
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995,
                  origin="Canal B")
    preferences = enabled_preferences(signal_channel_review_enabled=True, signal_channel_review_min_trades=10)
    worker, _ = executor(tmp_path, inbox, preferences, positions=positions_stub(losers))
    assert worker.process_pending() == ["QUEUED"]


def test_the_market_guard_holds_automatic_entries_like_a_suspension(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    worker, commands = executor(tmp_path, inbox, enabled_preferences())
    worker.market_guard_reason = "BTC −4.0 % en 4 h"
    assert worker.process_pending() == []
    assert worker.snapshot()["state"] == "MARKET_GUARD" and "BTC" in worker.snapshot()["last_detail"]
    assert inbox.recent("demo")[0]["auto_state"] == "" and commands.list_recent("demo") == []
    worker.market_guard_reason = ""
    assert worker.process_pending() == ["QUEUED"]


# ==========================================================================
# Protection marché
# ==========================================================================

HOUR = 3600
T0 = 1_000_000_000                      # secondes


def bar(close_time_s, close):
    close_ms = int(close_time_s * 1000)
    return [close_ms - 900_000 + 1, close, close, close, close, 0, close_ms]


def test_the_drop_uses_closed_candles_of_the_window_and_the_last_price():
    now_ms = T0 * 1000
    bars = [bar(T0 - 5 * HOUR, 120.0),                  # hors fenêtre
            bar(T0 - 3 * HOUR, 100.0), bar(T0 - 2 * HOUR, 99.0), bar(T0 - 900, 97.0),
            bar(T0 + 600, 80.0)]                        # bougie en cours : ignorée
    drop, peak, last = drop_over_window(bars, now_ms=now_ms, window_hours=4, last_price=96.0)
    assert (peak, last) == (100.0, 96.0) and drop == pytest.approx(4.0)
    assert drop_over_window(bars, now_ms=now_ms, window_hours=4)[0] == pytest.approx(3.0)
    assert drop_over_window(bars, now_ms=now_ms, window_hours=4, not_before_ms=(T0 - 2.5 * HOUR) * 1000)[1] == 99.0
    assert drop_over_window([], now_ms=now_ms, window_hours=4) is None


class Market:
    def __init__(self, bars, price):
        self.bars, self.price, self.fail = bars, price, False

    def klines(self, symbol, interval, limit=500):
        assert (symbol, interval) == ("BTCUSDT", "15m")
        if self.fail:
            raise RuntimeError("réseau")
        return self.bars

    def last(self, symbol):
        return self.price


def guard_for(tmp_path, market, preferences, clock):
    return MarketGuard(market.klines, market.last, lambda: preferences, path=tmp_path / "guard.json",
                       clock=lambda: clock[0])


def test_a_btc_drop_suspends_entries_until_the_pause_ends(tmp_path):
    market = Market([bar(T0 - 3 * HOUR, 100.0), bar(T0 - 900, 97.0)], 96.0)
    clock = [float(T0)]
    guard = guard_for(tmp_path, market, {}, clock)
    events = guard.check()
    assert [kind for kind, _ in events] == ["STARTED"] and "BTC −4.0 %" in events[0][1]
    assert "BTC −4.0 %" in guard.active_reason()
    assert active_status(tmp_path / "guard.json", now=T0 + 60)[1] == pytest.approx(T0 + 6 * HOUR)
    clock[0] = T0 + 60
    assert guard.check() == [] and guard.active_reason()
    clock[0] = T0 + 6 * HOUR + 1
    events = guard.check()
    assert [kind for kind, _ in events] == ["ENDED"] and "fin de la pause" in events[0][1]
    assert guard.active_reason() == "" and active_status(tmp_path / "guard.json") is None


def test_the_same_drop_does_not_restart_the_pause_but_a_new_one_does(tmp_path):
    market = Market([bar(T0 - HOUR, 100.0), bar(T0 - 900, 97.0)], 96.0)
    clock = [float(T0)]
    guard = guard_for(tmp_path, market, {"market_guard_pause_hours": 1.0}, clock)
    assert [k for k, _ in guard.check()] == ["STARTED"]
    clock[0] = T0 + HOUR + 1                     # fenêtre de 4 h : l'ancien plus haut y est encore
    assert [k for k, _ in guard.check()] == ["ENDED"]
    clock[0] = T0 + HOUR + 1 + CHECK_EVERY_SECONDS
    assert guard.check() == []
    market.bars = market.bars + [bar(T0 + 2 * HOUR, 95.0), bar(T0 + 2 * HOUR + 900, 93.0)]
    market.price = 91.0                          # nouvelle baisse de 4,2 % depuis le déclenchement
    clock[0] = T0 + 2 * HOUR + 1000
    assert [k for k, _ in guard.check()] == ["STARTED"]


def test_switching_the_guard_off_lifts_it_at_once(tmp_path):
    market = Market([bar(T0 - 3 * HOUR, 100.0), bar(T0 - 900, 97.0)], 96.0)
    clock = [float(T0)]
    preferences = {}
    guard = MarketGuard(market.klines, market.last, lambda: preferences, path=tmp_path / "guard.json",
                        clock=lambda: clock[0])
    assert [k for k, _ in guard.check()] == ["STARTED"]
    preferences["market_guard_enabled"] = False
    clock[0] = T0 + 60
    events = guard.check()
    assert [k for k, _ in events] == ["ENDED"] and "désactivée" in events[0][1]
    assert guard.active_reason() == ""


def test_a_small_drop_or_missing_data_never_triggers(tmp_path):
    market = Market([bar(T0 - 3 * HOUR, 100.0), bar(T0 - 900, 99.0)], 98.0)
    clock = [float(T0)]
    guard = guard_for(tmp_path, market, {}, clock)
    assert guard.check() == []                                       # −2 % < 3 %
    market.fail = True
    clock[0] = T0 + CHECK_EVERY_SECONDS
    assert guard.check() == [] and guard.active_reason() == ""
    disabled = guard_for(tmp_path, Market([bar(T0 - 900, 100.0)], 50.0), {"market_guard_enabled": False}, clock)
    assert disabled.check() == []


# ==========================================================================
# Worker : protection marché, stops remontés, rapport quotidien
# ==========================================================================


def guard_worker(journaled, rules, events, tmp_path, monkeypatch, positions, *, price):  # noqa: F811
    from binance_spot_manager.position_store import PositionStore
    from scripts import bot_worker

    execution, fake = journaled
    store = PositionStore(tmp_path / "positions")
    for position in positions:
        store.save(position)
    monkeypatch.setattr(bot_worker, "get_settings_store", lambda: SimpleNamespace(load=lambda: {}))
    worker = bot_worker.Worker.__new__(bot_worker.Worker)
    worker.automation = build_automation(execution, rules, events)
    worker.execution, worker.client, worker.positions, worker.events = execution, fake, store, events
    fake.price = price
    sent = []
    worker.notifications = SimpleNamespace(
        sl_moved=lambda p, before, after: ("sl", before, after),
        market_guard=lambda kind, detail: ("guard", kind),
        daily_report=lambda title, body: ("report", title, body),
        notify=sent.append,
        notify_position_event=lambda p, notice: sent.append(notice),
    )
    return worker, fake, store, sent


def test_the_guard_raises_stops_of_positions_in_profit_to_break_even(journaled, rules, events, tmp_path, monkeypatch):  # noqa: F811
    position = make_position(rules)
    break_even = position.metrics.break_even_with_fees
    worker, fake, store, sent = guard_worker(journaled, rules, events, tmp_path, monkeypatch, [position],
                                             price=86000.0)
    worker._tighten_stops()
    saved = store.load(position.position_id)
    assert saved.stop_loss.resolved_price == pytest.approx(break_even, abs=0.01)
    assert saved.stop_loss.resolved_price <= break_even
    stops = [o for o in fake.created if o["type"] == "STOP_LOSS_LIMIT"]
    assert stops and stops[-1]["stop_price"] == pytest.approx(saved.stop_loss.resolved_price)
    assert sent == [("sl", 80640.0, saved.stop_loss.resolved_price)]
    assert any("(protection marché)" in event.message for event in saved.history)


@pytest.mark.parametrize("price, stop", [(83000.0, 80640.0),       # sous le seuil de rentabilité
                                         (86000.0, 85000.0)])      # stop déjà au-dessus : jamais baissé
def test_the_guard_never_touches_a_losing_position_nor_lowers_a_stop(
        journaled, rules, events, tmp_path, monkeypatch, price, stop):  # noqa: F811
    position = make_position(rules)
    position.stop_loss.resolved_price = stop
    worker, fake, store, sent = guard_worker(journaled, rules, events, tmp_path, monkeypatch, [position],
                                             price=price)
    worker._tighten_stops()
    assert store.load(position.position_id).stop_loss.resolved_price == stop
    assert not [o for o in fake.created if o["type"] == "STOP_LOSS_LIMIT"] and sent == []


def test_the_guard_skips_a_position_still_waiting_for_a_purchase(journaled, rules, events, tmp_path, monkeypatch):  # noqa: F811
    position = make_position(rules)
    position.entries.append(Entry(sequence_number=2, order_type=OrderType.LIMIT, status=EntryStatus.SUBMITTED,
                                  binance_qty=0.002, resolved_price=82000.0, order_id=77, client_order_id="E2"))
    recompute_position(position)
    worker, fake, store, sent = guard_worker(journaled, rules, events, tmp_path, monkeypatch, [position],
                                             price=86000.0)
    worker._tighten_stops()
    assert store.load(position.position_id).stop_loss.resolved_price == 80640.0 and sent == []


def test_raise_stop_is_never_a_way_down(engine, events):
    execution, fake, rules = engine
    position = make_position(rules)
    automation = build_automation(execution, rules, events)
    assert automation.raise_stop(position, 80000.0, why="test").sl_moved_to is None
    position.automation.paused = True
    assert automation.raise_stop(position, 84500.0, why="test").sl_moved_to is None
    assert position.stop_loss.resolved_price == 80640.0 and fake.created == []


def test_the_worker_announces_the_guard_and_holds_the_executor(journaled, rules, events, tmp_path, monkeypatch):  # noqa: F811
    worker, fake, store, sent = guard_worker(journaled, rules, events, tmp_path, monkeypatch, [], price=86000.0)
    state = {"tighten_stops": True}
    tightened = []
    worker.market_guard = SimpleNamespace(check=lambda: [("STARTED", "BTC −4.0 % en 4 h")],
                                          active_reason=lambda: "BTC −4.0 % en 4 h", state=lambda: state)
    worker.auto_signal_executor = SimpleNamespace(market_guard_reason="")
    worker._tighten_stops = lambda: tightened.append(True)
    worker._check_market_guard()
    assert worker.auto_signal_executor.market_guard_reason == "BTC −4.0 % en 4 h"
    assert sent == [("guard", "STARTED")] and tightened == [True]
    assert events.tail(limit=1)[0]["event"] == "MARKET_GUARD_STARTED"

    def broken():
        raise OSError("disque")
    worker.market_guard = SimpleNamespace(check=broken)
    worker._check_market_guard()                                     # jamais bloquant
    assert worker.auto_signal_executor.market_guard_reason == "BTC −4.0 % en 4 h"


def test_the_daily_report_is_sent_once_and_retried_after_a_failure(journaled, rules, events, tmp_path, monkeypatch):  # noqa: F811
    worker, fake, store, sent = guard_worker(journaled, rules, events, tmp_path, monkeypatch,
                                             [losing_position(rules)], price=86000.0)
    marks = []
    worker.daily_report = SimpleNamespace(due=lambda: NOW, mark_sent=marks.append)
    worker._send_daily_report()
    assert marks == [NOW] and sent and sent[0][0] == "report" and "Rapport du 05/10" in sent[0][1]

    def fail(title, body):
        raise RuntimeError("telegram")
    worker.notifications.daily_report = fail
    worker._send_daily_report()
    assert marks == [NOW] and worker._daily_report_retry_at > 0
    worker.notifications.daily_report = lambda title, body: ("report", title, body)
    worker._send_daily_report()                                      # attend 10 minutes avant de réessayer
    assert marks == [NOW]


# ==========================================================================
# Rapport quotidien
# ==========================================================================


def test_the_report_is_due_once_a_day_after_the_chosen_hour(tmp_path):
    clock = [datetime(2026, 10, 5, 19, 59, tzinfo=UTC).timestamp()]
    preferences = {"daily_report_hour_utc": 20}
    report = DailyReport(lambda: preferences, path=tmp_path / "report.json", clock=lambda: clock[0])
    assert report.due() is None
    clock[0] += 120
    moment = report.due()
    assert moment is not None and moment.date().isoformat() == "2026-10-05"
    report.mark_sent(moment)
    assert report.due() is None                                      # déjà envoyé aujourd'hui
    restarted = DailyReport(lambda: preferences, path=tmp_path / "report.json", clock=lambda: clock[0])
    assert restarted.due() is None                                   # ni après un redémarrage
    clock[0] = datetime(2026, 10, 6, 20, 0, tzinfo=UTC).timestamp()
    assert report.due() is not None
    preferences["daily_report_enabled"] = False
    assert report.due() is None


def test_the_report_sums_today_and_the_week_and_names_the_channels(rules):
    loss = labelled(losing_position(rules), "Canal B")
    win = labelled(winning_position(rules), "Canal A")
    old_win = labelled(winning_position(rules), "Canal A")
    canceled = labelled(never_bought(rules), "Canal B")
    loss.closed_at = NOW - timedelta(hours=2)
    win.closed_at = NOW - timedelta(hours=1)
    canceled.closed_at = NOW - timedelta(hours=1)
    old_win.closed_at = NOW - timedelta(days=3)
    still_open = make_position(rules)
    title, body = build([loss, win, old_win, canceled, still_open], NOW, lambda p: {})
    today = loss.pnl.realized + win.pnl.realized
    assert title == f"Rapport du 05/10 — {today:+.2f} USDT aujourd'hui"
    assert f"Aujourd'hui : {today:+.2f} USDT sur 2 position(s) terminée(s), 1 gagnante(s)" in body
    assert "7 derniers jours : " in body and "sur 3 position(s)" in body
    assert f"Risque si tous les stops sont touchés : −{abs(still_open.metrics.max_loss_at_sl):.2f} USDT" in body
    assert "Meilleur canal (7 j) : Canal A" in body and "Pire canal (7 j) : Canal B" in body


def test_guard_and_report_notifications(settings):  # noqa: F811
    notifier = NotificationEngine(settings)
    started = notifier.market_guard("STARTED", "BTC −4.0 %")
    assert started.event == "MARKET_GUARD" and started.level == "WARNING" and "suspendues" in started.title
    ended = notifier.market_guard("ENDED", "fin")
    assert ended.level == "INFO" and "levee" in ended.title
    report = notifier.daily_report("Rapport du 05/10", "corps")
    assert (report.event, report.title, report.body) == ("DAILY_REPORT", "Rapport du 05/10", "corps")


# ==========================================================================
# BSM face au marché
# ==========================================================================

Q = 900_000


def test_price_at_reads_the_candle_that_contains_the_moment():
    opens = [(0, 100.0), (Q, 110.0)]
    at = lambda ms: datetime.fromtimestamp(ms / 1000, tz=UTC)  # noqa: E731
    assert price_at(opens, at(1000)) == 100.0
    assert price_at(opens, at(Q + 5)) == 110.0
    assert price_at(opens, at(2 * Q)) is None                     # après la dernière bougie connue
    assert price_at([(Q, 110.0)], at(0)) is None
    assert price_at([], at(0)) is None


def test_btc_candles_are_read_page_by_page():
    calls = []

    def klines(symbol, interval, *, start_time, limit):
        calls.append(start_time)
        end = min(start_time + limit * Q, 2500 * Q)
        return [[t, float(t // Q)] for t in range(start_time, end, Q)]

    opens = btc_opens(klines, datetime.fromtimestamp(0, tz=UTC), datetime.fromtimestamp(2499 * Q / 1000, tz=UTC))
    assert len(opens) == 2500 and calls == [0, 1000 * Q, 2000 * Q]


def comparable(rules, symbol="ALTUSDT", base="ALT", quote="USDT"):
    position = make_position(rules)
    position.symbol, position.base_asset, position.quote_asset = symbol, base, quote
    position.entries[0].filled_at = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    recompute_position(position)
    return position


def test_bsm_is_compared_to_holding_and_to_btc_with_the_same_money(rules):
    position = comparable(rules)
    prices = {"ALTUSDT": 90000.0, "BTCUSDT": 66000.0}
    result = compare([position], price_now=prices.get, fee_rates_for=lambda p: {},
                     btc_at=lambda moment: 60000.0)
    row = result.rows[0]
    valued = position.model_copy(deep=True)
    valued.metrics.current_price = 90000.0
    recompute_position(valued)
    assert row.invested == pytest.approx(504.0) and result.invested == pytest.approx(504.0)
    assert row.bsm == pytest.approx(valued.pnl.realized + valued.pnl.unrealized)
    assert row.hold == pytest.approx(0.006 * 90000.0 - 504.0 - 0.5)          # frais d'achat USDT compris
    assert row.btc == pytest.approx(504.0 * (66000.0 / 60000.0 - 1) - 0.5)
    assert position.metrics.current_price != 90000.0                        # la position n'est pas modifiée


def test_positions_without_price_purchase_or_usdt_are_left_out(rules):
    no_price = comparable(rules, symbol="NOPRICEUSDT", base="NOPRICE")
    in_btc = comparable(rules, symbol="ALTBTC", base="ALT", quote="BTC")
    not_bought = make_position(rules, entry_status=EntryStatus.SUBMITTED)
    not_bought.entries[0].executed_qty = 0.0
    prices = {"BTCUSDT": 66000.0}
    result = compare([no_price, in_btc, not_bought], price_now=prices.get, fee_rates_for=lambda p: {},
                     btc_at=lambda moment: 60000.0)
    assert result.rows == [] and result.hold is None and result.btc is None
    assert sorted(result.skipped) == ["ALTBTC (hors USDT)", "NOPRICEUSDT (prix actuel indisponible)"]


# ==========================================================================
# Settings
# ==========================================================================


def test_settings_save_the_guard_the_report_and_the_signal_options(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    from binance_spot_manager.position_store import JsonFileStore

    store = JsonFileStore(tmp_path / "settings.json")
    monkeypatch.setattr("binance_spot_manager.position_store.get_settings_store", lambda: store)
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
                        lambda self: {"BNB": {"free": 0.1, "locked": 0}})
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_price",
                        lambda self, symbol: 500)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    assert not app.exception

    app.number_input(key="market_guard_drop_input").set_value(5.0)
    app.toggle(key="market_guard_tighten_toggle").set_value(True)
    next(b for b in app.button if b.label == "Enregistrer la protection").click().run()
    assert not app.exception
    saved = store.load()
    assert saved["market_guard_drop_percent"] == 5.0 and saved["market_guard_tighten_stops"] is True
    assert saved["market_guard_window_hours"] == 4.0 and saved["market_guard_enabled"] is True

    app.number_input(key="daily_report_hour_input").set_value(7)
    next(b for b in app.button if b.label == "Enregistrer le rapport").click().run()
    assert store.load()["daily_report_hour_utc"] == 7

    app.toggle(key="signal_cancel_entry_if_tp1_first_toggle").set_value(True)
    next(b for b in app.button if b.label == "Enregistrer le réglage d'achat").click().run()
    assert store.load()["signal_cancel_entry_if_tp1_first"] is True

    app.toggle(key="signal_channel_review_toggle").set_value(True)
    next(b for b in app.button if b.label == "Enregistrer la règle du canal").click().run()
    assert not app.exception
    assert store.load()["signal_channel_review_enabled"] is True
    assert store.load()["signal_channel_review_min_trades"] == 30


def test_the_dashboard_compares_bsm_to_the_market(monkeypatch, tmp_path, rules):  # noqa: F811
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from binance_spot_manager.models import utcnow
    from binance_spot_manager.position_store import PositionStore
    from ui_common import get_service

    st.cache_data.clear()                                   # pas de comparaison gardée d'un autre test
    position = comparable(rules)
    position.entries[0].filled_at = utcnow() - timedelta(days=1)
    store = PositionStore(tmp_path / "positions")
    store.save(position)
    service = get_service()
    prices = {"ALTUSDT": 90000.0, "BTCUSDT": 66000.0}
    monkeypatch.setattr(service, "positions", store)
    monkeypatch.setattr(service.client, "get_balances", lambda: {})
    monkeypatch.setattr(service.client, "get_prices", lambda *a, **k: dict(prices))
    monkeypatch.setattr(service.client, "get_open_orders", lambda *a, **k: [])
    monkeypatch.setattr(service.client, "get_price", lambda symbol: prices.get(symbol))
    monkeypatch.setattr(service.client, "get_klines",
                        lambda symbol, interval, *, start_time=None, limit=500: [[start_time, 60000.0]])
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "pages" / "1_Dashboard.py"),
                            default_timeout=30).run()
    assert not app.exception
    assert "Montant acheté" in [metric.label for metric in app.metric]
    shown = " ".join(item.value for item in app.markdown)
    assert f"{504.0 * (66000.0 / 60000.0 - 1) - 0.5:+.2f}".lstrip("+") in shown.replace(",", "")
    st.cache_data.clear()


def test_history_shows_results_by_channel_and_late_fills(monkeypatch, tmp_path, rules):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from binance_spot_manager.position_store import PositionStore
    from ui_common import get_service

    store = PositionStore(tmp_path / "positions")
    late = labelled(winning_position(rules), "Canal A")
    late.tags.append(LATE_FILL_TAG)
    for position in (late, labelled(losing_position(rules), "Canal B"), labelled(never_bought(rules), "Canal B")):
        store.save(position)
    service = get_service()
    monkeypatch.setattr(service, "positions", store)
    monkeypatch.setattr(service, "fee_rates", lambda position: {})
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "pages" / "4_History.py"),
                            default_timeout=30).run()
    assert not app.exception
    tables = [frame.value for frame in app.dataframe]
    by_channel = next(t for t in tables if "PnL net" in t.columns and "Canal" in t.columns)
    assert list(by_channel["Canal"]) == ["Canal A", "Canal B"]
    assert list(by_channel["Positions"]) == [1, 1]                  # la position sans achat ne compte pas
    late_table = next(t for t in tables if "Achats" in t.columns)
    assert "achat après TP1 déjà touché" in list(late_table["Achats"])
