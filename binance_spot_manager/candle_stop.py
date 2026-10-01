"""Stop loss a la cloture de bougie : decisions pures, sans reseau ni ecriture.

Un SL « 0.02446 (15m) » ne se declenche pas quand le prix touche 0.02446, mais quand une
bougie 15m CLOTURE a 0.02446 ou dessous. Aucun ordre stop n'est donc place sur Binance (il se
declencherait au toucher) : le worker lit les bougies cloturees et vend au marche si besoin.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Optional

MINUTE_MS = 60_000
#: intervalles Binance acceptes et leur duree (le mois n'a pas de duree fixe : refuse)
INTERVAL_MS = {
    "1m": MINUTE_MS, "3m": 3 * MINUTE_MS, "5m": 5 * MINUTE_MS, "15m": 15 * MINUTE_MS,
    "30m": 30 * MINUTE_MS, "1h": 60 * MINUTE_MS, "2h": 120 * MINUTE_MS, "4h": 240 * MINUTE_MS,
    "6h": 360 * MINUTE_MS, "8h": 480 * MINUTE_MS, "12h": 720 * MINUTE_MS,
    "1d": 1440 * MINUTE_MS, "3d": 4320 * MINUTE_MS, "1w": 10080 * MINUTE_MS,
}
UNITS = {"m": "m", "min": "m", "mins": "m", "minute": "m", "minutes": "m",
         "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
         "d": "d", "day": "d", "days": "d", "w": "w", "week": "w", "weeks": "w"}
#: marge apres la fin theorique d'une bougie avant de la lire (publication Binance)
CLOSE_GRACE_MS = 2_000


def kline_interval(timeframe: str) -> Optional[str]:
    """« 15min », « 1H », « 4h » -> intervalle Binance ; None si inconnu (« bougie », « 2d »)."""
    match = re.fullmatch(r"\s*(\d{1,3})\s*([a-zA-Z]+)\s*", str(timeframe or ""))
    if not match:
        return None
    unit = UNITS.get(match[2].lower())
    interval = f"{int(match[1])}{unit}" if unit else None
    return interval if interval in INTERVAL_MS else None


@dataclass(frozen=True)
class CandleBreach:
    close_price: float
    close_time: int


def next_check_due(checked_until: Optional[int], interval: str, now_ms: int) -> bool:
    """Faux tant qu'aucune nouvelle bougie n'a pu cloturer depuis la derniere lecture."""
    if checked_until is None:
        return True
    return now_ms >= checked_until + INTERVAL_MS[interval] + CLOSE_GRACE_MS


def evaluate_closed_candles(
    klines: list[list[Any]], *, stop_price: float, armed_at_ms: int, now_ms: int
) -> tuple[Optional[CandleBreach], Optional[int]]:
    """(premiere bougie cloturee au stop ou dessous, closeTime de la derniere bougie cloturee lue).

    Seules comptent les bougies cloturees APRES l'armement du SL (une bougie anterieure au trade
    ne doit pas le fermer) et dont la fin est passee : la bougie en cours n'est jamais jugee.
    Une donnee illisible leve ValueError plutot que d'etre ignoree.
    """
    latest = None
    for kline in klines:
        try:
            close_time, close = int(kline[6]), float(kline[4])
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError("bougie Binance illisible") from exc
        if not math.isfinite(close) or close <= 0:
            raise ValueError("cloture de bougie invalide")
        if close_time >= now_ms:
            break  # bougie encore ouverte
        latest = close_time
        if close_time > armed_at_ms and close <= stop_price:
            return CandleBreach(close_price=close, close_time=close_time), latest
    return None, latest
