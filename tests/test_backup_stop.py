"""Stop de secours chez Binance pour un SL à la clôture de bougie : un vrai ordre stop, X % sous le niveau de
clôture, protège la position même si le worker s'arrête ; la clôture reste surveillée ; jamais deux ordres stop pour
la même quantité (vente TP, déplacement du stop, sortie à la clôture) ; sans réglage, rien ne change."""
from __future__ import annotations

import pytest

from binance_spot_manager.binance_client import BinanceError
from binance_spot_manager.models import CloseReason, SLRuleAfterTP, SLStatus, SLTrigger
from binance_spot_manager.signal_parser import parse_signal
from binance_spot_manager.signal_plan import prepare_signal
from binance_spot_manager.watchdog import exposure
from test_automation import SL_CLIENT_ID, SL_ORDER_ID, events, rules, settings  # noqa: F401 - fixtures partagees
from test_candle_stop import MIN, QUARTER, STOP, T0, candle_position, candles, kline, worker_for  # noqa: F401

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


# --- Relecture du 2026-10-05 : chemins d'échec ---------------------------------------------------------------------


def placed(automation, fake, position):
    fake.klines = [kline(T0, 81000)]
    automation.run_cycle(position, 81500)
    assert position.stop_loss.status is SLStatus.ACTIVE
    return next(o for o in fake.orders.values() if o["type"] == "STOP_LOSS_LIMIT")


def test_an_uncertain_backup_is_settled_before_any_candle_exit(candles, rules):  # noqa: F811
    automation, fake, clock = candles
    position = backup_position(rules)
    fake.klines = [kline(T0, 81000)]
    fake.fail_next = BinanceError("Timeout", code=-1007)                      # réponse perdue
    automation.run_cycle(position, 81500)
    sl = position.stop_loss
    assert sl.status is SLStatus.REPLACING
    fake.klines.append(kline(T0 + QUARTER, 79000))                           # clôture fautive pendant l'incertitude
    clock["now"] = T0 + 2 * QUARTER + MIN
    result = automation.run_cycle(position, 79500)
    assert result.candle_stop_hit is None and not position.automation.paused
    assert sl.candle_checked_until is None or sl.candle_checked_until < T0 + 2 * QUARTER - 1   # rien de consommé
    # L'ordre était bien arrivé chez Binance : il est adopté, puis la clôture fautive demande la sortie.
    fake.orders[sl.client_order_id] = {
        "symbol": "BTCUSDT", "orderId": 999, "clientOrderId": sl.client_order_id, "side": "SELL",
        "type": "STOP_LOSS_LIMIT", "status": "NEW", "price": "77986.1", "stopPrice": str(BACKUP), "origQty": "0.006",
        "executedQty": "0", "cummulativeQuoteQty": "0", "fills": []}
    result = automation.run_cycle(position, 79500)
    assert sl.status is SLStatus.ACTIVE and result.candle_stop_hit.close_price == 79000


def test_a_breach_candle_is_not_consumed_until_the_exit_is_done(candles, rules):  # noqa: F811
    automation, fake, clock = candles
    position = backup_position(rules, percent=0.0)
    fake.klines = [kline(T0, 81000), kline(T0 + QUARTER, 79000)]
    clock["now"] = T0 + 2 * QUARTER + MIN
    first = automation.run_cycle(position, 79500)
    assert first.candle_stop_hit.close_price == 79000
    again = automation.run_cycle(position, 79500)                             # vente échouée : redemandée
    assert again.candle_stop_hit.close_price == 79000


def test_a_refused_cancel_keeps_the_backup_and_the_candle_watch(candles, rules):  # noqa: F811
    automation, fake, _ = candles
    position = backup_position(rules)
    order = placed(automation, fake, position)
    fake.orders.pop(order["clientOrderId"])                                  # l'annulation ne peut être confirmée
    outcome = automation.raise_stop(position, 84100.0, why="(protection marché)")
    sl = position.stop_loss
    assert outcome.sl_moved_to is None and outcome.errors
    assert (sl.trigger, sl.candle_interval, sl.resolved_price) == (SLTrigger.CANDLE_CLOSE, "15m", STOP)


def test_no_move_while_a_stop_is_uncertain(candles, rules):  # noqa: F811
    automation, fake, _ = candles
    position = backup_position(rules)
    position.stop_loss.status, position.stop_loss.client_order_id = SLStatus.REPLACING, "BSM-X-SL"
    outcome = automation.raise_stop(position, 84100.0, why="(protection marché)")
    assert any("incertain" in error for error in outcome.errors) and fake.created == []
    assert position.stop_loss.resolved_price == STOP and position.stop_loss.trigger is SLTrigger.CANDLE_CLOSE


