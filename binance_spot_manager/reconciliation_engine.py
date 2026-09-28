"""Reconciliation Engine — Binance est la source de verite (section 19).

Compare regulierement l'etat local a l'etat Binance et detecte :
  - ordre annule manuellement ;
  - ordre execute manuellement ;
  - SL annule ;
  - quantite vendue manuellement ;
  - ordre absent ou inconnu ;
  - quantite du wallet differente de l'attendu.

Etats produits : SYNCED, DESYNC_DETECTED, MANUAL_CHANGE, RECONCILED.

Le moteur ne corrige jamais en silence : il propose un plan de correction que
l'utilisateur voit dans le Dashboard. Seule la reprise d'etat NON ambiguë
(un fill reel a enregistrer) est appliquee automatiquement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .event_store import EventStore
from .execution_engine import ExecutionEngine, OrderResult, normalize_order_response
from .models import (
    CloseReason,
    Commission,
    Entry,
    EntryStatus,
    EventType,
    Position,
    SLStatus,
    SyncStatus,
    TPStatus,
    TakeProfit,
    utcnow,
)
from .position_engine import QTY_EPSILON, recompute_position, finish_position

logger = logging.getLogger("bsm.reconciliation")

#: Ecart de quantite tolere entre wallet et local (arrondis Binance).
QTY_TOLERANCE_RATIO = 0.01


@dataclass
class Finding:
    """Un ecart constate entre l'etat local et Binance."""

    kind: str
    severity: str  # INFO | WARNING | CRITICAL
    message: str
    symbol: str = ""
    position_id: str = ""
    target_type: str = ""  # ENTRY | TP | SL | WALLET | ORDER
    target_id: str = ""
    order_id: Optional[int] = None
    suggested_action: str = ""
    auto_applied: bool = False
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconciliationReport:
    position_id: str = ""
    symbol: str = ""
    status: SyncStatus = SyncStatus.SYNCED
    findings: list[Finding] = field(default_factory=list)
    checked_at: Any = None
    open_orders_count: int = 0

    @property
    def has_desync(self) -> bool:
        return bool(self.findings)

    @property
    def manual_changes(self) -> list[Finding]:
        return [f for f in self.findings if f.kind == "MANUAL_CHANGE"]

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "CRITICAL"]

    def add(
        self,
        kind: str,
        message: str,
        *,
        severity: str = "WARNING",
        target_type: str = "",
        target_id: str = "",
        order_id: Optional[int] = None,
        suggested_action: str = "",
        auto_applied: bool = False,
        **data: Any,
    ) -> Finding:
        finding = Finding(
            kind=kind,
            severity=severity,
            message=message,
            symbol=self.symbol,
            position_id=self.position_id,
            target_type=target_type,
            target_id=target_id,
            order_id=order_id,
            suggested_action=suggested_action,
            auto_applied=auto_applied,
            data=data,
        )
        self.findings.append(finding)
        return finding


# ==========================================================================
# Moteur
# ==========================================================================


