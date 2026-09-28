"""Explicit Demo liquidation: cancel, confirm, then sell only this strategy's balance.

Persist the intent BEFORE posting. Recovery only queries; it never resends a sell.
"""

from .execution_engine import build_client_order_id
from .models import (CloseReason, EntryStatus, EventType, ManualExit,
                     PositionStatus, SLStatus, TPStatus)
from .position_engine import finish_position, recompute_position

TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


def apply_sale(position, sale, result, rules):
    if result.executed_qty < sale.executed_qty:
        raise RuntimeError("Execution cumulative incoherente ; cloture suspendue")
    sale.order_id = result.order_id or sale.order_id
    sale.status = result.status
    sale.executed_qty = result.executed_qty
    sale.quote_received = result.cummulative_quote_qty
    sale.average_fill_price = result.average_price
    sale.commissions = result.commissions
    recompute_position(position)
    if result.status == "FILLED" and float(rules.round_qty(position.metrics.net_qty, market=True)) == 0:
        finish_position(position, CloseReason.MANUAL_CLOSE)
    elif result.status in TERMINAL:
        position.status = PositionStatus.ACTIVE
        position.log(EventType.ERROR, "Cloture incomplete : position en pause, solde restant a verifier")


def poll_market_close(position, execution):
    """Return True when normal automation must remain suspended."""
    if position.status != PositionStatus.CLOSING:
        return False
    for sale in position.manual_exits:
        if sale.status not in TERMINAL:
            result = execution.fetch_order_status(position.symbol, order_id=sale.order_id,
                                                  client_order_id=None if sale.order_id else sale.client_order_id)
            if result is not None:
                apply_sale(position, sale, result, execution.rules(position.symbol))
    return True


def close_market(position, execution, positions):
    if execution._dry_run():
        return {"message": "DRY_RUN : aucune annulation ni vente effectuee"}
    if any(sale.status not in TERMINAL for sale in position.manual_exits):
        raise RuntimeError("Vente precedente non resolue : aucun nouvel ordre envoye")
    if position.stop_loss.status == SLStatus.REPLACING:
        raise RuntimeError("Remplacement SL non resolu : verifier les ordres avant cloture")
    execution.settings.assert_write_allowed("market close")
    position.automation.paused = True
    position.status = PositionStatus.CLOSING
    positions.save(position)

    targets = [(entry, "entry") for entry in position.open_entries]
    targets += [(tp, "tp") for tp in position.take_profits
                if tp.status in {TPStatus.SUBMITTED, TPStatus.PARTIALLY_EXECUTED}
                or (tp.status == TPStatus.FAILED and (tp.order_id or tp.client_order_id))]
    if position.stop_loss.status == SLStatus.ACTIVE or (position.stop_loss.status == SLStatus.FAILED
                                                       and (position.stop_loss.order_id or position.stop_loss.client_order_id)):
        targets.append((position.stop_loss, "sl"))
    # OCO identifiers remain authoritative even if a local branch status is stale.
    if position.oco_exit:
        oco = position.oco_exit
        for order_id, kind in ((oco.tp_order_id, "tp"), (oco.sl_order_id, "sl")):
            item = (next((tp for tp in position.take_profits if tp.order_id == order_id), None)
                    if kind == "tp" else position.stop_loss)
            if item is None or item.order_id != order_id:
                raise RuntimeError("Branche OCO non rattachee : reconciliation requise")
            if not any(target is item for target, _ in targets):
                targets.append((item, kind))
    for item, kind in targets:
        if not item.order_id and not item.client_order_id:
            raise RuntimeError("Ordre sans identifiant : annulation non verifiable")
        kwargs = {"order_id": item.order_id} if item.order_id else {"client_order_id": item.client_order_id}
        result = execution.fetch_order_status(position.symbol, **kwargs)
        if result is None:
            raise RuntimeError("Ordre introuvable : aucune vente envoyee")
        if result.status not in TERMINAL:
            execution.cancel_order(position.symbol, **kwargs)
            result = execution.fetch_order_status(position.symbol, **kwargs)
        if result is None or result.status not in TERMINAL:
            raise RuntimeError("Annulation non confirmee : aucune vente envoyee")
        if result.executed_qty < item.executed_qty:
            raise RuntimeError("Execution incoherente : reconciliation requise")
        item.executed_qty = result.executed_qty
        item.average_fill_price = result.average_price
        item.commissions = result.commissions
        if kind == "entry":
            item.quote_spent = result.cummulative_quote_qty
            item.status = EntryStatus.FILLED if result.status == "FILLED" else EntryStatus.CANCELED
        else:
            item.quote_received = result.cummulative_quote_qty
            item.status = (TPStatus.EXECUTED if result.status == "FILLED" else TPStatus.CANCELED) if kind == "tp" else (
                SLStatus.EXECUTED if result.status == "FILLED" else SLStatus.CANCELED)
        positions.save(position)
    for entry in position.entries:
        if entry.status == EntryStatus.PLANNED:
            entry.status = EntryStatus.CANCELED
    for tp in position.take_profits:
        if tp.status in {TPStatus.PENDING, TPStatus.TRIGGERED}:
            tp.status = TPStatus.CANCELED
    if position.stop_loss.status == SLStatus.PLANNED:
        position.stop_loss.status = SLStatus.CANCELED
    if position.oco_exit:
        position.oco_exit.status = "CANCELED"
    recompute_position(position)
    positions.save(position)
    if position.metrics.net_qty <= 1e-12:
        finish_position(position, CloseReason.MANUAL_CLOSE)
        positions.save(position)
        return {"message": "Ordres annules ; aucun solde restant a vendre"}
    rules = execution.rules(position.symbol, refresh=True)
    qty = float(rules.round_qty(position.metrics.net_qty, market=True))
    price = execution.client.get_price(position.symbol)
    errors = rules.validate_order(price, qty, market=True)
    if errors:
        raise RuntimeError("Position en pause, ordres annules, vente impossible : " + "; ".join(errors))
    if execution.get_free_balance(position.base_asset) + 1e-12 < qty:
        raise RuntimeError("Solde libre insuffisant : aucune vente envoyee, position en pause")
    sale = ManualExit(client_order_id=build_client_order_id(
        symbol=position.symbol, position_id=position.position_id,
        suffix=f"MC{len(position.manual_exits) + 1}", identity_version=2), requested_qty=qty)
    position.manual_exits.append(sale)
    positions.save(position)
    result = execution._create_order_safe(symbol=position.symbol, side="SELL", order_type="MARKET",
                                         quantity=qty, price=None, client_order_id=sale.client_order_id, market=True)
    apply_sale(position, sale, result, rules)
    positions.save(position)
    if not result.success or result.status not in TERMINAL:
        raise RuntimeError(result.error or "Vente en attente de confirmation ; aucun renvoi automatique")
    return {"message": "Vente au marche confirmee" if not position.is_open else "Cloture incomplete : position en pause",
            "order_id": sale.order_id, "quantity": sale.executed_qty,
            "realized_pnl": position.pnl.realized, "quote_asset": position.quote_asset,
            "remaining_qty": position.metrics.net_qty}
