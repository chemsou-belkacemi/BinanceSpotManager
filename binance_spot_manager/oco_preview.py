"""Prévisualisation sans écriture d'une sortie OCO Spot Demo.

Ce module ne migre pas les positions du worker actuel. Une position avec un
SL indépendant actif est volontairement bloquée pour éviter deux protections
concurrentes et un solde BTC déjà réservé.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import EntryStatus, Position, PositionStatus, SLStatus
from .symbol_rules import SymbolRules


@dataclass(frozen=True)
class OcoSellPreview:
    symbol: str
    quantity: str
    take_profit_price: str
    stop_price: str
    stop_limit_price: str
    blockers: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.blockers

    @property
    def request_params(self) -> dict[str, str]:
        """Corps de POST /api/v3/orderList/oco, sans identifiants ni signature."""
        return {
            "symbol": self.symbol,
            "side": "SELL",
            "quantity": self.quantity,
            "aboveType": "LIMIT_MAKER",
            "abovePrice": self.take_profit_price,
            "belowType": "STOP_LOSS_LIMIT",
            "belowStopPrice": self.stop_price,
            "belowPrice": self.stop_limit_price,
            "belowTimeInForce": "GTC",
        }


def preview_oco_sell(
    position: Position, rules: SymbolRules, current_price: float
) -> OcoSellPreview:
    """Calcule un OCO 100 % TP + SL sans consulter ni modifier Binance."""
    blockers: list[str] = []
    if position.environment != "DEMO":
        blockers.append("Prototype réservé aux positions Binance Demo")
    if rules.symbol != position.symbol or not rules.is_trading:
        blockers.append("Règles Binance de la paire indisponibles ou non négociables")
    if position.status is not PositionStatus.ACTIVE:
        blockers.append("Position non active")
    if position.oco_exit is not None:
        blockers.append("Position déjà gérée par un OCO")
    if not position.entries:
        blockers.append("Aucune entrée exécutée")
    if position.stop_loss.status is SLStatus.ACTIVE:
        blockers.append("SL indépendant déjà actif : migration contrôlée requise")
    if any(entry.status not in {EntryStatus.FILLED, EntryStatus.CANCELED} for entry in position.entries):
        blockers.append("Entrée encore en attente")

    pending = [tp for tp in position.take_profits if tp.is_pending]
    if len(pending) != 1 or pending[0].sell_percent < 99.99:
        blockers.append("Prototype limité à un seul TP vendant 100 % du restant")

    quantity = rules.round_qty(position.metrics.net_qty, market=False)
    tp_target = pending[0].target_price if len(pending) == 1 else 0.0
    tp_price = rules.round_price(tp_target, mode="up")
    sl_target = position.stop_loss.resolved_price or 0.0
    stop_price = rules.round_price(sl_target, mode="down")
    stop_limit = rules.round_price(
        sl_target * (1 - position.stop_loss.limit_offset_percent / 100),
        mode="down",
    )

    if quantity <= 0:
        blockers.append("Quantité vendable nulle après arrondi Binance")
    else:
        blockers.extend(rules.check_qty(quantity))
    if not (tp_price > current_price > stop_price > stop_limit > 0):
        blockers.append("Prix OCO invalides : TP > marché > stop > limite SL requis")
    else:
        blockers.extend(rules.check_price(tp_price))
        blockers.extend(rules.check_price(stop_price))
        blockers.extend(rules.check_price(stop_limit))
        blockers.extend(rules.check_notional(tp_price, quantity))
        blockers.extend(rules.check_notional(stop_limit, quantity))

    return OcoSellPreview(
        symbol=position.symbol,
        quantity=rules.qty_str(quantity),
        take_profit_price=rules.price_str(tp_price),
        stop_price=rules.price_str(stop_price),
        stop_limit_price=rules.price_str(stop_limit),
        blockers=tuple(blockers),
    )
