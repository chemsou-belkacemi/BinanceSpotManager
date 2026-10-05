"""Stop de secours chez Binance pour un SL à la clôture de bougie : un vrai ordre stop, X % sous le niveau de
clôture, protège la position même si le worker s'arrête ; la clôture reste surveillée ; jamais deux ordres stop pour
la même quantité (vente TP, déplacement du stop, sortie à la clôture) ; sans réglage, rien ne change."""
from __future__ import annotations

import pytest

from binance_spot_manager.models import CloseReason, SLRuleAfterTP, SLStatus, SLTrigger
from binance_spot_manager.signal_parser import parse_signal
from binance_spot_manager.signal_plan import prepare_signal
from binance_spot_manager.watchdog import exposure
from test_automation import SL_CLIENT_ID, SL_ORDER_ID, events, rules, settings  # noqa: F401 - fixtures partagees
from test_candle_stop import STOP, T0, candle_position, candles, kline, worker_for  # noqa: F401

BACKUP = 78220.8          # 80 640 × (1 − 3 %), arrondi au tick de 0,01 vers le bas


def backup_position(rules, percent=3.0, **options):  # noqa: F811
    position = candle_position(rules, **options)
    position.stop_loss.backup_percent = percent
    return position


def live_stops(fake):
    return sorted(float(o["stopPrice"]) for o in fake.orders.values()
                  if o["type"] == "STOP_LOSS_LIMIT" and o["status"] in {"NEW", "PARTIALLY_FILLED"})


def test_the_backup_is_placed_below_the_candle_level_and_candles_stay_watched(candles, rules):  # noqa: F811
    automation, fake, _ = candles
    position = backup_position(rules)
    fake.klines = [kline(T0, 81000)]
    result = automation.run_cycle(position, 81500)
    assert live_stops(fake) == [pytest.approx(BACKUP)] and not result.errors
    sl = position.stop_loss
    assert sl.status is SLStatus.ACTIVE and sl.trigger is SLTrigger.CANDLE_CLOSE and sl.resolved_price == STOP
    assert fake.kline_calls                                                    # la clôture reste surveillée
    automation.run_cycle(position, 81500)
    assert len([o for o in fake.created if o["type"] == "STOP_LOSS_LIMIT"]) == 1   # jamais empilé
    assert any("Stop de secours pose" in action for action in result.actions)


def test_without_the_setting_nothing_changes(candles, rules):  # noqa: F811
    automation, fake, _ = candles
    position = backup_position(rules, percent=0.0)
    fake.klines = [kline(T0, 81000)]
    automation.run_cycle(position, 81500)
    assert fake.created == [] and position.stop_loss.status is SLStatus.PLANNED
    assert position.stop_loss.binance_stop_price() is None


def test_a_candle_close_exit_cancels_the_backup_before_selling(settings, rules, events, tmp_path, monkeypatch):  # noqa: F811
    position = backup_position(rules)
    worker, fake, sent = worker_for(position, settings, rules, events, tmp_path, monkeypatch)
    fake.seed_stop_loss(qty=0.006, stop=BACKUP)
    sl = position.stop_loss
    sl.status, sl.order_id, sl.client_order_id = SLStatus.ACTIVE, SL_ORDER_ID, SL_CLIENT_ID
    worker.positions.save(position)
    worker._process_position(position, 81500)                                 # bougie 15m close à 79 000
    assert SL_ORDER_ID in fake.cancelled and live_stops(fake) == []
    assert [o["qty"] for o in fake.created if o["type"] == "MARKET"] == pytest.approx([0.006])
    assert not position.is_open and position.close_reason is CloseReason.SL_CANDLE_CLOSE


def test_a_tp_releases_the_backup_and_puts_it_back_at_the_backup_level(candles, rules):  # noqa: F811
    automation, fake, _ = candles
    position = backup_position(rules)
    fake.klines = [kline(T0, 81000)]
    automation.run_cycle(position, 81500)
    result = automation.run_cycle(position, 86600)
    assert result.tp_executed == 1
    stops = [o["stop_price"] for o in fake.created if o["type"] == "STOP_LOSS_LIMIT"]
    assert stops == [pytest.approx(BACKUP), pytest.approx(BACKUP)]           # reposé au niveau de secours
    assert live_stops(fake) == [pytest.approx(BACKUP)]
    assert position.stop_loss.trigger is SLTrigger.CANDLE_CLOSE and position.stop_loss.resolved_price == STOP


