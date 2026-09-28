"""Test volontaire avec vrais ordres Demo : achat d'environ 15 USDT puis suivi OCO.

Sans --execute : aucune ecriture. Une absence d'execution laisse l'OCO de test
actif et affiche ses identifiants ; aucun ordre preexistant n'est touche.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.binance_client import BinanceSpotClient, BinanceError
from binance_spot_manager.bot_process_manager import BotProcessManager
from binance_spot_manager.config import SecurityError, get_settings
from binance_spot_manager.execution_engine import ExecutionEngine, build_client_order_id
from binance_spot_manager.models import (
    Entry, OcoExit, OrderType, Position, PositionStatus, SLStatus, TakeProfit, TPStatus,
)
from binance_spot_manager.oco_preview import preview_oco_sell
from binance_spot_manager.position_engine import PositionEngine
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.symbol_rules import SymbolRulesCache


def report(stage, **fields):
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=True), flush=True)


def confirm_single_oco_fill(tp, sl):
    """Une seule vente remplie, branche opposee terminee sans execution."""
    filled = [order for order in (tp, sl) if order["status"] == "FILLED"]
    if len(filled) != 1:
        raise RuntimeError("Une seule branche FILLED attendue")
    other = sl if tp["status"] == "FILLED" else tp
    if (other["status"] not in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}
            or float(other["executedQty"]) != 0):
        raise RuntimeError("Branche opposee encore ouverte ou partiellement executee")


def run(resume_id=None):
    settings = get_settings()
    if settings.dry_run:
        raise SecurityError("Ce test exige des ordres Demo reels, hors DRY_RUN")
    settings.assert_write_allowed("test cycle OCO Demo")
    if not settings.has_credentials or not BotProcessManager(settings).is_running():
        raise RuntimeError("Cles Demo et worker actif requis")
    client = BinanceSpotClient(settings)
    store = PositionStore()
    symbol = "ETHUSDT"
    if (not resume_id and any(p.symbol == symbol for p in store.list_open())) or client.get_open_orders(symbol):
        raise RuntimeError("ETHUSDT deja utilise : aucun test lance")
    original_orders = {o["orderId"]: o for o in client.get_open_orders()}
    if not resume_id and client.get_balances().get("USDT", {}).get("free", 0) < 15:
        raise RuntimeError("Solde Demo insuffisant")
    cache = SymbolRulesCache(client)
    rules = cache.get(symbol)
    price = client.get_price(symbol)
    qty = rules.round_qty(14.5 / price, market=True)
    blockers = rules.check_qty(qty) + rules.check_notional(price, qty)
    if blockers:
        raise RuntimeError("Achat de test invalide : " + "; ".join(blockers))
    execution = ExecutionEngine(client, cache, settings=settings)
    if resume_id:
        position = store.load(resume_id)
        if (position is None or position.symbol != symbol or "demo-e2e-test" not in position.tags
                or position.planned_capital > 15 or position.oco_exit is not None
                or position.metrics.net_qty <= 0 or position.status is PositionStatus.CLOSED):
            raise RuntimeError("Reprise refusee : position de test non eligible")
        for existing_entry in position.entries:
            confirmed = client.get_order(symbol, order_id=existing_entry.order_id)
            if confirmed["status"] != "FILLED":
                raise RuntimeError("Achat de test non confirme")
        report("RESUME_EXISTING_BUY", position_id=resume_id)
    else:
        position = Position(
            symbol=symbol, base_asset=rules.base_asset, quote_asset=rules.quote_asset,
            creation_price=price, planned_capital=15, tags=["demo-e2e-test"],
        )
        position.automation.paused = True
        entry = Entry(order_type=OrderType.MARKET, binance_qty=float(qty), quote_amount=15)
        entry.client_order_id = build_client_order_id(
            symbol=symbol, position_id=position.position_id, suffix="E1",
        )
        position.entries.append(entry)
        store.save(position)
        report("BUY_REQUEST", position_id=position.position_id, symbol=symbol, quantity=float(qty))
        result = execution.place_entry(position, entry, current_price=price)
        store.save(position)
        if not result.success or not result.is_filled:
            raise RuntimeError(f"Achat non confirme : {result.status}. Ne pas relancer a l'aveugle.")
        engine = PositionEngine(rules)
        engine.apply_entry_fill(
            position, entry.entry_id, executed_qty=result.executed_qty,
            average_price=result.average_price, quote_spent=result.cummulative_quote_qty,
            commissions=result.commissions, order_id=result.order_id,
        )
        report("BUY_FILLED", order_id=result.order_id, spent=result.cummulative_quote_qty,
               gross_qty=result.executed_qty, net_qty=position.metrics.net_qty)
    position.status = PositionStatus.ACTIVE
    current = client.get_price(symbol)
    position.take_profits = [TakeProfit(target_price=current * 1.0001, sell_percent=100)]
    position.stop_loss.resolved_price = current * 0.999
    preview = preview_oco_sell(position, rules, current)
    if not preview.eligible:
        store.save(position)
        raise RuntimeError("Achat conserve en Demo, OCO bloque : " + "; ".join(preview.blockers))
    list_id = build_client_order_id(symbol=symbol, position_id=position.position_id, suffix="OCO1")
    tp_id = build_client_order_id(symbol=symbol, position_id=position.position_id, suffix="OCOTP1")
    sl_id = build_client_order_id(symbol=symbol, position_id=position.position_id, suffix="OCOSL1")
    # Sauvegarder les references recuperables avant le POST ; position en pause.
    position.take_profits[0].client_order_id = tp_id
    position.stop_loss.client_order_id = sl_id
    store.save(position)
    params = preview.request_params | {
        "listClientOrderId": list_id, "aboveClientOrderId": tp_id, "belowClientOrderId": sl_id,
    }
    try:
        response = client.create_oco_sell(params, experimental_confirmation=True)
    except BinanceError:
        # Lecture de recuperation uniquement, jamais un second POST OCO.
        response = client.get_order_list(list_client_order_id=list_id)
    orders = response.get("orders", [])
    tp_order = next(o for o in orders if o.get("clientOrderId") == tp_id)
    sl_order = next(o for o in orders if o.get("clientOrderId") == sl_id)
    position.oco_exit = OcoExit(
        order_list_id=response["orderListId"], list_client_order_id=list_id,
        tp_order_id=tp_order["orderId"], sl_order_id=sl_order["orderId"],
        quantity=float(preview.quantity),
    )
    position.take_profits[0].order_id = tp_order["orderId"]
    position.take_profits[0].estimated_qty = float(preview.quantity)
    position.take_profits[0].status = TPStatus.SUBMITTED
    position.stop_loss.order_id = sl_order["orderId"]
    position.stop_loss.status = SLStatus.ACTIVE
    position.stop_loss.quantity = float(preview.quantity)
    store.save(position)
    report("OCO_CREATED", position_id=position.position_id, list_id=response["orderListId"],
           tp_order_id=tp_order["orderId"], sl_order_id=sl_order["orderId"],
           quantity=preview.quantity, tp=preview.take_profit_price, sl=preview.stop_price)
    deadline = time.monotonic() + 120
    filled = False
    while time.monotonic() < deadline:
        tp = client.get_order(symbol, order_id=tp_order["orderId"])
        sl = client.get_order(symbol, order_id=sl_order["orderId"])
        if tp["status"] == "FILLED" or sl["status"] == "FILLED":
            filled = True
            local = store.load(position.position_id)
            report("EXECUTION", tp_status=tp["status"], sl_status=sl["status"],
                   local_status=local.status.value, local_oco=local.oco_exit.status)
            if local.status is PositionStatus.CLOSED:
                confirm_single_oco_fill(tp, sl)
                report("WORKER_CONFIRMED", position_id=position.position_id,
                       reason=local.close_reason.value, realized_pnl=local.pnl.realized)
                break
        time.sleep(2)
    else:
        report("TIMEOUT", exchange_fill_seen=filled, position_id=position.position_id,
               message="Test non complet. Aucun ordre existant annule ; verifier la position de test.")
    # Lecture finale des ordres qui existaient avant le test : aucune mutation.
    for order_id, old in original_orders.items():
        actual = client.get_order(old["symbol"], order_id=order_id)
        report("EXISTING_ORDER", symbol=old["symbol"], order_id=order_id,
               status=actual["status"], quantity=actual["origQty"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume-position", help="Reprendre seulement un achat de test deja confirme")
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute requis : ce test place des ordres Binance Demo")
    run(args.resume_position)
