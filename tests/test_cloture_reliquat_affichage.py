"""Retours du 2026-10-05 : achat vu par la réconciliation → position ACTIVE ; reliquat sous les minimums Binance
→ position terminée au lieu de rester en CLOSING ; prix affichés avec 4 à 5 décimales ; le rapport Telegram et le
Dashboard donnent les mêmes chiffres pour les mêmes questions. Aucun ordre réel, aucun réseau."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from binance_spot_manager.daily_report import build, results
from binance_spot_manager.execution_engine import OrderResult
from binance_spot_manager.market_close import apply_sale, close_market, poll_market_close
from binance_spot_manager.models import (CloseReason, EntryStatus, ManualExit, PositionStatus, SLStatus,
                                         TPStatus)
from binance_spot_manager.notification_engine import NotificationEngine
from binance_spot_manager.pnl_display import format_price
from binance_spot_manager.position_engine import recompute_position
from test_automation import make_position, rules, settings  # noqa: F401 - fixtures partagees
from test_entry_recovery import order, recovery  # noqa: F401
from test_market_close import setup  # noqa: F401
from test_suivi_canaux_protection import NOW, winning_position

DUST = .000049                      # 0,000049 BTC × 84 000 ≈ 4,12 USDT < 5 (minNotional), mais ≥ minQty


def with_dust(position):
    """Le TP a vendu presque tout : il reste un reliquat sous minNotional (cas FETUSDT : 0,1 FET ≈ 0,03 USDT)."""
    tp = position.take_profits[0]
    tp.status, tp.executed_qty, tp.average_fill_price = TPStatus.EXECUTED, .00095, 86000
    tp.quote_received = .00095 * 86000
    recompute_position(position)
    assert position.metrics.net_qty == pytest.approx(DUST)
    return position


# -- achat vu par la réconciliation -------------------------------------------------------------------------------

@pytest.mark.parametrize("status,quantity", [("PARTIALLY_FILLED", 0.4), ("FILLED", 1.0)])
def test_a_purchase_seen_by_the_reconciliation_makes_the_position_active(recovery, status, quantity):  # noqa: F811
    engine, state, position, store = recovery
    state["order"] = order(status, quantity)
    engine.reconcile(position)
    assert position.status is PositionStatus.ACTIVE                       # avant : PENDING_ENTRIES (XNOUSDT)
    store.save(position)
    assert store.load(position.position_id).status is PositionStatus.ACTIVE


def test_a_position_left_pending_with_a_purchase_heals_at_the_next_cycle(rules):  # noqa: F811
    stuck = make_position(rules)
    stuck.status = PositionStatus.PENDING_ENTRIES                        # état enregistré par l'ancienne version
    recompute_position(stuck)
    assert stuck.status is PositionStatus.ACTIVE
    waiting = make_position(rules, entry_status=EntryStatus.SUBMITTED)
    waiting.entries[0].executed_qty = 0.0
    waiting.status = PositionStatus.PENDING_ENTRIES
    recompute_position(waiting)
    assert waiting.status is PositionStatus.PENDING_ENTRIES               # rien d'acheté : rien ne change


# -- reliquat sous les minimums ----------------------------------------------------------------------------------

def test_minimums_rule(setup):  # noqa: F811
    filters = setup[3].rules()
    assert filters.below_minimums(DUST, 84000, market=True)              # ≈ 4,12 USDT < 5
    assert filters.below_minimums(.000009, 84000, market=True)           # nul une fois arrondi au stepSize
    assert not filters.below_minimums(.0001, 84000, market=True)         # 8,4 USDT : vendable
    assert not filters.below_minimums(DUST, None, market=True)           # prix inconnu : aucune supposition


@pytest.mark.parametrize("reason", [CloseReason.MANUAL_CLOSE, CloseReason.STOP_CROSSED])
def test_closing_a_dust_remainder_finishes_the_position_without_selling(setup, reason):  # noqa: F811
    position, _, store, execution, orders, calls = setup
    with_dust(position)
    result = close_market(position, execution, store, reason=reason)
    assert calls == [("cancel", 3)]                                       # le SL est annulé, aucune vente
    assert not position.is_open and position.close_reason is reason
    assert "sous les minimums Binance" in result["message"] and position.manual_exits == []
    assert store.load(position.position_id).status is PositionStatus.CLOSED


def test_a_position_stuck_in_closing_with_dust_is_finished_by_the_worker(setup):  # noqa: F811
    """Cas FETUSDT du 2026-10-05 : l'ancienne version a tout annulé puis refusé la vente (« vente impossible ») ;
    la position restait en CLOSING, en pause, pour toujours."""
    position, _, store, execution, _, calls = setup
    with_dust(position)
    position.stop_loss.status = SLStatus.CANCELED
    position.status, position.automation.paused = PositionStatus.CLOSING, True
    position.metrics.current_price = 0.0
    assert poll_market_close(position, execution)                          # prix du worker inconnu : prix moyen
    assert not position.is_open and position.close_reason is CloseReason.ALL_TP_HIT
    assert calls == []


def test_a_stuck_closing_is_left_alone_while_an_order_may_live_or_the_rest_is_sellable(setup):  # noqa: F811
    position, _, store, execution, _, calls = setup
    with_dust(position)
    position.status = PositionStatus.CLOSING                              # SL encore ACTIVE chez Binance
    assert poll_market_close(position, execution, 84000) and position.status is PositionStatus.CLOSING
    position.stop_loss.status = SLStatus.CANCELED
    position.manual_exits.append(ManualExit(client_order_id="MC1", order_id=99, requested_qty=DUST))
    assert poll_market_close(position, execution, 84000) and position.status is PositionStatus.CLOSING
    position.manual_exits[0].status = "CANCELED"                          # vente sans réponse : jamais oubliée
    position.take_profits[0].executed_qty = .0005                         # reste ≈ 41,9 USDT : vendable
    recompute_position(position)
    assert poll_market_close(position, execution, 84000) and position.status is PositionStatus.CLOSING
    assert calls == []


def test_a_filled_sale_that_leaves_dust_finishes_instead_of_pausing(setup):  # noqa: F811
    position, _, store, execution, _, _ = setup
    position.status = PositionStatus.CLOSING
    sale = ManualExit(client_order_id="MC1", requested_qty=.00095)
    position.manual_exits.append(sale)
    result = OrderResult(success=True, order_id=4, status="FILLED", executed_qty=.00095,
                         average_price=84000, cummulative_quote_qty=.00095 * 84000)
    apply_sale(position, sale, result, execution.rules())
    assert position.metrics.net_qty == pytest.approx(DUST)
    assert not position.is_open and position.close_reason is CloseReason.MANUAL_CLOSE   # avant : ACTIVE + erreur


# -- prix ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,text", [
    (0.3712, "0.37120"), (0.25, "0.25000"), (0.0034123, "0.0034123"), (0.00001234, "0.00001234"),
    (3.4567, "3.4567"), (152.34, "152.3400"), (84000, "84 000.00"), (0, "0.00"),
])
def test_prices_show_enough_decimals(value, text):
    assert format_price(value) == text


def test_ui_prices_and_amounts():
    import ui_common

    assert ui_common.fmt_price(0.3712) == "0.37120" and ui_common.fmt_price(None) == "—"
    assert ui_common.fmt_price(0.3712, 2) == "0.37"                       # décimales imposées : inchangé
    assert ui_common.fmt_amount(2537.164) == "2 537.16" and ui_common.fmt_amount(200.5812) == "200.58"
    assert ui_common.colored_pnl(6.0412) == ":green[6.04]"                # un PnL reste un montant


def test_the_entry_notification_shows_a_small_average_price(settings, rules):  # noqa: F811
    position = make_position(rules)
    position.metrics.average_price = 0.36294
    notice = NotificationEngine(settings).entry_filled(position, position.entries[0])
    assert "Prix moyen position : 0.36294" in notice.body


# -- rapport Telegram et Dashboard ---------------------------------------------------------------------------------

def test_the_report_gives_the_dashboard_figures_and_the_dashboard_the_report_figures(setup, rules,  # noqa: F811
                                                                                    monkeypatch):
    from binance_spot_manager.dashboard_service import DashboardService

    position, sibling, store, execution, _, _ = setup
    store.delete(sibling.position_id)
    old = winning_position(rules)
    old.closed_at = NOW - timedelta(days=10)                              # hors des 7 jours, compté depuis le début
    today = winning_position(rules)
    today.closed_at = NOW - timedelta(hours=1)
    partial = with_dust(position)                                         # TP déjà vendu, position encore ouverte
    for item in (old, today, partial):
        store.save(item)
    everything = store.list_all()
    figures = results(everything, NOW)
    assert figures.realized_open == pytest.approx(partial.pnl.realized) and figures.realized_open > 0
    assert figures.realized_all == pytest.approx(old.pnl.realized + today.pnl.realized + partial.pnl.realized)
    _, body = build(everything, NOW, lambda p: {})
    assert f"Aujourd'hui (depuis 00:00 UTC) : {today.pnl.realized:+.2f} USDT sur 1 position(s)" in body
    assert f"Depuis le début : réalisé {figures.realized_all:+.2f} USDT, dont {partial.pnl.realized:+.2f}" in body

    monkeypatch.setattr("binance_spot_manager.dashboard_service.utcnow", lambda: NOW)
    view = DashboardService(settings=execution.settings, position_store=store,
                            client=SimpleNamespace()).portfolio(live=False)
    assert view.realized_pnl == pytest.approx(figures.realized_all)       # « PnL réalisé (depuis le début) »
    assert view.realized_on_open == pytest.approx(partial.pnl.realized)
    assert (view.closed_today, view.closed_week) == (1, 1)                # les positions terminées du rapport
    assert view.realized_today == view.realized_week == pytest.approx(today.pnl.realized)
