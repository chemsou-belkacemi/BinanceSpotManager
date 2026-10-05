"""SL a la cloture de bougie : aucun ordre stop Binance, vente au marche apres une cloture au SL."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from binance_spot_manager.candle_stop import evaluate_closed_candles, kline_interval
from binance_spot_manager.models import CloseReason, SLRuleAfterTP, SLStatus, SLTrigger
from binance_spot_manager.protection_status import protection_alerts, protection_overview
from test_automation import (  # noqa: F401 - fixtures partagees
    FakeClient,
    JournaledFakeClient,
    build_automation,
    build_execution,
    events,
    make_position,
    rules,
    settings,
)

pytestmark = pytest.mark.unit

MIN = 60_000
QUARTER = 15 * MIN
T0 = 1_789_999_200_000  # debut d'une bougie 15m (multiple de 15 minutes)
STOP = 80640.0  # SL de make_position


def at(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def kline(open_ms, close, interval_ms=QUARTER):
    return [open_ms, "0", "0", "0", str(close), "0", open_ms + interval_ms - 1]


class CandleMixin:
    def setup_candles(self):
        self.klines, self.kline_calls = [], []

    def get_klines(self, symbol, interval, *, start_time=None, limit=500):
        self.kline_calls.append((symbol, interval, start_time))
        return [k for k in self.klines if start_time is None or k[0] >= start_time][:limit]


class CandleClient(CandleMixin, FakeClient):
    def __init__(self, rules):
        super().__init__(rules)
        self.setup_candles()


class JournaledCandleClient(CandleMixin, JournaledFakeClient):
    def __init__(self, rules, journal_path):
        super().__init__(rules, journal_path)
        self.setup_candles()


def candle_position(rules, *, interval="15m", armed_at=T0 + 5 * MIN, tp_rules=None):
    position = make_position(rules, tp_rules=tp_rules)
    sl = position.stop_loss
    sl.trigger, sl.candle_interval = SLTrigger.CANDLE_CLOSE, interval
    sl.status, sl.order_id, sl.client_order_id = SLStatus.PLANNED, None, None
    position.created_at = at(T0)
    position.entries[0].filled_at = at(armed_at)
    return position


@pytest.fixture
def candles(settings, rules, events):
    fake = CandleClient(rules)
    automation = build_automation(build_execution(settings, rules, events, fake), rules, events)
    clock = {"now": T0 + 16 * MIN}
    automation.clock = lambda: clock["now"] / 1000
    return automation, fake, clock


@pytest.mark.parametrize("text,interval", [
    ("15m", "15m"), ("15min", "15m"), ("30m", "30m"), ("1h", "1h"), ("4H", "4h"),
    ("1d", "1d"), ("1w", "1w"), ("bougie", None), ("2d", None), ("", None),
])
def test_signal_timeframes_map_to_binance_candles(text, interval):
    assert kline_interval(text) == interval


def test_only_closed_candles_after_arming_count_and_the_stop_level_itself_exits():
    klines = [kline(T0, 70000), kline(T0 + QUARTER, 81000), kline(T0 + 2 * QUARTER, STOP),
              kline(T0 + 3 * QUARTER, 60000)]
    breach, checked = evaluate_closed_candles(
        klines, stop_price=STOP, armed_at_ms=T0 + 5 * MIN + QUARTER, now_ms=T0 + 3 * QUARTER + MIN,
    )
    # La bougie T0 (avant l'achat) est ignoree, la bougie en cours aussi ; cloture == SL : sortie.
    assert breach.close_price == STOP and breach.close_time == T0 + 3 * QUARTER - 1
    assert checked == breach.close_time
    assert evaluate_closed_candles(klines[:2], stop_price=STOP, armed_at_ms=T0 + 20 * MIN,
                                   now_ms=T0 + 2 * QUARTER) == (None, T0 + 2 * QUARTER - 1)
    with pytest.raises(ValueError):
        evaluate_closed_candles([["x"]], stop_price=STOP, armed_at_ms=0, now_ms=T0)


def test_price_under_the_stop_sells_nothing_until_a_candle_closes_under_it(candles, rules):
    automation, fake, clock = candles
    position = candle_position(rules)
    fake.klines = [kline(T0, 81000), kline(T0 + QUARTER, 79000)]  # la seconde est encore ouverte

    result = automation.run_cycle(position, 79000)  # un stop au toucher serait parti
    assert result.candle_stop_hit is None
    assert fake.created == []  # aucun ordre stop Binance, jamais
    assert position.stop_loss.candle_checked_until == T0 + QUARTER - 1

    automation.run_cycle(position, 79000)
    assert len(fake.kline_calls) == 1  # aucune nouvelle bougie possible : pas de relecture

    clock["now"] = T0 + 2 * QUARTER + MIN  # la bougie a 79000 est cloturee
    result = automation.run_cycle(position, 81500)  # le rebond ne l'annule pas
    assert result.candle_stop_hit.close_price == 79000
    assert result.exits_blocked and result.tp_executed is None
    assert fake.created == []  # la vente passe par le worker (cloture au marche)


def test_candles_missed_while_the_worker_was_stopped_are_caught_up(candles, rules):
    automation, fake, clock = candles
    position = candle_position(rules)
    position.stop_loss.candle_checked_until = T0 + QUARTER - 1
    fake.klines = [kline(T0 + QUARTER * n, close) for n, close in
                   enumerate([81000, 82000, 80000, 85000, 86000], start=1)]
    clock["now"] = T0 + 6 * QUARTER + MIN

    result = automation.run_cycle(position, 86000)
    assert result.candle_stop_hit.close_price == 80000
    assert fake.kline_calls[0][2] == T0 + QUARTER  # reprise juste apres la derniere bougie lue


def test_unreadable_candles_are_reported_never_ignored(candles, rules):
    automation, fake, _ = candles
    fake.get_klines = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout"))
    result = automation.run_cycle(candle_position(rules), 81000)
    assert any("bougies illisibles" in error for error in result.errors)


def test_trailing_rule_turns_the_moved_candle_stop_into_a_binance_price_stop(candles, rules):
    automation, fake, _ = candles
    position = candle_position(rules, tp_rules={1: SLRuleAfterTP.FIXED_PRICE})
    position.take_profits[0].sl_rule_value = 84000.0

    result = automation.run_cycle(position, 86600)
    assert result.tp_executed == 1
    stops = [order for order in fake.created if order["type"] == "STOP_LOSS_LIMIT"]
    assert [order["stop_price"] for order in stops] == [84000.0]
    assert position.stop_loss.trigger is SLTrigger.TOUCH and position.stop_loss.status is SLStatus.ACTIVE


def test_without_trailing_the_candle_stop_keeps_its_price_and_mode_after_a_tp(candles, rules):
    automation, fake, _ = candles
    position = candle_position(rules)

    result = automation.run_cycle(position, 86600)
    assert result.tp_executed == 1
    assert not [order for order in fake.created if order["type"] == "STOP_LOSS_LIMIT"]
    sl = position.stop_loss
    assert (sl.trigger, sl.candle_interval, sl.resolved_price) == (SLTrigger.CANDLE_CLOSE, "15m", STOP)


def worker_for(position, settings, rules, events, tmp_path, monkeypatch, preferences=None):
    from binance_spot_manager.position_store import PositionStore
    from scripts import bot_worker

    fake = JournaledCandleClient(rules, tmp_path / "order_intents.sqlite3")
    execution = build_execution(settings, rules, events, fake)
    store = PositionStore(tmp_path / "positions")
    store.save(position)
    monkeypatch.setattr(bot_worker, "get_settings_store",
                        lambda: SimpleNamespace(load=lambda: preferences or {}))
    worker = bot_worker.Worker.__new__(bot_worker.Worker)
    worker.automation = build_automation(execution, rules, events)
    worker.automation.clock = lambda: (T0 + 2 * QUARTER + MIN) / 1000
    worker.execution, worker.client, worker.positions, worker.events = execution, fake, store, events
    sent = []
    worker.notifications = SimpleNamespace(
        candle_stop_exit=lambda *a: "candle_exit", stop_crossed_paused=lambda *a: "paused",
        tp_executed=lambda *a: "tp", position_finished=lambda *a, **k: "finished",
        notify_position_event=lambda position, notice: sent.append(notice),
    )
    fake.klines = [kline(T0, 81000), kline(T0 + QUARTER, 79000)]
    return worker, fake, sent


def test_worker_sells_the_position_at_market_after_a_candle_closes_under_the_stop(
    settings, rules, events, tmp_path, monkeypatch,
):
    position = candle_position(rules)
    worker, fake, sent = worker_for(position, settings, rules, events, tmp_path, monkeypatch)

    worker._process_position(position, 81500)

    assert not position.is_open
    assert position.close_reason is CloseReason.SL_CANDLE_CLOSE
    assert [o["qty"] for o in fake.created if o["type"] == "MARKET"] == pytest.approx([0.006])
    assert not [o for o in fake.created if o["type"] == "STOP_LOSS_LIMIT"]
    assert "candle_exit" in sent and "paused" not in sent


def test_disabled_market_exit_pauses_the_position_instead_of_selling(
    settings, rules, events, tmp_path, monkeypatch,
):
    position = candle_position(rules)
    worker, fake, sent = worker_for(position, settings, rules, events, tmp_path, monkeypatch,
                                    preferences={"exit_on_crossed_stop": False})

    worker._process_position(position, 81500)

    assert position.is_open and position.automation.paused
    assert fake.created == []
    assert sent == ["paused"]


def test_candle_stop_is_reported_as_worker_protection_not_as_a_missing_stop(rules):
    position = candle_position(rules)
    assert protection_alerts([position]) == []
    assert "clôture 15m" in protection_overview([position])[0]["Protection"]