def test_an_uncertain_manual_move_becomes_a_price_stop_at_the_target(settings, rules, tmp_path):  # noqa: F811
    from types import SimpleNamespace

    from binance_spot_manager.command_processor import CommandProcessor
    from binance_spot_manager.execution_engine import OrderResult
    from binance_spot_manager.position_store import PositionStore

    position = backup_position(rules)
    position.stop_loss.status, position.stop_loss.order_id = SLStatus.ACTIVE, SL_ORDER_ID
    store = PositionStore(tmp_path / "positions")
    store.save(position)

    def uncertain_move(current, *, new_stop_price, quantity, keep_level=False):
        sl = current.stop_loss
        sl.status, sl.order_id, sl.client_order_id = SLStatus.REPLACING, None, "BSM-X-SL1"
        sl.resolved_price = new_stop_price                                    # place_stop_loss sans keep_level
        return OrderResult(success=False, status="UNKNOWN", error="Statut du SL incertain")

    execution = SimpleNamespace(settings=settings, client=SimpleNamespace(get_price=lambda symbol: 86000.0),
                                move_stop_loss=uncertain_move)
    processor = CommandProcessor(None, store, execution, lambda: None)
    try:
        processor._move_sl({"position_id": position.position_id, "expected_order_id": SL_ORDER_ID,
                            "target_price": 83000.0})
    except Exception:  # noqa: BLE001 - le résultat incertain est signalé ; l'état sauvegardé compte
        pass
    sl = store.load(position.position_id).stop_loss
    assert (sl.trigger, sl.resolved_price, sl.status) == (SLTrigger.TOUCH, 83000.0, SLStatus.REPLACING)


def test_a_backup_under_binance_minimums_is_never_sent_and_noted_once(candles, rules):  # noqa: F811
    from binance_spot_manager.position_engine import recompute_position

    automation, fake, _ = candles
    position = backup_position(rules)
    entry = position.entries[0]
    entry.executed_qty = entry.net_qty = entry.binance_qty = 0.00006           # ≈ 4,69 USDT au niveau de secours
    entry.quote_spent = 0.00006 * 84000.0
    entry.commissions = []
    recompute_position(position)
    fake.klines = [kline(T0, 81000)]
    first = automation.run_cycle(position, 81500)
    second = automation.run_cycle(position, 81500)
    assert not [o for o in fake.created if o["type"] == "STOP_LOSS_LIMIT"]
    assert not first.errors and not second.errors
    assert sum("Stop de secours impossible" in a for a in first.actions + second.actions) == 1
    assert fake.kline_calls                                                    # la clôture reste surveillée


def test_a_backup_cancelled_outside_the_bot_keeps_the_candle_watch(candles, rules):  # noqa: F811
    automation, fake, clock = candles
    position = backup_position(rules)
    order = placed(automation, fake, position)
    order["status"] = "CANCELED"                                               # annulé à la main chez Binance
    result = automation.run_cycle(position, 81500)
    sl = position.stop_loss
    assert not position.automation.paused and not result.errors
    assert (sl.status, sl.backup_percent, sl.order_id) == (SLStatus.PLANNED, 0.0, None)
    fake.klines.append(kline(T0 + QUARTER, 79000))
    clock["now"] = T0 + 2 * QUARTER + MIN
    assert automation.run_cycle(position, 79500).candle_stop_hit.close_price == 79000
    assert len([o for o in fake.created if o["type"] == "STOP_LOSS_LIMIT"]) == 1   # pas reposé



# --- Contre-vérification : exécution partielle, DRY_RUN, annulation par le bot ----------------------------------


def backup_worker(settings, rules, events, tmp_path, monkeypatch, *, status, executed):  # noqa: F811
    """Position à la clôture 15m avec un secours chez Binance déjà en partie (ou entièrement) exécuté."""
    position = backup_position(rules)
    worker, fake, sent = worker_for(position, settings, rules, events, tmp_path, monkeypatch)
    fake.seed_stop_loss(qty=0.006, stop=BACKUP)
    order = fake.orders[SL_CLIENT_ID]
    order.update(status=status, executedQty=str(executed), cummulativeQuoteQty=str(executed * 78000.0))
    sl = position.stop_loss
    sl.status, sl.order_id, sl.client_order_id, sl.quantity = SLStatus.ACTIVE, SL_ORDER_ID, SL_CLIENT_ID, 0.006
    worker.positions.save(position)
    return position, worker, fake