def test_a_trailing_rule_leaves_a_single_price_stop(candles, rules):  # noqa: F811
    automation, fake, _ = candles
    position = backup_position(rules, tp_rules={1: SLRuleAfterTP.FIXED_PRICE})
    position.take_profits[0].sl_rule_value = 84000.0
    fake.klines = [kline(T0, 81000)]
    automation.run_cycle(position, 81500)
    automation.run_cycle(position, 86600)
    assert live_stops(fake) == [84000.0] and position.stop_loss.trigger is SLTrigger.TOUCH


def test_raising_the_stop_replaces_the_live_backup(candles, rules):  # noqa: F811
    automation, fake, _ = candles
    position = backup_position(rules)
    fake.klines = [kline(T0, 81000)]
    automation.run_cycle(position, 81500)
    outcome = automation.raise_stop(position, 84100.0, why="(protection marché)")
    assert outcome.sl_moved_to == 84100.0 and live_stops(fake) == [84100.0]
    assert position.stop_loss.trigger is SLTrigger.TOUCH and position.stop_loss.status is SLStatus.ACTIVE


def test_a_filled_backup_closes_the_position(candles, rules):  # noqa: F811
    automation, fake, _ = candles
    position = backup_position(rules)
    fake.klines = [kline(T0, 81000)]
    automation.run_cycle(position, 81500)
    order = next(o for o in fake.orders.values() if o["type"] == "STOP_LOSS_LIMIT")
    order.update(status="FILLED", executedQty=order["origQty"],
                 cummulativeQuoteQty=str(float(order["origQty"]) * 78000.0))
    result = automation.run_cycle(position, 77000)
    assert result.position_finished and not position.is_open
    assert position.stop_loss.status is SLStatus.EXECUTED


def test_signals_with_a_candle_stop_carry_the_backup_setting(rules):  # noqa: F811
    text = "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000 (1h)"
    options = dict(budget=200, available_quote=1000, reserve_percent=20, current_price=84500, signal_id="row",
                   validity_confirmed=True)
    _, payload = prepare_signal(parse_signal(text), rules, candle_backup_percent=3.0, **options)
    sl = payload["position"]["stop_loss"]
    assert sl["trigger"] == "CANDLE_CLOSE" and sl["backup_percent"] == 3.0
    _, payload = prepare_signal(parse_signal(text), rules, candle_backup_percent=99, **options)
    assert payload["position"]["stop_loss"]["backup_percent"] == 20.0                    # borné
    _, payload = prepare_signal(parse_signal(text), rules, touch_stop=True, candle_backup_percent=3.0, **options)
    assert payload["position"]["stop_loss"]["trigger"] == "TOUCH"


def test_automatic_signals_use_the_saved_backup_setting(tmp_path):
    from binance_spot_manager.signal_inbox import SignalInbox
    from test_signal_auto_execution import enabled_preferences, executor, telegram_id

    text = "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000 (1h)"
    for folder, preferences, expected in (("default", {}, 3.0), ("off", {"signal_candle_backup_percent": 0}, 0.0)):
        (tmp_path / folder).mkdir()
        inbox = SignalInbox(tmp_path / folder / "signals.db")
        row = inbox.receive("demo", text, source="telegram", external_id=telegram_id(1), source_timestamp=995)
        worker, commands = executor(tmp_path / folder, inbox, enabled_preferences(**preferences))
        assert worker.process_pending() == ["QUEUED"]
        payload = commands.get_by_request_key("demo", f"signal:{row['id']}")["payload"]
        assert payload["position"]["stop_loss"]["backup_percent"] == expected


def test_the_watchdog_counts_a_live_backup_as_protection(rules):  # noqa: F811
    without, protected = candle_position(rules), backup_position(rules)
    protected.stop_loss.status, protected.stop_loss.order_id = SLStatus.ACTIVE, SL_ORDER_ID
    assert exposure([without, protected]).unprotected == ["BTCUSDT"]
