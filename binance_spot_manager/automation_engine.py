"""Automation Engine — surveillance des TP et du SL evolutif.

Boucle d'un cycle (section 14) :
  1. le prix courant atteint-il un TP ?
  2. vendre la quantite prevue et confirmer le fill Binance ;
  3. calculer le gain realise et la quantite restante ;
  4. appliquer la regle de SL liee a ce TP ;
  5. recalculer / remplacer le SL unique pour la quantite restante.

Un TP n'est JAMAIS marque execute parce que le prix a touche le niveau :
il faut la confirmation Binance. L'ordre des etapes est volontairement
sequentiel — un TP ne se declenche qu'un par cycle pour garder un etat coherent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .event_store import EventStore
from .execution_engine import ExecutionEngine, normalize_order_response
from .models import (
    CloseReason,
    Entry,
    EntryStatus,
    EventType,
    Position,
    PositionStatus,
    SLStatus,
    SyncStatus,
    TPStatus,
    TakeProfit,
    utcnow,
)
from .position_engine import (
    QTY_EPSILON,
    PositionEngine,
    apply_tp_sl_rule,
    finish_position,
    recompute_position,
)
from .symbol_rules import SymbolRules, SymbolRulesCache

logger = logging.getLogger("bsm.automation")


@dataclass
class CycleResult:
    """Ce qui s'est passe pendant un cycle sur une position."""

    position_id: str = ""
    symbol: str = ""
    actions: list[str] = field(default_factory=list)
    tp_executed: Optional[int] = None
    sl_moved_to: Optional[float] = None
    position_finished: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.actions)


@dataclass
class AutomationConfig:
    """Parametres de la boucle."""

    #: ne remplacer le SL que si le nouveau prix differe d'au moins ce %
    sl_replace_threshold_percent: float = 0.05
    #: declencher un TP des que prix >= cible (pas de tolerance cachee)
    tp_trigger_tolerance_percent: float = 0.0
    #: annuler les Entries restantes quand le premier TP est atteint
    cancel_entries_on_first_tp: bool = False


def planned_tp_quantity(position: Position, tp: TakeProfit, rules: SymbolRules) -> float:
    """Quantite d'un TP apres frais et arrondi, sans appel reseau."""
    remaining = position.metrics.net_qty
    if remaining <= QTY_EPSILON:
        return 0.0

    raw = remaining * tp.sell_percent / 100.0
    qty = float(rules.round_qty(raw, market=True))
    remaining_rounded = float(rules.round_qty(remaining, market=True))
    is_last = tp.sequence_number == max(
        (item.sequence_number for item in position.take_profits),
        default=tp.sequence_number,
    )
    if is_last and tp.sell_percent >= 99.99:
        return remaining_rounded
    return min(qty, remaining_rounded)


