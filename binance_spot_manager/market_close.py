"""Explicit Demo liquidation: cancel, confirm, then sell only this strategy's balance.

Persist the intent BEFORE posting. Recovery only queries; it never resends a sell.

Reliquat sous les minimums Binance (minQty, minNotional) : rien a vendre ni a proteger ; la position est
terminee et le reliquat reste sur le compte (meme regle qu'apres les TP), au lieu de rester en CLOSING.
"""

from .execution_engine import build_client_order_id
from .models import (CloseReason, EntryStatus, EventType, ManualExit,
                     PositionStatus, SLStatus, TPStatus)
from .position_engine import finish_position, recompute_position

TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}


def finish_remainder(position, reason, price):
    """Termine la position en laissant sur le compte un reliquat sous les minimums Binance ; message rendu."""
    remaining = position.metrics.net_qty
    value = remaining * price if price else 0.0
    message = (f"reliquat {remaining:g} {position.base_asset} (≈ {value:.2f} {position.quote_asset}) sous les "
               "minimums Binance, laisse sur le compte : position terminee")
    position.log(EventType.POSITION_UPDATED, message[0].upper() + message[1:], remaining=remaining)
    finish_position(position, reason)
    return message


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
    if result.status not in TERMINAL:
        return
    price = result.average_price or position.metrics.current_price
    if float(rules.round_qty(position.metrics.net_qty, market=True)) == 0:
        finish_position(position, sale.close_reason)
    elif rules.below_minimums(position.metrics.net_qty, price, market=True):
        finish_remainder(position, sale.close_reason, price)
    else:
        position.status = PositionStatus.ACTIVE
        position.log(EventType.ERROR, "Cloture incomplete : position en pause, solde restant a verifier")


def _nothing_live(position):
    """Aucun ordre de cette position ne peut encore vivre chez Binance (achat, TP, SL, OCO, vente)."""
    def uncertain(item):
        return bool(item.order_id or item.client_order_id)

    sl = position.stop_loss
    return (not position.open_entries
            and not any(tp.status in {TPStatus.SUBMITTED, TPStatus.PARTIALLY_EXECUTED}
                        or (tp.status == TPStatus.FAILED and uncertain(tp)) for tp in position.take_profits)
            and not (sl.status in {SLStatus.ACTIVE, SLStatus.REPLACING} or (sl.status == SLStatus.FAILED
                                                                          and uncertain(sl)))
            and not (position.oco_exit and position.oco_exit.status not in {"CANCELED", "FILLED",
                                                                           "PARTIAL_TERMINAL"})
            and all(sale.status in TERMINAL for sale in position.manual_exits))


def _finish_stuck_remainder(position, execution, price):
    """Cloture restee en CLOSING parce que le reste etait invendable (versions precedentes de close_market) :
    terminee des que plus rien n'est vivant chez Binance et que le reste est sous les minimums. Prix de
    reference : celui du worker, sinon le dernier connu ou le prix moyen (seul l'ordre de grandeur compte)."""
    if not _nothing_live(position):
        return
    reference = price or position.metrics.current_price or position.metrics.average_price
    try:
        rules = execution.rules(position.symbol)
    except Exception:  # noqa: BLE001 - filtres illisibles : on reessaie au cycle suivant, sans erreur
        return
    if not rules.below_minimums(position.metrics.net_qty, reference, market=True):
        return
    reason = position.closing_reason or (position.manual_exits[-1].close_reason if position.manual_exits
                                         else CloseReason.ALL_TP_HIT if position.hit_tps else CloseReason.MANUAL_CLOSE)
    finish_remainder(position, reason, reference)


def poll_market_close(position, execution, price=None):
    """Return True when normal automation must remain suspended."""
    if position.status != PositionStatus.CLOSING:
        return False
    for sale in position.manual_exits:
        if sale.status not in TERMINAL:
            result = execution.fetch_order_status(position.symbol, order_id=sale.order_id,
                                                  client_order_id=None if sale.order_id else sale.client_order_id)
            if result is not None:
                apply_sale(position, sale, result, execution.rules(position.symbol))
    if position.status == PositionStatus.CLOSING:
        _finish_stuck_remainder(position, execution, price)
    return True


def close_market(position, execution, positions, *, reason=CloseReason.MANUAL_CLOSE):
    if execution._dry_run():
        return {"message": "DRY_RUN : aucune annulation ni vente effectuee"}
    if any(sale.status not in TERMINAL for sale in position.manual_exits):
        raise RuntimeError("Vente precedente non resolue : aucun nouvel ordre envoye")
    if position.stop_loss.status == SLStatus.REPLACING:
        raise RuntimeError("Remplacement SL non resolu : verifier les ordres avant cloture")
    execution.settings.assert_write_allowed("market close")
    position.automation.paused = True
    position.status = PositionStatus.CLOSING
    position.closing_reason = reason
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
        finish_position(position, reason)
        positions.save(position)
        return {"message": "Ordres annules ; aucun solde restant a vendre"}
    rules = execution.rules(position.symbol, refresh=True)
    qty = float(rules.round_qty(position.metrics.net_qty, market=True))
    price = execution.client.get_price(position.symbol)
    if rules.below_minimums(position.metrics.net_qty, price, market=True):
        message = finish_remainder(position, reason, price)
        positions.save(position)
        return {"message": "Ordres annules ; " + message, "quantity": 0.0,
                "realized_pnl": position.pnl.realized, "quote_asset": position.quote_asset,
                "remaining_qty": position.metrics.net_qty}
    errors = rules.validate_order(price, qty, market=True)
    if errors:
        raise RuntimeError("Position en pause, ordres annules, vente impossible : " + "; ".join(errors))
    if execution.get_free_balance(position.base_asset) + 1e-12 < qty:
        raise RuntimeError("Solde libre insuffisant : aucune vente envoyee, position en pause")
    sale = ManualExit(client_order_id=build_client_order_id(
        symbol=position.symbol, position_id=position.position_id,
        suffix=f"MC{len(position.manual_exits) + 1}", identity_version=2), requested_qty=qty,
        close_reason=reason)
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