class ReconciliationEngine:
    def __init__(
        self,
        execution: Optional[ExecutionEngine] = None,
        *,
        events: Optional[EventStore] = None,
        auto_apply_fills: bool = True,
    ) -> None:
        self.execution = execution or ExecutionEngine()
        self.events = events or EventStore()
        #: enregistrer automatiquement un fill reel non ambigu (jamais une annulation).
        self.auto_apply_fills = auto_apply_fills

    # ------------------------------------------------------------------
    # Reconciliation d'une position
    # ------------------------------------------------------------------

    def reconcile(
        self,
        position: Position,
        *,
        wallet_free_qty: Optional[float] = None,
        apply: bool = True,
    ) -> ReconciliationReport:
        if not apply:
            position = position.model_copy(deep=True)
        report = ReconciliationReport(
            position_id=position.position_id,
            symbol=position.symbol,
            checked_at=utcnow(),
        )

        if position.oco_exit is not None:
            report.add("OCO_MONITOR", "OCO suivi par le worker : utiliser le controle TP/SL de Settings", severity="INFO")
            return report

        if self.execution.settings.dry_run:
            # En DRY_RUN il n'y a rien a comparer : aucun ordre n'existe.
            report.status = SyncStatus.SYNCED
            return report

        try:
            open_orders = self.execution.get_open_orders(position.symbol)
        except Exception as exc:  # noqa: BLE001
            report.add(
                "ERROR",
                f"Ordres ouverts illisibles : {exc}",
                severity="CRITICAL",
                suggested_action="Verifier la connexion Binance",
            )
            report.status = SyncStatus.DESYNC_DETECTED
            return report

        report.open_orders_count = len(open_orders)
        by_client_id = {o.get("clientOrderId"): o for o in open_orders}
        by_order_id = {o.get("orderId"): o for o in open_orders}

        self._reconcile_entries(position, report, by_client_id, by_order_id, apply)
        self._reconcile_take_profits(position, report, by_client_id, by_order_id, apply)
        self._reconcile_stop_loss(position, report, by_client_id, by_order_id)
        self._reconcile_wallet(position, report, wallet_free_qty)

        if report.has_desync:
            has_manual = bool(report.manual_changes)
            report.status = (
                SyncStatus.MANUAL_CHANGE if has_manual else SyncStatus.DESYNC_DETECTED
            )
            position.sync_status = report.status
            if apply:
                self.events.append(
                    EventType.MANUAL_CHANGE if has_manual else EventType.DESYNC_DETECTED,
                    f"{len(report.findings)} ecart(s) detecte(s) sur {position.symbol}",
                    position_id=position.position_id,
                    symbol=position.symbol,
                    level="WARNING",
                    findings=[f.message for f in report.findings],
                )
            if apply:
                position.log(
                    EventType.MANUAL_CHANGE if has_manual else EventType.DESYNC_DETECTED,
                    f"{len(report.findings)} ecart(s) avec Binance",
                    status=report.status.value,
                )
        else:
            if position.sync_status is not SyncStatus.SYNCED:
                position.sync_status = SyncStatus.RECONCILED
                report.status = SyncStatus.RECONCILED
            else:
                report.status = SyncStatus.SYNCED

        recompute_position(position)
        return report

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def _reconcile_entries(
        self,
        position: Position,
        report: ReconciliationReport,
        by_client_id: dict[str, dict[str, Any]],
        by_order_id: dict[int, dict[str, Any]],
        apply: bool,
    ) -> None:
        for entry in position.entries:
            order = self._find_order(entry, by_client_id, by_order_id)

            if entry.status is EntryStatus.SUBMITTED and order is None:
                # L'ordre a quitte les ordres ouverts : rempli ou annule ?
                status = self.execution.fetch_order_status(
                    position.symbol,
                    order_id=entry.order_id,
                    client_order_id=entry.client_order_id,
                )
                if status is None:
                    report.add(
                        "ORDER_MISSING",
                        f"Entry {entry.sequence_number} absente des deux cotes",
                        severity="WARNING",
                        target_type="ENTRY",
                        target_id=entry.entry_id,
                        suggested_action="Verifier manuellement sur Binance",
                    )
                    continue
                if status.executed_qty > QTY_EPSILON:
                    self._apply_entry_fill(position, entry, status, report, apply)
                elif status.is_terminal_dead:
                    entry.status = EntryStatus.CANCELED
                    report.add(
                        "MANUAL_CHANGE",
                        f"Entry {entry.sequence_number} annulee cote Binance",
                        target_type="ENTRY",
                        target_id=entry.entry_id,
                        suggested_action="Aucune action automatique — annulation respectee",
                    )
                continue

            if order is not None and entry.status is EntryStatus.PLANNED:
                report.add(
                    "ORDER_UNKNOWN",
                    f"Ordre ouvert inattendu pour Entry {entry.sequence_number}",
                    target_type="ENTRY",
                    target_id=entry.entry_id,
                    order_id=order.get("orderId"),
                    suggested_action="Adopter l'ordre ou l'annuler depuis le Dashboard",
                )

    def _apply_entry_fill(
        self,
        position: Position,
        entry: Entry,
        status: OrderResult,
        report: ReconciliationReport,
        apply: bool,
    ) -> None:
        if entry.executed_qty > QTY_EPSILON:
            return
        if not (self.auto_apply_fills and apply):
            report.add(
                "FILL_DETECTED",
                f"Entry {entry.sequence_number} remplie cote Binance "
                f"(non enregistree localement)",
                severity="CRITICAL",
                target_type="ENTRY",
                target_id=entry.entry_id,
                suggested_action="Resynchroniser la position",
                executed_qty=status.executed_qty,
            )
            return

        entry.status = (
            EntryStatus.FILLED
            if abs(status.executed_qty - entry.binance_qty) < QTY_EPSILON
            else EntryStatus.PARTIALLY_FILLED
        )
        entry.executed_qty = status.executed_qty
        entry.average_fill_price = status.average_price
        entry.quote_spent = status.cummulative_quote_qty or status.executed_qty * status.average_price
        if status.commissions:
            entry.commissions = list(status.commissions)
        entry.order_id = status.order_id or entry.order_id
        entry.filled_at = entry.filled_at or utcnow()

        report.add(
            "FILL_APPLIED",
            f"Entry {entry.sequence_number} : fill Binance enregistre "
            f"({status.executed_qty} @ {status.average_price})",
            severity="WARNING",
            target_type="ENTRY",
            target_id=entry.entry_id,
            auto_applied=True,
            suggested_action="Verifier le prix moyen recalcule",
        )

    # ------------------------------------------------------------------
    # Take Profits
    # ------------------------------------------------------------------

    def _reconcile_take_profits(
        self,
        position: Position,
        report: ReconciliationReport,
        by_client_id: dict[str, dict[str, Any]],
        by_order_id: dict[int, dict[str, Any]],
        apply: bool,
    ) -> None:
        for tp in position.take_profits:
            if tp.status is not TPStatus.SUBMITTED or not (tp.order_id or tp.client_order_id):
                continue
            order = by_order_id.get(tp.order_id) or (
                by_client_id.get(tp.client_order_id or "") 
            )
            if order is not None:
                if apply and not tp.order_id and order.get("orderId"):
                    tp.order_id = order["orderId"]
                    report.add(
                        "ORDER_ADOPTED",
                        f"TP {tp.sequence_number} : identifiant Binance retrouve",
                        target_type="TP", target_id=tp.tp_id,
                        order_id=tp.order_id, auto_applied=True,
                    )
                continue

            status = self.execution.fetch_order_status(
                position.symbol, order_id=tp.order_id, client_order_id=tp.client_order_id
            )
            if status is None:
                report.add(
                    "ORDER_MISSING",
                    f"TP {tp.sequence_number} : ordre introuvable cote Binance",
                    target_type="TP",
                    target_id=tp.tp_id,
                    suggested_action="Verifier l'historique des ordres",
                )
                continue

            if status.is_open:
                if status.executed_qty > QTY_EPSILON:
                    report.add(
                        "PARTIAL_FILL",
                        f"TP {tp.sequence_number} : vente partielle encore ouverte",
                        severity="CRITICAL", target_type="TP", target_id=tp.tp_id,
                        suggested_action="Attendre la confirmation Binance avant toute nouvelle vente",
                    )
                continue

            if status.executed_qty > QTY_EPSILON and apply and self.auto_apply_fills:
                tp.order_id = status.order_id or tp.order_id
                tp.executed_qty = status.executed_qty
                tp.average_fill_price = status.average_price
                tp.quote_received = status.cummulative_quote_qty
                if status.commissions:
                    tp.commissions = list(status.commissions)
                average = position.metrics.average_price or position.creation_price
                tp.gain_realized = (
                    (status.average_price - average) * status.executed_qty
                    - tp.commission_total(position.quote_asset)
                )
                tp.status = TPStatus.EXECUTED
                tp.executed_at = utcnow()
                report.add(
                    "FILL_APPLIED",
                    f"TP {tp.sequence_number} : vente Binance enregistree "
                    f"({status.executed_qty} @ {status.average_price})",
                    target_type="TP",
                    target_id=tp.tp_id,
                    auto_applied=True,
                )
            elif status.executed_qty > QTY_EPSILON:
                report.add(
                    "FILL_DETECTED",
                    f"TP {tp.sequence_number} : vente confirmee non appliquee localement",
                    severity="CRITICAL", target_type="TP", target_id=tp.tp_id,
                    suggested_action="Resynchroniser la position",
                )
            elif status.is_terminal_dead and status.executed_qty <= QTY_EPSILON:
                if apply:
                    tp.status = TPStatus.CANCELED
                report.add(
                    "MANUAL_CHANGE",
                    f"TP {tp.sequence_number} termine sans vente sur Binance",
                    target_type="TP", target_id=tp.tp_id,
                    suggested_action="Verifier la protection restante ; aucun ordre de remplacement automatique",
                )

    # ------------------------------------------------------------------
    # Stop Loss
    # ------------------------------------------------------------------

    def _reconcile_stop_loss(
        self,
        position: Position,
        report: ReconciliationReport,
        by_client_id: dict[str, dict[str, Any]],
        by_order_id: dict[int, dict[str, Any]],
    ) -> None:
        sl = position.stop_loss
        remaining = position.metrics.net_qty

        # SL attendu mais absent
        if sl.status is SLStatus.ACTIVE and sl.order_id:
            still_open = by_order_id.get(sl.order_id) is not None or (
                by_client_id.get(sl.client_order_id or "") is not None
            )
            if not still_open:
                status = self.execution.fetch_order_status(
                    position.symbol,
                    order_id=sl.order_id,
                    client_order_id=sl.client_order_id,
                )
                if status is not None and status.executed_qty > QTY_EPSILON:
                    report.add(
                        "FILL_DETECTED",
                        f"SL execute cote Binance @ {status.average_price}",
                        severity="CRITICAL",
                        target_type="SL",
                        suggested_action="Enregistrer la sortie et cloturer la position",
                        executed_qty=status.executed_qty,
                    )
                elif status is not None and status.is_terminal_dead:
                    sl.status = SLStatus.CANCELED
                    report.add(
                        "MANUAL_CHANGE",
                        "SL annule cote Binance — position non protegee",
                        severity="CRITICAL",
                        target_type="SL",
                        suggested_action="Recréer un SL depuis le Dashboard",
                    )
                else:
                    report.add(
                        "UNKNOWN_ORDER", "SL non confirme : son absence des ordres ouverts ne prouve pas une annulation",
                        severity="CRITICAL", target_type="SL",
                        suggested_action="Verifier le statut Binance avant toute action",
                    )

        # Le SL ne peut proteger que la quantite vendable au pas LOT_SIZE.
        # Le reliquat inferieur a un pas n'est pas une desynchronisation.
        if sl.status is SLStatus.ACTIVE and sl.quantity > 0 and remaining > QTY_EPSILON:
            expected_qty = float(self.execution.rules(position.symbol).round_qty(remaining))
            gap = abs(sl.quantity - expected_qty)
            if gap > max(QTY_EPSILON, expected_qty * QTY_TOLERANCE_RATIO):
                report.add(
                    "SL_QTY_MISMATCH",
                    f"SL protege {sl.quantity} alors que la quantite vendable est {expected_qty}",
                    severity="WARNING",
                    target_type="SL",
                    suggested_action="Remplacer le SL pour la quantite restante",
                    sl_qty=sl.quantity,
                    remaining=remaining,
                    expected_qty=expected_qty,
                )

    # ------------------------------------------------------------------
    # Wallet
    # ------------------------------------------------------------------

    def _reconcile_wallet(
        self,
        position: Position,
        report: ReconciliationReport,
        wallet_free_qty: Optional[float],
    ) -> None:
        if wallet_free_qty is None:
            return
        local = position.metrics.net_qty
        if local <= QTY_EPSILON:
            return

        gap = abs(wallet_free_qty - local) / local
        if gap <= QTY_TOLERANCE_RATIO:
            return

        # Une quantite en moins = vente non enregistree ; en plus = achat non vu.
        direction = "moins" if wallet_free_qty < local else "plus"
        report.add(
            "WALLET_MISMATCH",
            f"Wallet {position.base_asset} : {wallet_free_qty} vs {local} attendu "
            f"({direction} que prevu)",
            severity="CRITICAL" if gap > 0.05 else "WARNING",
            target_type="WALLET",
            suggested_action="Comparer avec l'historique des trades Binance",
            wallet_qty=wallet_free_qty,
            local_qty=local,
        )

    # ------------------------------------------------------------------
    # Aides
    # ------------------------------------------------------------------

    @staticmethod
    def _find_order(
        entry: Entry,
        by_client_id: dict[str, dict[str, Any]],
        by_order_id: dict[int, dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if entry.client_order_id and entry.client_order_id in by_client_id:
            return by_client_id[entry.client_order_id]
        if entry.order_id and entry.order_id in by_order_id:
            return by_order_id[entry.order_id]
        return None


# ==========================================================================
# Verifications globales cote Binance
# ==========================================================================


def audit_open_orders(
    position: Position,
    open_orders: list[dict[str, Any]],
) -> list[Finding]:
    """Detecte les ordres Binance orphelins (non suivis par une position).

    Affiche dans le Dashboard : un ordre inconnu n'est jamais supprime
    automatiquement (section 53 et 54).
    """
    known_ids: set[Any] = set()
    known_client_ids: set[str] = set()

    for entry in position.entries:
        if entry.order_id:
            known_ids.add(entry.order_id)
        if entry.client_order_id:
            known_client_ids.add(entry.client_order_id)
    for tp in position.take_profits:
        if tp.order_id:
            known_ids.add(tp.order_id)
        if tp.client_order_id:
            known_client_ids.add(tp.client_order_id)
    if position.stop_loss.order_id:
        known_ids.add(position.stop_loss.order_id)
    if position.stop_loss.client_order_id:
        known_client_ids.add(position.stop_loss.client_order_id)

    findings: list[Finding] = []
    for order in open_orders:
        if order.get("orderId") in known_ids:
            continue
        if order.get("clientOrderId") in known_client_ids:
            continue
        findings.append(
            Finding(
                kind="ORPHAN_ORDER",
                severity="WARNING",
                message=(
                    f"Ordre {order.get('side')} {order.get('type')} "
                    f"{order.get('origQty')} @ {order.get('price')} non suivi "
                    f"par la position"
                ),
                symbol=position.symbol,
                position_id=position.position_id,
                target_type="ORDER",
                order_id=order.get("orderId"),
                suggested_action="Annuler depuis le Dashboard si non desire",
                data={"client_order_id": order.get("clientOrderId")},
            )
        )
    return findings
