"""Migration explicite d'une position Demo à un unique OCO de sortie.

Exécution volontairement séparée du Dashboard. Ne jamais l'utiliser sur Live.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.binance_client import BinanceError, BinanceSpotClient  # noqa: E402
from binance_spot_manager.bot_process_manager import BotProcessManager  # noqa: E402
from binance_spot_manager.config import Environment, RunMode, get_settings  # noqa: E402
from binance_spot_manager.execution_engine import ExecutionEngine, build_client_order_id  # noqa: E402
from binance_spot_manager.models import OcoExit, SLStatus, SyncStatus, TPStatus  # noqa: E402
from binance_spot_manager.oco_preview import preview_oco_sell  # noqa: E402
from binance_spot_manager.position_store import PositionStore  # noqa: E402
from binance_spot_manager.symbol_rules import SymbolRulesCache  # noqa: E402


def _confirmed_order_list(response, above_client_id, below_client_id):
    orders = response.get("orders") or []
    if len(orders) != 2:
        raise RuntimeError("OCO créé, mais impossible d'identifier ses deux ordres")
    above = next((item for item in orders if item.get("clientOrderId") == above_client_id), None)
    below = next((item for item in orders if item.get("clientOrderId") == below_client_id), None)
    if above is None or below is None:
        raise RuntimeError("OCO créé, mais identifiants TP/SL inattendus")
    return above, below


def migrate(position_id: str) -> None:
    settings = get_settings()
    if settings.environment is not Environment.DEMO or settings.run_mode not in {
        RunMode.DEMO_MANUAL, RunMode.DEMO_AUTO,
    }:
        raise RuntimeError("Migration OCO réservée à Binance Demo, hors DRY_RUN")
    settings.assert_write_allowed("migration OCO Demo")

    client = BinanceSpotClient(settings)
    store = PositionStore()
    position = store.load(position_id)
    if position is None or position.oco_exit is not None:
        raise RuntimeError("Position absente ou déjà migrée vers OCO")
    rules_cache = SymbolRulesCache(client)
    rules = rules_cache.get(position.symbol)
    preview = preview_oco_sell(position, rules, client.get_price(position.symbol))
    blockers = [b for b in preview.blockers if "SL indépendant déjà actif" not in b]
    if blockers or position.stop_loss.status is not SLStatus.ACTIVE or not position.stop_loss.order_id:
        raise RuntimeError("Migration refusée : " + "; ".join(blockers or ["SL actif absent"]))

    old_id = position.stop_loss.order_id
    old_order = client.get_order(position.symbol, order_id=old_id)
    if old_order.get("status") != "NEW" or float(old_order.get("executedQty") or 0) > 0:
        raise RuntimeError("SL existant modifié ou exécuté : migration refusée")
    if float(old_order.get("origQty") or 0) != float(preview.quantity):
        raise RuntimeError("Quantité du SL différente de l'OCO prévu")
    other_sells = [
        o for o in client.get_open_orders(position.symbol)
        if o.get("side") == "SELL" and o.get("orderId") != old_id
    ]
    if other_sells:
        raise RuntimeError("Autre ordre de vente ouvert : migration refusée")

    manager = BotProcessManager(settings)
    if manager.is_running():
        stopped, message = manager.stop(timeout_seconds=20)
        if not stopped:
            raise RuntimeError(f"Worker non arrêté : {message}")
    print("Worker arrêté ; vérification finale avant annulation")
    position = store.load(position_id)
    if position is None or position.stop_loss.order_id != old_id:
        raise RuntimeError("Position modifiée pendant l'arrêt : migration abandonnée")
    preview = preview_oco_sell(position, rules, client.get_price(position.symbol))
    blockers = [b for b in preview.blockers if "SL indépendant déjà actif" not in b]
    if blockers:
        manager.start()
        raise RuntimeError("Prix/position modifiés : " + "; ".join(blockers))

    cancelled = client.cancel_order(position.symbol, order_id=old_id)
    if cancelled.get("status") != "CANCELED" or float(cancelled.get("executedQty") or 0) > 0:
        raise RuntimeError("Annulation du SL non confirmée ; worker laissé arrêté")
    position.stop_loss.status = SLStatus.CANCELED
    position.stop_loss.order_id = None
    position.automation.paused = True
    position.sync_status = SyncStatus.DESYNC_DETECTED
    store.save(position)
    print("SL annulé ; création immédiate de l'OCO Demo")

    list_id = build_client_order_id(
        symbol=position.symbol, position_id=position_id, suffix="OCO1"
    )
    above_client_id = build_client_order_id(
        symbol=position.symbol, position_id=position_id, suffix="OCOTP1"
    )
    below_client_id = build_client_order_id(
        symbol=position.symbol, position_id=position_id, suffix="OCOSL1"
    )
    params = preview.request_params | {
        "listClientOrderId": list_id,
        "aboveClientOrderId": above_client_id,
        "belowClientOrderId": below_client_id,
    }
    response = None
    try:
        response = client.create_oco_sell(params, experimental_confirmation=True)
    except BinanceError as exc:
        # Un timeout/5xx ne prouve pas que l'ordre a échoué : interroger Binance.
        try:
            response = client.get_order_list(list_client_order_id=list_id)
        except BinanceError as lookup_exc:
            opened = client.get_open_orders(position.symbol)
            oco_orders = [
                item for item in opened
                if item.get("clientOrderId") in {above_client_id, below_client_id}
            ]
            if len(oco_orders) == 2:
                response = {
                    "orderListId": oco_orders[0]["orderListId"],
                    "orders": oco_orders,
                }
            elif oco_orders or not (
                exc.status is not None and 400 <= exc.status < 500
                and exc.code is not None
                and lookup_exc.code in {-2011, -2013, -2022}
            ):
                raise RuntimeError(
                    "Résultat OCO inconnu : ne pas recréer le SL automatiquement"
                ) from lookup_exc
            else:
                print(f"OCO non accepté : {exc}")

    if response is None:
        execution = ExecutionEngine(client, rules_cache, settings=settings)
        position.automation.paused = False
        position.stop_loss.replace_count += 1
        restored = execution.place_stop_loss(
            position, stop_price=position.stop_loss.resolved_price,
            quantity=position.metrics.net_qty, attempt=position.stop_loss.replace_count,
        )
        store.save(position)
        if restored.success:
            manager.start()
            raise RuntimeError("OCO refusé ; SL restauré et worker relancé")
        raise RuntimeError(f"OCO refusé ET SL non restauré : {restored.error}")

    # Dès ici un OCO peut exister ; ne jamais restaurer un second SL à l'aveugle.
    above, below = _confirmed_order_list(response, above_client_id, below_client_id)
    position.oco_exit = OcoExit(
        order_list_id=int(response["orderListId"]),
        list_client_order_id=list_id,
        tp_order_id=int(above["orderId"]),
        sl_order_id=int(below["orderId"]),
        quantity=float(preview.quantity),
    )
    position.take_profits[0].order_id = int(above["orderId"])
    position.take_profits[0].status = TPStatus.SUBMITTED
    position.stop_loss.order_id = int(below["orderId"])
    position.stop_loss.status = SLStatus.ACTIVE
    position.stop_loss.quantity = float(preview.quantity)
    position.automation.paused = False
    position.sync_status = SyncStatus.SYNCED
    store.save(position)
    started, message = manager.start()
    print(
        f"OCO accepté : liste {position.oco_exit.order_list_id}, "
        f"TP {position.oco_exit.tp_order_id}, SL {position.oco_exit.sl_order_id}"
    )
    print(f"Worker : {message}")
    if not started:
        raise RuntimeError("OCO actif, mais worker non relancé : vérifier le Dashboard")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--position-id", required=True)
    parser.add_argument("--execute", action="store_true", help="autorise les écritures Demo")
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute requis pour la migration Demo")
    migrate(args.position_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
