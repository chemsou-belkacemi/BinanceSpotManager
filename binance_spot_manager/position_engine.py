"""Position Engine — logique metier d'une strategie identifiee par position_id.

Responsabilites :
- convertir un StrategyPlan en Position persistable ;
- ajouter un lot d'Entries a une position existante (section 44) ;
- recalculer metriques, prix moyen, quantites restantes et PnL ;
- resoudre les references dependantes (Entry 1, TP precedent, etc.).

Aucun appel reseau. Les montants reels viennent de Execution Engine ;
ici on travaille sur l'etat local et on le recalcule apres chaque fill.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence

from .models import (
    CloseReason,
    Commission,
    Entry,
    EntryReference,
    EntryStatus,
    EventType,
    OrderType,
    Position,
    PositionStatus,
    PriceMode,
    SLMode,
    SLStatus,
    SourceGroup,
    TPExecutionPolicy,
    TPReference,
    TPStatus,
    TakeProfit,
    utcnow,
)
from .strategy_engine import (
    EntrySpec,
    StrategyPlan,
    StrategySpec,
    percent_change,
    resolve_sl_price,
)
from .symbol_rules import SymbolRules

#: Tolerance sur les comparaisons de quantite (arrondis Binance).
QTY_EPSILON = 1e-12


class PositionEngineError(RuntimeError):
    pass


class DuplicatePositionError(PositionEngineError):
    """Tentative de creation d'une seconde position sur un symbole deja actif."""


