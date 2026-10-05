"""Valorisation des frais payes dans un troisieme actif (BNB le plus souvent), au cours Binance actuel.

Partage entre l'historique (pages/4_History.py via DashboardService) et les notifications de fin de
position : un meme PnL, frais compris, des deux cotes.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from .models import Position

logger = logging.getLogger(__name__)


def external_fee_assets(position: Position) -> set[str]:
    """Actifs de commission autres que la base et la cotation de la position."""
    items = [*position.entries, *position.take_profits, position.stop_loss, *position.manual_exits]
    return {
        fee.asset.upper() for item in items for fee in item.commissions
        if fee.amount > 0 and fee.asset.upper() not in {
            position.base_asset.upper(), position.quote_asset.upper()
        }
    }


def fee_rates(position: Position, price_of: Callable[[str], Optional[float]]) -> dict[str, float]:
    """Cours actuels des actifs de commission, exprimes dans la cotation de la position.

    `price_of(symbole)` rend le dernier prix (ou None) ; une paire absente est essayee dans l'autre
    sens. Un cours indisponible est simplement omis : l'appelant le signale.
    """
    quote = position.quote_asset.upper()
    rates: dict[str, float] = {}
    for asset in external_fee_assets(position):
        try:
            direct = price_of(f"{asset}{quote}")
        except Exception as exc:  # noqa: BLE001 - affichage indicatif, jamais bloquant
            logger.warning("Taux de frais %s/%s indisponible : %s", asset, quote, exc)
            direct = None
        if direct is not None and direct > 0:
            rates[asset] = direct
            continue
        try:
            inverse = price_of(f"{quote}{asset}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Taux de frais %s/%s indisponible : %s", quote, asset, exc)
            inverse = None
        if inverse is not None and inverse > 0:
            rates[asset] = 1.0 / inverse
    return rates