def test_a_partly_filled_backup_lets_the_candle_exit_sell_the_rest(settings, rules, events, tmp_path, monkeypatch):  # noqa: F811
    position, worker, fake = backup_worker(settings, rules, events, tmp_path, monkeypatch,
                                           status="PARTIALLY_FILLED", executed=0.002)
    try:
        worker._process_position(position, 77500)                            # bougie 15m close à 79 000
    except RuntimeError:
        pass                                                                   # « SL partiellement exécuté » signalé
    assert [o["qty"] for o in fake.created if o["type"] == "MARKET"] == pytest.approx([0.004])
    assert not position.is_open


def test_a_backup_filled_on_a_smaller_quantity_lets_the_candle_exit_sell_the_rest(settings, rules, events, tmp_path,
                                                                                 monkeypatch):  # noqa: F811
    position, worker, fake = backup_worker(settings, rules, events, tmp_path, monkeypatch,
                                           status="FILLED", executed=0.004)
    try:
        worker._process_position(position, 77500)
    except RuntimeError:
        pass
    assert [o["qty"] for o in fake.created if o["type"] == "MARKET"] == pytest.approx([0.002])
    assert not position.is_open


def test_in_dry_run_a_breach_candle_is_announced_once(settings, rules, events):  # noqa: F811
    from binance_spot_manager.config import RunMode
    from test_automation import build_automation, build_execution
    from test_candle_stop import CandleClient

    fake = CandleClient(rules)
    dry = settings.model_copy(update={"run_mode": RunMode.DRY_RUN})
    automation = build_automation(build_execution(dry, rules, events, fake), rules, events)
    automation.clock = lambda: (T0 + 2 * QUARTER + MIN) / 1000
    assert automation.execution.settings.dry_run
    position = backup_position(rules, percent=0.0)
    fake.klines = [kline(T0, 81000), kline(T0 + QUARTER, 79000)]
    assert automation.run_cycle(position, 79500).candle_stop_hit is not None
    assert automation.run_cycle(position, 79500).candle_stop_hit is None           # annoncée une seule fois


def test_the_bot_s_own_uncertain_cancel_is_not_taken_for_a_manual_one(candles, rules):  # noqa: F811
    automation, fake, _ = candles
    position = backup_position(rules)
    order = placed(automation, fake, position)
    sl = position.stop_loss
    sl.cancel_pending = True                                                   # annulation du bot restée incertaine
    order["status"] = "CANCELED"
    result = automation.run_cycle(position, 81500)
    assert sl.backup_percent == 3.0 and sl.status is SLStatus.FAILED and not sl.cancel_pending
    assert not result.errors and not position.automation.paused
    automation.run_cycle(position, 81500)
    assert live_stops(fake) == [pytest.approx(BACKUP)]                          # secours reposé



def test_a_stop_still_alive_clears_the_pending_cancel(candles, rules):  # noqa: F811
    """Annulation du bot refusée (ordre toujours vivant) : une annulation vue plus tard est un geste manuel."""
    automation, fake, _ = candles
    position = backup_position(rules)
    order = placed(automation, fake, position)
    position.stop_loss.cancel_pending = True
    automation.run_cycle(position, 81500)                                       # l'ordre est encore NEW
    assert position.stop_loss.cancel_pending is False
    order["status"] = "CANCELED"
    automation.run_cycle(position, 81500)
    assert position.stop_loss.backup_percent == 0.0                              # geste manuel respecté


def test_a_creation_that_never_reached_binance_forgets_its_phantom_id(candles, rules, monkeypatch):  # noqa: F811
    automation, fake, _ = candles
    position = backup_position(rules)
    sl = position.stop_loss
    sl.status, sl.client_order_id = SLStatus.REPLACING, "BSM-FANTOME-SL"
    monkeypatch.setattr(automation, "_fetch_uncertain_sl", lambda p: (None, True))
    monkeypatch.setattr(automation, "_sl_intent_expired", lambda p: True)
    fake.klines = [kline(T0, 81000)]
    automation.run_cycle(position, 81500)
    assert sl.status in {SLStatus.FAILED, SLStatus.ACTIVE} and sl.client_order_id != "BSM-FANTOME-SL"
