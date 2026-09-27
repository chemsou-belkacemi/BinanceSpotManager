"""Planification hors ligne de plusieurs sorties OCO Spot Demo.

Ne place et n'annule aucun ordre. Une migration doit traiter la persistance,
les timeouts et la reprise avant d'utiliser ces parametres sur Binance.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import EntryStatus, Position, PositionStatus, SLStatus
from .symbol_rules import SymbolRules


@dataclass(frozen=True)
class OcoTranche:
    tp_id: str
    sequence_number: int
    quantity: str
    take_profit_price: str
    stop_price: str
    stop_limit_price: str

    @property
    def request_params(self) -> dict[str, str]:
        return {
            "side": "SELL",
            "quantity": self.quantity,
            "aboveType": "LIMIT_MAKER",
            "abovePrice": self.take_profit_price,
            "belowType": "STOP_LOSS_LIMIT",
            "belowStopPrice": self.stop_price,
            "belowPrice": self.stop_limit_price,
            "belowTimeInForce": "GTC",
        }


@dataclass(frozen=True)
class MultiOcoPreview:
    tranches: tuple[OcoTranche, ...]
    blockers: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return bool(self.tranches) and not self.blockers


def preview_multi_oco(
    position: Position, rules: SymbolRules, current_price: float
) -> MultiOcoPreview:
    """Répartit le BTC net reçu, jamais la quantité brute achetée."""
    blockers: list[str] = []
    if position.environment != "DEMO":
        blockers.append("OCO multiples réservés à Binance Demo")
    if position.status is not PositionStatus.ACTIVE:
        blockers.append("Position non active")
    if position.oco_exit is not None:
        blockers.append("Position déjà gérée par un OCO")
    if position.stop_loss.status is SLStatus.ACTIVE:
        blockers.append("SL indépendant actif : migration contrôlée requise")
    if rules.symbol != position.symbol or not rules.is_trading:
        blockers.append("Règles de paire indisponibles")
    if not position.entries or any(
        entry.status not in {EntryStatus.FILLED, EntryStatus.CANCELED}
        for entry in position.entries
    ):
        blockers.append("Toutes les entrées doivent être terminées")

    tps = [tp for tp in position.sorted_tps if tp.is_pending]
    if len(tps) < 2:
        blockers.append("Au moins deux TP en attente sont requis")
    if rules.max_num_algo_orders is not None and len(tps) > rules.max_num_algo_orders:
        blockers.append("Trop d'OCO pour MAX_NUM_ALGO_ORDERS")
    if rules.max_num_orders is not None and 2 * len(tps) > rules.max_num_orders:
        blockers.append("Trop d'ordres pour MAX_NUM_ORDERS")

    total_percent = sum(Decimal(str(tp.sell_percent)) for tp in tps)
    if any(tp.sell_percent <= 0 for tp in tps):
        blockers.append("Chaque TP doit vendre une part positive")
    if abs(total_percent - Decimal("100")) > Decimal("0.01"):
        blockers.append("La somme des TP doit être 100 %")

    total_qty = rules.round_qty(position.metrics.net_qty)
    if total_qty <= 0:
        blockers.append("Quantité nette vendable nulle")

    stop_target = position.stop_loss.resolved_price or 0
    stop_price = rules.round_price(stop_target, mode="down")
    stop_limit = rules.round_price(
        Decimal(str(stop_target))
        * (Decimal("1") - Decimal(str(position.stop_loss.limit_offset_percent)) / 100),
        mode="down",
    )
    if not (Decimal(str(current_price)) > stop_price > stop_limit > 0):
        blockers.append("Prix SL invalides : marché > stop > limite requis")

    tranches: list[OcoTranche] = []
    allocated = Decimal("0")
    for index, tp in enumerate(tps):
        qty = (
            total_qty - allocated if index == len(tps) - 1
            else rules.round_qty(total_qty * Decimal(str(tp.sell_percent)) / 100)
        )
        allocated += qty
        tp_price = rules.round_price(tp.target_price or 0, mode="up")
        if not (tp_price > Decimal(str(current_price)) > stop_price):
            blockers.append(f"TP {tp.sequence_number} : prix OCO invalides")
        for error in (
            rules.check_qty(qty)
            + rules.check_price(tp_price)
            + rules.check_price(stop_price)
            + rules.check_price(stop_limit)
            + rules.check_notional(tp_price, qty)
            + rules.check_notional(stop_limit, qty)
        ):
            blockers.append(f"TP {tp.sequence_number} : {error}")
        tranches.append(OcoTranche(
            tp_id=tp.tp_id,
            sequence_number=tp.sequence_number,
            quantity=rules.qty_str(qty),
            take_profit_price=rules.price_str(tp_price),
            stop_price=rules.price_str(stop_price),
            stop_limit_price=rules.price_str(stop_limit),
        ))

    return MultiOcoPreview(tuple(tranches), tuple(blockers))