class AutomationEngine:
    """Applique la politique TP/SL sur les positions ouvertes."""

    def __init__(
        self,
        execution: Optional[ExecutionEngine] = None,
        position_engine: Optional[PositionEngine] = None,
        rules_cache: Optional[SymbolRulesCache] = None,
        *,
        events: Optional[EventStore] = None,
        config: Optional[AutomationConfig] = None,
    ) -> None:
        self.execution = execution or ExecutionEngine()
        self.position_engine = position_engine or PositionEngine()
        self.rules_cache = rules_cache or self.execution.rules_cache
        self.events = events or EventStore()
        self.config = config or AutomationConfig()

    # ------------------------------------------------------------------
    # Point d'entree
    # ------------------------------------------------------------------

    def run_cycle(self, position: Position, current_price: float) -> CycleResult:
        """Un passage complet de surveillance sur une position."""
        result = CycleResult(position_id=position.position_id, symbol=position.symbol)

        if not position.is_open:
            return result
        if position.automation.paused:
            result.actions.append("automation en pause — aucune action")
            return result

        position.metrics.current_price = current_price
        recompute_position(position)

        try:
            self._check_expired_entries(position, result)
            self._check_stop_loss(position, current_price, result)
            if not result.position_finished:
                self._check_take_profits(position, current_price, result)
        except Exception as exc:  # noqa: BLE001 — une position ne doit pas tuer le worker
            logger.exception("Cycle automation en echec (%s)", position.symbol)
            result.errors.append(str(exc))
            self.events.append(
                EventType.ERROR,
                f"Automation {position.symbol} : {exc}",
                position_id=position.position_id,
                symbol=position.symbol,
                level="ERROR",
            )

        position.automation.last_run_at = utcnow()
        recompute_position(position)
        return result

    # ------------------------------------------------------------------
    # Entries expirees
    # ------------------------------------------------------------------

    def _check_expired_entries(self, position: Position, result: CycleResult) -> None:
        for entry in list(position.open_entries):
            if not entry.is_expired:
                continue
            cancel = self.execution.cancel_entry(position, entry)
            if cancel.success:
                entry.status = EntryStatus.EXPIRED
                result.actions.append(f"Entry {entry.sequence_number} expiree")
                self.events.append(
                    EventType.ENTRY_EXPIRED,
                    f"Entry {entry.sequence_number} expiree ({position.symbol})",
                    position_id=position.position_id,
                    symbol=position.symbol,
                )
            else:
                result.errors.append(
                    f"Entry {entry.sequence_number} : expiration impossible ({cancel.error})"
                )

    # ------------------------------------------------------------------
    # Stop Loss
    # ------------------------------------------------------------------

    def _check_stop_loss(
        self, position: Position, current_price: float, result: CycleResult
    ) -> None:
        """Detecte un SL execute et, sinon, verifie que la protection existe.

        Trois temps, dans cet ordre :
          1. un SL est connu localement et ACTIVE -> verifier son etat, puis sortir ;
          2. un clientOrderId a deja servi cote Binance -> adopter l'ordre ;
          3. sinon, recreer un SL pour la quantite restante (jamais sous minQty).
        """
        sl = position.stop_loss
        remaining = position.metrics.net_qty

        if sl.status is SLStatus.CANCELED and any(
            tp.status is TPStatus.SUBMITTED for tp in position.take_profits
        ):
            return

        # Aucune quantite nette : il n'y a rien a proteger et aucun ordre a
        # interroger. Sans ce garde, on demande a Binance le statut d'un ordre
        # inexistant a chaque cycle (-2013 dans le journal).
        if remaining <= QTY_EPSILON and not sl.order_id:
            return

        # 1. Un SL existe cote Binance : on verifie son etat, puis on sort.
        if sl.order_id and sl.status is SLStatus.ACTIVE:
            status = self._fetch_status_safe(
                position, order_id=sl.order_id, client_order_id=sl.client_order_id
            )
            if status is None and not self.execution.settings.dry_run:
                # Ordre introuvable : soit execute (il quitte les ordres ouverts
                # uniquement s'il est rempli), soit annule manuellement.
                result.actions.append("SL introuvable cote Binance — reconciliation requise")
                position.sync_status = position.sync_status.__class__.DESYNC_DETECTED
                return
            if status is not None and status.executed_qty > QTY_EPSILON:
                self.position_engine.apply_sl_fill(
                    position,
                    executed_qty=status.executed_qty,
                    average_price=status.average_price,
                    quote_received=status.cummulative_quote_qty,
                    commissions=status.commissions,
                )
                result.actions.append(f"SL execute @ {status.average_price}")
                result.position_finished = True
                return
            next_tp = position.next_tp
            tp_due = bool(
                next_tp and next_tp.target_price
                and self._is_triggered(next_tp, current_price)
            )
            if status is not None and sl.resolved_price and not tp_due:
                rules = self._rules(position)
                desired_qty = float(rules.round_qty(remaining))
                active_qty = float(status.raw.get("origQty") or sl.quantity or 0)
                if desired_qty >= float(rules.min_qty) and abs(active_qty - desired_qty) >= float(rules.step_size) / 2:
                    adjusted = self.execution.move_stop_loss(
                        position,
                        new_stop_price=sl.resolved_price,
                        quantity=desired_qty,
                    )
                    if adjusted.success:
                        result.actions.append(f"SL ajuste a {desired_qty} {position.base_asset}")
                    else:
                        result.errors.append(f"SL non ajuste : {adjusted.error}")
                    return
            # L'ordre vit toujours et n'est pas rempli : la protection est en
            # place. On sort ici — sans ce retour, le bloc suivant recreerait un
            # SL a chaque cycle et empilerait les ordres.
            return

        # 2. Sans quantite nette, aucune protection a creer non plus.
        if remaining <= QTY_EPSILON:
            return

        # 3. Un clientOrderId a-t-il deja servi sans que le local le sache ?
        #    Cas typique apres un redemarrage : l'ordre existe chez Binance
        #    mais order_id est vide en local.
        if not self.execution.settings.dry_run and sl.resolved_price:
            candidate_id = sl.client_order_id or self.execution._sl_client_order_id(
                position, sl.replace_count
            )
            existing = self.execution.find_existing_order(position.symbol, candidate_id)
            if existing is not None and normalize_order_response(existing).is_open:
                sl.order_id = existing.get("orderId")
                sl.client_order_id = existing.get("clientOrderId") or candidate_id
                sl.status = SLStatus.ACTIVE
                if not sl.quantity:
                    sl.quantity = remaining
                result.actions.append("SL existant adopte (reprise d'etat)")
                return

        # 4. Protection manquante alors qu'il reste de la quantite : on recree
        #    — mais jamais sous minQty, ou l'ordre serait refuse par Binance.
        min_qty = float(self._rules(position).min_qty)
        if (
            remaining >= min_qty
            and sl.status in {SLStatus.PLANNED, SLStatus.FAILED, SLStatus.CANCELED}
            and sl.resolved_price
        ):
            created = self.execution.place_stop_loss(
                position,
                stop_price=sl.resolved_price,
                quantity=remaining,
                attempt=sl.replace_count,
            )
            if created.success:
                result.actions.append(f"SL recree @ {sl.resolved_price}")
            else:
                result.errors.append(f"SL non recree : {created.error}")

    def _fetch_status_safe(
        self,
        position: Position,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ):
        """Statut d'un ordre, ou None s'il n'existe pas (jamais d'exception).

        Un ordre inconnu est un etat normal : il a pu etre rempli et quitter la
        liste des ordres ouverts, ou etre annule depuis Binance. Le journaliser
        en ERROR a chaque cycle noierait les vraies erreurs.
        """
        if not order_id and not client_order_id:
            return None
        try:
            return self.execution.fetch_order_status(
                position.symbol,
                order_id=order_id,
                client_order_id=client_order_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "Statut indisponible pour %s (order_id=%s) : %s",
                position.symbol,
                order_id,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Take Profits
    # ------------------------------------------------------------------

    def _check_take_profits(
        self, position: Position, current_price: float, result: CycleResult
    ) -> None:
        """Traite AU PLUS un TP par cycle, dans l'ordre des sequences."""
        next_tp = position.next_tp
        if next_tp is None or not next_tp.target_price:
            self._finish_if_fully_sold(position, result)
            return

        if next_tp.status is TPStatus.SUBMITTED:
            status = self._fetch_status_safe(
                position,
                order_id=next_tp.order_id,
                client_order_id=next_tp.client_order_id,
            )
            if status is not None and (status.is_filled or status.is_terminal_dead):
                if status.executed_qty > QTY_EPSILON:
                    self._apply_confirmed_tp(position, next_tp, status, result)
                else:
                    next_tp.status = TPStatus.FAILED
                    next_tp.attempt_count += 1
                    next_tp.order_id = None
                    next_tp.client_order_id = None
                    self._restore_stop_loss(position, result)
            else:
                result.actions.append("TP en attente de confirmation Binance")
            return

        if not self._is_triggered(next_tp, current_price):
            return

        if next_tp.triggered_at is None:
            next_tp.triggered_at = utcnow()
            next_tp.status = TPStatus.TRIGGERED
            self.events.append(
                EventType.TP_TRIGGERED,
                f"TP {next_tp.sequence_number} {position.symbol} atteint @ {current_price}",
                position_id=position.position_id,
                symbol=position.symbol,
                target=next_tp.target_price,
            )
            position.log(
                EventType.TP_TRIGGERED,
                f"TP {next_tp.sequence_number} atteint @ {current_price}",
                tp_id=next_tp.tp_id,
            )

        quantity = self._sell_quantity(position, next_tp)
        if quantity <= 0:
            next_tp.status = TPStatus.FAILED
            next_tp.last_error = "Quantite a vendre nulle ou sous minQty"
            result.errors.append(
                f"TP {next_tp.sequence_number} : quantite vendable insuffisante"
            )
            self.events.append(
                EventType.ERROR,
                f"TP {next_tp.sequence_number} non vendable ({position.symbol})",
                position_id=position.position_id,
                symbol=position.symbol,
                level="ERROR",
            )
            return

        if not self.execution.settings.dry_run:
            if position.stop_loss.status is SLStatus.ACTIVE:
                if not self._release_stop_loss(position, result):
                    return
            try:
                available = self.execution.get_free_balance(position.base_asset)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"Solde {position.base_asset} illisible : {exc}")
                self._restore_stop_loss(position, result)
                return
            available_rounded = float(
                self._rules(position).round_qty(available, market=True)
            )
            if available_rounded + QTY_EPSILON < quantity:
                position.sync_status = SyncStatus.DESYNC_DETECTED
                result.errors.append(
                    f"Solde libre {position.base_asset} insuffisant apres annulation "
                    f"du SL ({available_rounded} < {quantity})"
                )
                self._restore_stop_loss(position, result)
                return

        try:
            order = self.execution.place_tp_sell(
                position, next_tp, quantity=quantity, current_price=current_price
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"TP {next_tp.sequence_number} : {exc}")
            self._restore_stop_loss(position, result)
            return

        if order.dry_run:
            next_tp.status = TPStatus.TRIGGERED
            result.actions.append(
                f"TP {next_tp.sequence_number} detecte (DRY_RUN : aucune vente envoyee)"
            )
            return

        if not order.success:
            next_tp.status = TPStatus.FAILED
            next_tp.last_error = order.error
            result.errors.append(f"TP {next_tp.sequence_number} : {order.error}")
            self._restore_stop_loss(position, result)
            return

        if order.is_terminal_dead and order.executed_qty <= QTY_EPSILON:
            next_tp.status = TPStatus.FAILED
            next_tp.attempt_count += 1
            next_tp.order_id = None
            next_tp.client_order_id = None
            self._restore_stop_loss(position, result)
            return

        # Confirmation Binance obligatoire avant de valider le TP.
        confirmed = self._confirm_tp_fill(position, next_tp, order)
        if not confirmed:
            next_tp.status = TPStatus.SUBMITTED
            result.actions.append(
                f"TP {next_tp.sequence_number} envoye, fill en attente de confirmation"
            )
            return

        self._apply_confirmed_tp(position, next_tp, confirmed, result)

    def _apply_confirmed_tp(
        self, position: Position, tp: TakeProfit, confirmed, result: CycleResult
    ) -> None:
        self.position_engine.apply_tp_fill(
            position,
            tp.tp_id,
            executed_qty=confirmed.executed_qty,
            average_price=confirmed.average_price,
            quote_received=confirmed.cummulative_quote_qty,
            commissions=confirmed.commissions,
            order_id=confirmed.order_id,
        )
        result.tp_executed = tp.sequence_number
        result.actions.append(
            f"TP {tp.sequence_number} execute @ {confirmed.average_price} "
            f"({confirmed.executed_qty})"
        )

        if tp.sequence_number == 1 and self.config.cancel_entries_on_first_tp:
            cancels = self.execution.cancel_open_entries(position)
            if cancels:
                result.actions.append(f"{len(cancels)} entry(ies) restantes annulees")

        self._apply_sl_rule(position, tp, result)
        self._finish_if_fully_sold(position, result)
        self._restore_stop_loss(position, result)

    def _release_stop_loss(self, position: Position, result: CycleResult) -> bool:
        sl = position.stop_loss
        if not sl.order_id and not sl.client_order_id:
            result.errors.append("SL actif sans identifiant : vente TP suspendue")
            return False
        cancelled = self.execution.cancel_order(
            position.symbol,
            order_id=sl.order_id,
            client_order_id=sl.client_order_id,
        )
        if not cancelled.success or cancelled.status != "CANCELED" or cancelled.executed_qty > 0:
            position.sync_status = SyncStatus.DESYNC_DETECTED
            result.errors.append(
                "Annulation du SL non confirmee : vente TP suspendue "
                f"({cancelled.error or cancelled.status})"
            )
            return False
        sl.status = SLStatus.CANCELED
        sl.order_id = None
        sl.replace_count += 1
        result.actions.append("SL annule avant vente TP")
        return True

    def _restore_stop_loss(self, position: Position, result: CycleResult) -> None:
        sl = position.stop_loss
        if not position.is_open or sl.status is not SLStatus.CANCELED:
            return
        quantity = float(self._rules(position).round_qty(position.metrics.net_qty))
        if quantity <= 0:
            return
        if not sl.resolved_price:
            result.errors.append("SL non recree : prix de protection absent")
            return
        restored = self.execution.place_stop_loss(
            position,
            stop_price=sl.resolved_price,
            quantity=quantity,
            attempt=sl.replace_count,
        )
        if restored.success:
            result.actions.append("SL restaure apres tentative de TP")
        else:
            result.errors.append(f"SL non restaure apres TP : {restored.error}")
            self.events.append(
                EventType.ERROR,
                f"SL non restaure apres TP {position.symbol} : {restored.error}",
                position_id=position.position_id,
                symbol=position.symbol,
                level="CRITICAL",
            )

    def _is_triggered(self, tp: TakeProfit, current_price: float) -> bool:
        tolerance = tp.target_price * (self.config.tp_trigger_tolerance_percent / 100.0)
        return current_price >= (tp.target_price - tolerance)

    def _sell_quantity(self, position: Position, tp: TakeProfit) -> float:
        """Quantite reellement vendable pour ce TP, bornee par le restant."""
        return planned_tp_quantity(position, tp, self._rules(position))

    def _confirm_tp_fill(
        self, position: Position, tp: TakeProfit, order
    ) -> Optional[object]:
        """Relit le statut pour confirmer la vente. None tant que non rempli.

        Une vente Market est normalement FILLED immediatement ; une Limit peut
        rester NEW. Dans ce cas on ne valide rien et on repassera au cycle suivant.
        """
        if order.is_filled:
            return order
        # Ordre jamais envoye (aucun identifiant) : rien a interroger.
        if not order.order_id and not order.client_order_id:
            return None

        status = self._fetch_status_safe(
            position,
            order_id=order.order_id,
            client_order_id=order.client_order_id,
        )
        if status is None:
            return None
        if status.is_filled:
            return status
        if status.is_terminal_dead and status.executed_qty > QTY_EPSILON:
            # Vente partielle : on enregistre ce qui a vraiment ete vendu.
            return status
        return None

    # ------------------------------------------------------------------
    # SL evolutif
    # ------------------------------------------------------------------

    def _apply_sl_rule(
        self, position: Position, tp: TakeProfit, result: CycleResult
    ) -> None:
        """Applique la regle SL attachee au TP qui vient d'etre execute."""
        new_price = apply_tp_sl_rule(
            position, tp, self.position_engine, after_tp_sequence=tp.sequence_number
        )
        if new_price is None:
            return

        rules = self._rules(position)
        new_price = float(rules.round_price(new_price, mode="down"))
        if new_price <= 0:
            return

        previous = position.stop_loss.resolved_price or 0.0
        if previous > 0:
            delta_percent = abs(new_price - previous) / previous * 100.0
            if delta_percent < self.config.sl_replace_threshold_percent:
                result.actions.append(
                    f"SL inchange (ecart {delta_percent:.3f} % < seuil)"
                )
                return

        remaining = position.metrics.net_qty
        if remaining <= QTY_EPSILON:
            return

        min_qty = float(rules.min_qty)
        if remaining < min_qty:
            # Reste trop peu pour un ordre SL valide : on annule la protection
            # et on le journalise — jamais de SL fantome sous minQty.
            if position.stop_loss.order_id:
                self.execution.cancel_order(
                    position.symbol,
                    order_id=position.stop_loss.order_id,
                    client_order_id=position.stop_loss.client_order_id,
                )
            position.stop_loss.status = SLStatus.NONE
            position.stop_loss.resolved_price = new_price
            result.actions.append(
                f"SL non replace ({remaining} < minQty {min_qty}) — position non protegee"
            )
            self.events.append(
                EventType.ERROR,
                f"SL {position.symbol} non remplacable : quantite restante sous minQty",
                position_id=position.position_id,
                symbol=position.symbol,
                level="CRITICAL",
            )
            return

        if position.stop_loss.status is not SLStatus.ACTIVE:
            position.stop_loss.resolved_price = new_price
            result.sl_moved_to = new_price
            return

        order = self.execution.move_stop_loss(
            position, new_stop_price=new_price, quantity=remaining
        )
        position.stop_loss.resolved_price = new_price
        recompute_position(position)

        if order.success:
            result.sl_moved_to = new_price
            result.actions.append(f"SL deplace vers {new_price}")
            position.log(
                EventType.SL_MOVED,
                f"SL deplace vers {new_price} apres TP {tp.sequence_number}",
                tp_id=tp.tp_id,
            )
        else:
            result.errors.append(f"SL non deplace : {order.error}")

    # ------------------------------------------------------------------
    # Fin de position
    # ------------------------------------------------------------------

    def _finish_if_fully_sold(self, position: Position, result: CycleResult) -> None:
        remaining = position.metrics.net_qty
        rules = self._rules(position)
        # Meme logique que PositionEngine._maybe_finish : on ne ferme que si
        # tout est vendu. Le seul reliquat tolerable est la FRACTION SOUS UN PAS
        # (difference entre la quantite reelle et son arrondi au stepSize).
        dust = remaining - float(rules.round_qty(remaining))
        if remaining > QTY_EPSILON + dust:
            return
        if position.stop_loss.status is SLStatus.ACTIVE:
            # Plus rien a vendre : la protection n'a plus d'objet.
            self.execution.cancel_order(
                position.symbol,
                order_id=position.stop_loss.order_id,
                client_order_id=position.stop_loss.client_order_id,
            )
            position.stop_loss.status = SLStatus.CANCELED
        finish_position(position, CloseReason.ALL_TP_HIT)
        result.position_finished = True
        result.actions.append("position terminee (quantite entierement vendue)")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _rules(self, position: Position) -> SymbolRules:
        return self.rules_cache.get(position.symbol)


# ==========================================================================
# Entree de boucle pour le worker
# ==========================================================================


def run_automation_for_positions(
    positions: list[Position],
    price_provider,
    *,
    engine: Optional[AutomationEngine] = None,
    save_position=None,
) -> list[CycleResult]:
    """Execute un cycle sur chaque position ouverte.

    `price_provider` : callable(symbol) -> float | None.
    `save_position`  : callable(Position) -> None (persistance apres mutation).
    """
    engine = engine or AutomationEngine()
    results: list[CycleResult] = []

    for position in positions:
        if not position.is_open:
            continue

        price = price_provider(position.symbol)
        if price is None:
            continue

        outcome = engine.run_cycle(position, float(price))
        results.append(outcome)

        if save_position is not None and (outcome.changed or outcome.errors):
            save_position(position)

    return results