class PositionEngine:
    """Construit et met a jour les positions."""

    def __init__(self, rules: Optional[SymbolRules] = None) -> None:
        self.rules = rules

    # ------------------------------------------------------------------
    # Creation depuis un plan
    # ------------------------------------------------------------------

    def from_plan(
        self,
        plan: StrategyPlan,
        spec: StrategySpec,
        *,
        environment: str = "DEMO",
    ) -> Position:
        """Convertit un StrategyPlan valide en Position."""
        if not plan.is_valid:
            raise PositionEngineError(
                "Plan invalide, impossible de creer la position : "
                + " ; ".join(plan.errors)
            )

        position = Position(
            order_identity_version=2,
            symbol=plan.symbol,
            base_asset=plan.base_asset,
            quote_asset=plan.quote_asset,
            status=PositionStatus.PENDING_ENTRIES,
            environment=environment,
            creation_price=spec.current_price,
            planned_capital=plan.capital_total,
            preset_name=spec.preset_name,
            tags=list(spec.tags),
        )

        group = SourceGroup(
            signal_id="",
            source=spec.source,
            label=spec.source_name or spec.source.value,
        )
        position.source_groups.append(group)

        source_specs = spec.entries
        for index, resolved in enumerate(plan.entries):
            entry_spec = source_specs[index] if index < len(source_specs) else EntrySpec()
            entry = Entry(
                sequence_number=resolved.sequence,
                group_id=group.group_id,
                signal_id=group.signal_id,
                source=spec.source,
                order_type=resolved.order_type,
                price_mode=entry_spec.price_mode,
                reference_mode=entry_spec.reference,
                requested_price=entry_spec.price,
                requested_offset_percent=entry_spec.offset_percent,
                resolved_price=resolved.price,
                capital_percent=resolved.capital_percent,
                quote_amount=resolved.quote_amount,
                requested_qty=resolved.raw_qty,
                binance_qty=resolved.qty,
                expires_at=self._expiry(entry_spec.expires_hours),
            )
            position.entries.append(entry)
            group.entry_ids.append(entry.entry_id)

        tp_specs = spec.take_profits
        for index, resolved in enumerate(plan.take_profits):
            tp_spec = tp_specs[index] if index < len(tp_specs) else None
            tp = TakeProfit(
                sequence_number=resolved.sequence,
                price_mode=tp_spec.price_mode if tp_spec else PriceMode.PERCENT,
                reference_mode=tp_spec.reference if tp_spec else TPReference.AVERAGE_PRICE,
                target_price=resolved.target_price,
                target_percent=resolved.target_percent,
                sell_percent=resolved.sell_percent,
                estimated_qty=resolved.estimated_qty,
                gain_estimated=resolved.gain_estimated,
                execution_policy=plan.tp_execution_policy,
                sl_rule_after_hit=resolved.sl_rule_after_hit,
                sl_rule_value=resolved.sl_rule_value,
                status=TPStatus.PENDING,
            )
            position.take_profits.append(tp)

        position.stop_loss.mode = plan.stop_loss.mode
        position.stop_loss.value = plan.stop_loss.value
        position.stop_loss.resolved_price = plan.stop_loss.price
        position.stop_loss.status = SLStatus.PLANNED
        position.stop_loss.quantity = plan.estimated_total_qty

        position.automation.tp_execution_policy = plan.tp_execution_policy
        position.automation.cancel_remaining_entries_on_first_tp = (
            plan.cancel_remaining_entries_on_first_tp
        )

        position.log(
            EventType.POSITION_CREATED,
            f"Position {position.symbol} creee ({len(position.entries)} entries, "
            f"{len(position.take_profits)} TP)",
            capital=plan.capital_total,
            average_price=plan.estimated_average_price,
        )

        recompute_position(position)
        return position

    # ------------------------------------------------------------------
    # Ajout d'Entries a une position existante
    # ------------------------------------------------------------------

    def add_entries(
        self,
        position: Position,
        new_entries: Sequence[Entry],
        *,
        source_group: Optional[SourceGroup] = None,
    ) -> Position:
        """Ajoute explicitement un lot d'Entries a la position fournie par son ID."""
        if not position.is_open:
            raise PositionEngineError(
                f"Position {position.position_id} non ouverte : ajout impossible"
            )
        if not new_entries:
            return position

        group = source_group or SourceGroup(label="ajout")
        position.source_groups.append(group)

        for entry in new_entries:
            entry.sequence_number = position.next_entry_sequence()
            entry.group_id = group.group_id
            entry.signal_id = group.signal_id or entry.signal_id
            entry.source = group.source if group.source else entry.source
            position.entries.append(entry)
            group.entry_ids.append(entry.entry_id)

        position.status = PositionStatus.ACTIVE
        position.log(
            EventType.POSITION_UPDATED,
            f"Ajout de {len(new_entries)} entry(ies) sur {position.symbol}",
            total_entries=len(position.entries),
        )
        recompute_position(position)
        return position

    def add_take_profits(self, position: Position, tps: Sequence[TakeProfit]) -> Position:
        for tp in tps:
            tp.sequence_number = position.next_tp_sequence()
            position.take_profits.append(tp)
        position.log(
            EventType.POSITION_UPDATED,
            f"Ajout de {len(tps)} TP sur {position.symbol}",
            total_tps=len(position.take_profits),
        )
        recompute_position(position)
        return position

    # ------------------------------------------------------------------
    # Application des fills
    # ------------------------------------------------------------------

    def apply_entry_fill(
        self,
        position: Position,
        entry_id: str,
        *,
        executed_qty: float,
        average_price: float,
        quote_spent: float,
        commissions: Optional[list[Commission]] = None,
        order_id: Optional[int] = None,
        status: Optional[EntryStatus] = None,
    ) -> Entry:
        """Met a jour une Entry apres confirmation Binance."""
        entry = position.entry_by_id(entry_id)
        if entry is None:
            raise PositionEngineError(f"Entry inconnue : {entry_id}")

        entry.executed_qty = executed_qty
        entry.average_fill_price = average_price
        entry.quote_spent = quote_spent
        if commissions:
            entry.commissions = list(commissions)
        entry.net_qty = max(executed_qty - entry.commission_total(position.base_asset), 0.0)
        if order_id is not None:
            entry.order_id = order_id

        if status is not None:
            entry.status = status
        elif executed_qty <= 0:
            entry.status = EntryStatus.SUBMITTED
        elif abs(executed_qty - entry.binance_qty) < QTY_EPSILON:
            entry.status = EntryStatus.FILLED
        else:
            entry.status = EntryStatus.PARTIALLY_FILLED

        if entry.status is EntryStatus.FILLED:
            entry.filled_at = utcnow()
            position.log(
                EventType.ENTRY_FILLED,
                f"Entry {entry.sequence_number} remplie @ {average_price}",
                entry_id=entry.entry_id,
                qty=executed_qty,
            )
        elif entry.status is EntryStatus.PARTIALLY_FILLED:
            position.log(
                EventType.ENTRY_PARTIAL,
                f"Entry {entry.sequence_number} partiellement remplie "
                f"({executed_qty}/{entry.binance_qty})",
                entry_id=entry.entry_id,
            )

        if position.status is PositionStatus.PENDING_ENTRIES and position.filled_entries:
            position.status = PositionStatus.ACTIVE

        recompute_position(position)
        self.refresh_tp_estimates(position)
        return entry

    def apply_tp_fill(
        self,
        position: Position,
        tp_id: str,
        *,
        executed_qty: float,
        average_price: float,
        quote_received: float,
        commissions: Optional[list[Commission]] = None,
        order_id: Optional[int] = None,
    ) -> TakeProfit:
        """Marque un TP comme execute apres verification Binance (section 14)."""
        tp = position.tp_by_id(tp_id)
        if tp is None:
            raise PositionEngineError(f"TP inconnu : {tp_id}")

        tp.executed_qty = executed_qty
        tp.average_fill_price = average_price
        tp.quote_received = quote_received
        if commissions:
            tp.commissions = list(commissions)
        if order_id is not None:
            tp.order_id = order_id

        average_price_position = position.metrics.average_price or position.creation_price
        fees = tp.commission_total(position.quote_asset)
        tp.gain_realized = (average_price - average_price_position) * executed_qty - fees

        if executed_qty <= 0:
            tp.status = TPStatus.FAILED
        elif tp.estimated_qty > 0 and executed_qty + QTY_EPSILON < tp.estimated_qty:
            tp.status = TPStatus.PARTIALLY_EXECUTED
        else:
            tp.status = TPStatus.EXECUTED

        if executed_qty > 0:
            tp.last_error = ""

        tp.executed_at = utcnow()
        position.log(
            EventType.TP_EXECUTED,
            f"TP {tp.sequence_number} execute @ {average_price} "
            f"(gain {tp.gain_realized:+.2f})",
            tp_id=tp.tp_id,
            qty=executed_qty,
        )

        recompute_position(position)
        self._maybe_finish(position)
        return tp

    def apply_sl_fill(
        self,
        position: Position,
        *,
        executed_qty: float,
        average_price: float,
        quote_received: float,
        commissions: Optional[list[Commission]] = None,
    ) -> Position:
        """SL execute cote Binance : la position se ferme."""
        sl = position.stop_loss
        sl.executed_qty = executed_qty
        sl.average_fill_price = average_price
        sl.quote_received = quote_received
        if commissions:
            sl.commissions = list(commissions)
        sl.status = SLStatus.EXECUTED
        sl.executed_at = utcnow()

        position.log(
            EventType.SL_EXECUTED,
            f"SL execute @ {average_price}",
            qty=executed_qty,
        )
        recompute_position(position)
        finish_position(position, CloseReason.SL_EXECUTED)
        return position

    # ------------------------------------------------------------------
    # Recalcul des TP restants apres un changement de quantite
    # ------------------------------------------------------------------

    def refresh_tp_estimates(self, position: Position) -> None:
        """Recalcule les quantites estimees des TP non executes."""
        remaining = max(position.metrics.net_qty, 0.0)
        for tp in position.sorted_tps:
            if tp.is_done:
                continue
            tp.estimated_qty = remaining * tp.sell_percent / 100.0

    # ------------------------------------------------------------------
    # Fermeture
    # ------------------------------------------------------------------

    def _maybe_finish(self, position: Position) -> None:
        """Ferme la position uniquement si tout est vendu.

        Point delicat : les quantites arrondies par Binance au stepSize laissent
        un reliquat de l'ordre de 1e-8 apres la vente du dernier TP. Comparer a
        QTY_EPSILON (1e-12) laisserait la position ouverte indefiniment. Le
        reliquat tolerable est donc la FRACTION SOUS UN PAS — la difference
        entre la quantite reelle et son arrondi au stepSize — et non la
        quantite arrondie elle-meme, qui vaut la quantite entiere quand elle
        est alignee sur le pas.
        """
        remaining = position.metrics.net_qty

        if self.rules is not None:
            dust = remaining - float(self.rules.round_qty(remaining))
        else:
            dust = 0.0

        if remaining <= QTY_EPSILON + dust:
            finish_position(position, CloseReason.ALL_TP_HIT)
            return

        # Tous les TP atteints mais un reliquat vendable subsiste : on ne ferme
        # pas la position en laissant une quantite sans trace.
        all_tps_done = (
            all(tp.is_done for tp in position.take_profits) if position.take_profits else False
        )
        if all_tps_done:
            position.log(
                EventType.POSITION_UPDATED,
                f"Tous les TP sont atteints mais il reste {remaining} "
                f"{position.base_asset} (residu non vendable)",
                remaining=remaining,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _expiry(hours: Optional[float]):
        from datetime import timedelta

        if not hours or hours <= 0:
            return None
        return utcnow() + timedelta(hours=hours)

    def compute_sl_rule_price(
        self,
        position: Position,
        rule: str,
        value: Optional[float] = None,
        *,
        after_tp_sequence: Optional[int] = None,
    ) -> Optional[float]:
        """Prix SL resultant d'une regle apres TP (section 17).

        Regles : NO_CHANGE, BREAK_EVEN, BREAK_EVEN_WITH_FEES, PREVIOUS_TP,
        FIXED_PRICE, CUSTOM_PERCENT.

        `after_tp_sequence` : sequence du TP qui vient d'etre atteint. Pour
        PREVIOUS_TP, le niveau vise est celui du TP de sequence strictement
        inferieure — sans ce parametre, un TP qui se declencherait seul
        remonterait le SL a son propre niveau.
        """
        metrics = position.metrics
        if rule == "NO_CHANGE":
            return position.stop_loss.resolved_price

        if rule == "BREAK_EVEN":
            return metrics.average_price or None

        if rule == "BREAK_EVEN_WITH_FEES":
            return metrics.break_even_with_fees or metrics.average_price or None

        if rule == "PREVIOUS_TP":
            # TP ayant vendu quelque chose : un TP2 marque PARTIALLY_EXECUTED (arrondi au stepSize) est
            # bien le TP precedent du TP3 ; avec les seuls EXECUTED, le stop restait au TP1.
            executed = position.hit_tps
            if after_tp_sequence is not None:
                executed = [
                    t for t in executed if t.sequence_number < after_tp_sequence
                ]
            return executed[-1].target_price if executed else None

        if rule == "FIXED_PRICE":
            return value if value else None

        if rule == "CUSTOM_PERCENT":
            if value is None:
                return None
            base = metrics.average_price
            if not base:
                return None
            return base * (1.0 + value / 100.0)

        return position.stop_loss.resolved_price


# ==========================================================================
# Recalcul des metriques
# ==========================================================================


def recompute_position(position: Position, *, fee_rates: Optional[dict[str, float]] = None) -> Position:
    """Recalcule integralement metriques et PnL depuis l'etat des Entries/TP/SL.

    Cette fonction est idempotente : l'appeler deux fois donne le meme resultat.
    C'est la garantie de reprise apres redemarrage (section 77).
    """
    metrics = position.metrics

    filled = position.filled_entries
    for entry in filled:
        entry.net_qty = max(
            entry.executed_qty - entry.commission_total(position.base_asset), 0.0
        )
    total_bought = sum(e.executed_qty for e in filled)
    spent = sum((e.executed_qty * e.average_fill_price) or e.quote_spent for e in filled)

    sold = sum(tp.executed_qty for tp in position.take_profits)
    received = sum(tp.quote_received for tp in position.take_profits)
    sold += position.stop_loss.executed_qty
    received += position.stop_loss.quote_received
    sold += sum(exit.executed_qty for exit in position.manual_exits)
    received += sum(exit.quote_received for exit in position.manual_exits)

    metrics.total_bought_qty = total_bought
    metrics.total_sold_qty = sold
    base_fees_on_buys = sum(e.commission_total(position.base_asset) for e in position.entries)
    base_fees_on_sells = sum(tp.commission_total(position.base_asset) for tp in position.take_profits)
    base_fees_on_sells += position.stop_loss.commission_total(position.base_asset)
    base_fees_on_sells += sum(exit.commission_total(position.base_asset) for exit in position.manual_exits)
    metrics.net_qty = max(total_bought - sold - base_fees_on_buys - base_fees_on_sells, 0.0)

    net_bought = max(total_bought - base_fees_on_buys, 0.0)
    metrics.average_price = (spent / net_bought) if net_bought > 0 else 0.0
    metrics.capital_committed = spent

    planned = sum(e.quote_amount for e in position.entries)
    metrics.capital_planned = planned
    metrics.capital_pending = max(
        sum(e.quote_amount for e in position.entries if not e.is_terminal and e.executed_qty <= 0),
        0.0,
    )

    quote_fees = sum(e.commission_total(position.quote_asset) for e in position.entries)
    quote_fees += sum(tp.commission_total(position.quote_asset) for tp in position.take_profits)
    quote_fees += position.stop_loss.commission_total(position.quote_asset)
    quote_fees += sum(exit.commission_total(position.quote_asset) for exit in position.manual_exits)
    base_fees = sum(e.commission_total(position.base_asset) for e in position.entries)
    base_fees += sum(tp.commission_total(position.base_asset) for tp in position.take_profits)
    base_fees += position.stop_loss.commission_total(position.base_asset)
    base_fees += sum(exit.commission_total(position.base_asset) for exit in position.manual_exits)
    metrics.commissions_quote = quote_fees
    metrics.commissions_base = base_fees

    average = metrics.average_price
    metrics.break_even_price = average
    if net_bought > 0 and quote_fees > 0:
        metrics.break_even_with_fees = average + (quote_fees / net_bought)
    else:
        metrics.break_even_with_fees = average

    current = metrics.current_price or average
    metrics.position_value = metrics.net_qty * current

    sl_price = position.stop_loss.resolved_price or 0.0
    metrics.max_loss_at_sl = (
        metrics.net_qty * (sl_price - metrics.average_price) if metrics.net_qty > 0 else 0.0
    )

    from .accounting import accounting_snapshot
    accounting = accounting_snapshot(position, fee_rates=fee_rates)
    realized = accounting["Realise"]
    position.pnl.realized = realized

    unrealized = accounting["Non realise"]
    position.pnl.unrealized = unrealized
    position.pnl.unrealized_percent = (
        percent_change(current, metrics.average_price) if metrics.average_price else 0.0
    )
    position.pnl.total = realized + unrealized
    position.pnl.fees_paid = quote_fees + accounting["Frais externes valorises"]
    position.pnl.unpriced_fees = accounting["Frais non convertis"]
    position.pnl.complete = accounting["Complet"]
    position.pnl.updated_at = utcnow()
    metrics.updated_at = utcnow()

    position.touch()
    return position


def apply_tp_sl_rule(
    position: Position,
    tp: TakeProfit,
    engine: Optional[PositionEngine] = None,
    *,
    after_tp_sequence: Optional[int] = None,
) -> Optional[float]:
    """Retourne le nouveau prix SL impose par la regle du TP qui vient d'etre atteint.

    `after_tp_sequence` est transmis a `compute_sl_rule_price` : pour la regle
    PREVIOUS_TP, le niveau vise doit etre celui d'un TP STRICTEMENT anterieur,
    sinon le SL remonterait au niveau du TP qui vient de se declencher.
    """
    engine = engine or PositionEngine()
    rule = tp.sl_rule_after_hit.value
    sequence = after_tp_sequence if after_tp_sequence is not None else tp.sequence_number
    return engine.compute_sl_rule_price(
        position, rule, tp.sl_rule_value, after_tp_sequence=sequence
    )


def set_current_price(position: Position, price: float) -> Position:
    """Met a jour le prix courant et recalcule le PnL latent."""
    position.metrics.current_price = price
    recompute_position(position)
    return position


def finish_position(position: Position, reason: CloseReason) -> Position:
    """Cloture logique de la position."""
    if not position.is_open:
        return position
    position.status = PositionStatus.CLOSED
    position.close_reason = reason
    position.closed_at = utcnow()
    position.log(
        EventType.POSITION_FINISHED,
        f"Position {position.symbol} terminee ({reason.value}) — "
        f"PnL realise {position.pnl.realized:+.2f}",
        reason=reason.value,
        realized=position.pnl.realized,
    )
    position.touch()
    return position


def remaining_sellable_qty(position: Position) -> float:
    """Quantite encore vendable (nette, arrondie par l'appelant si besoin)."""
    return max(position.metrics.net_qty, 0.0)


def total_planned_risk(position: Position) -> float:
    return abs(position.metrics.max_loss_at_sl)
