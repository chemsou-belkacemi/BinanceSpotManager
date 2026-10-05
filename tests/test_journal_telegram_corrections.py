"""Corrections issues du journal Telegram du bot (export du 2026-10-05) :

- toute fin de position est annoncee avec son resultat, pertes comprises, frais BNB valorises comme
  dans History ; le compteur « TP atteints » compte les TP partiellement executes ;
- un achat execute est annonce « Achat execute », pas « Desynchronisation » ;
- « SL deplace » affiche l'ancien niveau, pas le nouveau deux fois ;
- un TP atteint avant l'execution de l'achat n'est ni une erreur ni un echec ;
- un reliquat sous les minimums Binance termine la position au lieu d'une erreur a chaque cycle.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from binance_spot_manager.models import (
    CloseReason,
    Commission,
    EntryStatus,
    PositionStatus,
    SLRuleAfterTP,
    SLStatus,
    TPStatus,
)
from binance_spot_manager.notification_engine import NotificationEngine
from binance_spot_manager.position_engine import finish_position, recompute_position
from binance_spot_manager.reconciliation_engine import Finding, ReconciliationReport
from test_automation import (  # noqa: F401 - fixtures partagees
    build_automation,
    engine,
    events,
    journaled,
    make_position,
    rules,
    settings,
)
from test_candle_stop import candle_position, worker_for


def losing_position(rules, *, fee_asset: str = "BNB"):
    """Achat 0,006 BTC a 84 000, stop execute a 80 640 : une perte, frais payes en BNB."""
    position = make_position(rules)
    position.entries[0].commissions = [Commission(asset=fee_asset, amount=0.001)]
    sl = position.stop_loss
    sl.executed_qty = 0.006
    sl.average_fill_price = 80640.0
    sl.quote_received = 0.006 * 80640.0
    sl.commissions = [Commission(asset=fee_asset, amount=0.001)]
    sl.status = SLStatus.EXECUTED
    finish_position(position, CloseReason.SL_EXECUTED)
    recompute_position(position)
    return position


def test_a_loss_is_announced_with_its_result_and_bnb_fees(rules, settings):
    position = losing_position(rules)
    before = position.pnl.realized
    notice = NotificationEngine(settings).position_finished(position, fee_rates={"BNB": 600.0})
    valued = position.model_copy(deep=True)
    recompute_position(valued, fee_rates={"BNB": 600.0})
    assert "Perte" in notice.title and f"{valued.pnl.realized:+.2f}" in notice.title
    assert "frais compris" in notice.body and "BNB au cours actuel" in notice.body
    assert valued.pnl.realized < before                       # les frais BNB sont bien deduits
    assert position.pnl.realized == before                    # la position elle-meme n'est pas modifiee


def test_a_missing_bnb_rate_is_said_not_hidden(rules, settings):
    notice = NotificationEngine(settings).position_finished(losing_position(rules), fee_rates={})
    assert "hors frais BNB : cours indisponible" in notice.body


def test_the_tp_counter_counts_partially_executed_tps(rules, settings):
    position = make_position(rules)
    tp1, tp2 = position.sorted_tps
    tp1.status, tp1.executed_qty = TPStatus.EXECUTED, 0.0036
    tp2.status, tp2.executed_qty = TPStatus.PARTIALLY_EXECUTED, 0.00235
    notice = NotificationEngine(settings).position_finished(position)
    assert "TP atteints : 2/2" in notice.body


def test_tp_gain_says_bnb_fees_are_not_deducted(rules, settings):
    position = make_position(rules)
    tp = position.sorted_tps[0]
    tp.commissions = [Commission(asset="BNB", amount=0.0001)]
    assert "(hors frais BNB)" in NotificationEngine(settings).tp_executed(position, tp).body
    tp.commissions = [Commission(asset="USDT", amount=0.1)]
    assert "(hors frais BNB)" not in NotificationEngine(settings).tp_executed(position, tp).body


def test_a_filled_purchase_is_announced_as_such_not_as_a_desync(rules, settings):
    from scripts import bot_worker

    position = make_position(rules)
    entry = position.entries[0]
    worker = bot_worker.Worker.__new__(bot_worker.Worker)
    report = ReconciliationReport(findings=[
        Finding(kind="FILL_APPLIED", severity="WARNING", message="Entry 1 : fill Binance enregistre (0.006 @ 84000)",
                target_type="ENTRY", target_id=entry.entry_id),
    ])
    worker.reconciliation = SimpleNamespace(reconcile=lambda p: report)
    worker.positions = SimpleNamespace(save=lambda p: None)
    sent = []
    worker.notifications = SimpleNamespace(
        entry_filled=lambda p, e: ("achat", e.sequence_number), desync=lambda p, m: ("desync", m),
        notify_position_event=lambda p, notice: sent.append(notice),
    )
    worker._reconcile([position])
    assert sent == [("achat", 1)]
    report.findings.append(Finding(kind="ORDER_MISSING", severity="CRITICAL", message="ordre absent"))
    sent.clear()
    worker._reconcile([position])
    assert sent == [("achat", 1), ("desync", ["ordre absent"])]
    notice = NotificationEngine(settings).entry_filled(position, entry)
    assert "Entry 1 remplie" in notice.title and "Quantite : 0.006" in notice.body


def test_a_candle_close_exit_announces_the_result(settings, rules, events, tmp_path, monkeypatch):
    position = candle_position(rules)
    worker, fake, sent = worker_for(position, settings, rules, events, tmp_path, monkeypatch)
    worker._process_position(position, 81500)
    assert not position.is_open and position.close_reason is CloseReason.SL_CANDLE_CLOSE
    assert sent.index("candle_exit") < sent.index("finished")       # sortie puis resultat


def test_sl_moved_shows_the_old_level(journaled, rules, events, tmp_path, monkeypatch):
    from binance_spot_manager.position_store import PositionStore
    from scripts import bot_worker

    execution, fake = journaled
    store = PositionStore(tmp_path / "positions")
    position = make_position(rules, tp_rules={1: SLRuleAfterTP.BREAK_EVEN})
    old = position.stop_loss.resolved_price
    store.save(position)
    monkeypatch.setattr(bot_worker, "get_settings_store", lambda: SimpleNamespace(load=lambda: {}))
    worker = bot_worker.Worker.__new__(bot_worker.Worker)
    worker.automation = build_automation(execution, rules, events)
    worker.execution, worker.client, worker.positions, worker.events = execution, fake, store, events
    moves = []
    worker.notifications = SimpleNamespace(
        tp_executed=lambda *a: "tp", sl_moved=lambda p, before, after: moves.append((before, after)) or "sl",
        position_finished=lambda *a, **k: "finished", notify_position_event=lambda p, notice: None,
    )
    worker._process_position(position, 86600)
    assert moves and moves[0][0] == pytest.approx(old) and moves[0][1] != pytest.approx(old)


def test_a_tp_reached_before_the_purchase_is_neither_an_error_nor_a_failure(engine, events):
    execution, fake, rules = engine
    position = make_position(rules)
    entry = position.entries[0]
    entry.status = EntryStatus.SUBMITTED
    entry.executed_qty = entry.net_qty = entry.binance_qty = entry.quote_spent = 0.0
    entry.average_fill_price = 0.0
    position.stop_loss.status = SLStatus.PLANNED
    position.stop_loss.order_id = None
    position.stop_loss.client_order_id = None
    position.status = PositionStatus.PENDING_ENTRIES if hasattr(PositionStatus, "PENDING_ENTRIES") else position.status
    recompute_position(position)
    automation = build_automation(execution, rules, events)
    for _ in range(3):                                         # avant : une erreur et un TP FAILED a chaque cycle
        outcome = automation.run_cycle(position, 86600.0)
        assert not [e for e in outcome.errors if "vendable" in e]
    assert position.sorted_tps[0].status is not TPStatus.FAILED
    assert not [o for o in fake.created if o["type"] == "MARKET"]


def test_an_unsellable_remainder_finishes_the_position_instead_of_erroring(engine, events):
    execution, fake, rules = engine
    position = make_position(rules)
    tp1, tp2 = position.sorted_tps
    tp1.status, tp1.executed_qty, tp1.average_fill_price = TPStatus.EXECUTED, 0.0036, 86520.0
    tp1.quote_received = 0.0036 * 86520.0
    tp2.status, tp2.executed_qty, tp2.average_fill_price = TPStatus.PARTIALLY_EXECUTED, 0.00235, 89040.0
    tp2.quote_received = 0.00235 * 89040.0
    position.stop_loss.status = SLStatus.CANCELED                     # SL annule pour la vente du TP2
    position.stop_loss.order_id = None
    recompute_position(position)
    assert position.metrics.net_qty == pytest.approx(0.00005)         # ≈ 4,4 USDT < 5 (minNotional)
    automation = build_automation(execution, rules, events)
    outcome = automation.run_cycle(position, 88000.0)
    assert not outcome.errors and outcome.position_finished
    assert position.close_reason is CloseReason.ALL_TP_HIT and not position.is_open
    assert not [o for o in fake.created if o["type"] == "STOP_LOSS_LIMIT"]
    assert any("sous les minimums Binance" in a for a in outcome.actions)


def test_restoring_the_stop_on_an_unsellable_remainder_finishes_instead_of_erroring(engine, events):
    """Cas FETUSDT du 2026-10-04 : apres un TP, le reste a proteger est sous minNotional ; avant, « SL non
    restaure apres TP : NOTIONAL » a chaque cycle (~70 erreurs)."""
    from binance_spot_manager.automation_engine import CycleResult

    execution, fake, rules = engine
    position = make_position(rules)
    tp1 = position.sorted_tps[0]
    tp1.status, tp1.executed_qty, tp1.average_fill_price = TPStatus.EXECUTED, 0.00595, 86520.0
    tp1.quote_received = 0.00595 * 86520.0
    position.stop_loss.status = SLStatus.CANCELED
    position.stop_loss.order_id = None
    position.metrics.current_price = 86600.0
    recompute_position(position)
    assert position.metrics.net_qty == pytest.approx(0.00005)
    automation = build_automation(execution, rules, events)
    result = CycleResult(position_id=position.position_id, symbol=position.symbol)
    automation._restore_stop_loss(position, result)
    assert not result.errors and result.position_finished and not position.is_open
    assert not [o for o in fake.created if o["type"] == "STOP_LOSS_LIMIT"]
