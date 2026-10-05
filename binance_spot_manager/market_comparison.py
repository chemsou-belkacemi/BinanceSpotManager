"""BSM face au marche : meme argent, memes moments, autre gestion.

Pour chaque position dont un achat a ete execute (paires en USDT seulement) :

- BSM : resultat realise + latent, frais compris (frais BNB au cours actuel, comme History) ;
- « garder les memes cryptos » : la quantite reellement recue a l'achat, gardee jusqu'a maintenant ;
- « BTC a la place » : le meme montant achete en BTC au moment du premier achat, garde jusqu'a
  maintenant (prix d'ouverture de la bougie 15 minutes qui contient ce moment).

Les deux references paient les memes frais d'achat que BSM (BNB au cours actuel), pas de frais de vente.

Limite a garder en tete : les deux references gardent chaque achat jusqu'a maintenant, alors que BSM
libere le capital plus tot et peut le reutiliser. Ce n'est ni un rendement annualise ni une preuve
qu'une methode est meilleure : quelques jours de resultats dependent surtout du marche du moment.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from .models import Position

BTC = "BTCUSDT"
QUOTE = "USDT"
INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000


@dataclass
class PositionComparison:
    symbol: str
    bought_at: datetime
    invested: float
    bsm: float
    hold: Optional[float]
    btc: Optional[float]
    is_open: bool


@dataclass
class MarketComparison:
    rows: list[PositionComparison] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)        # positions sans prix ou hors USDT

    @property
    def positions(self) -> int:
        return len(self.rows)

    @property
    def invested(self) -> float:
        return sum(r.invested for r in self.rows)

    @property
    def bsm(self) -> float:
        return sum(r.bsm for r in self.rows)

    @property
    def hold(self) -> Optional[float]:
        values = [r.hold for r in self.rows]
        return None if not values or any(v is None for v in values) else sum(v for v in values if v is not None)

    @property
    def btc(self) -> Optional[float]:
        values = [r.btc for r in self.rows]
        return None if not values or any(v is None for v in values) else sum(v for v in values if v is not None)


def first_fill_time(position: Position) -> Optional[datetime]:
    """Moment du premier achat execute (heure Binance du premier fill si connue)."""
    times: list[datetime] = []
    for entry in position.entries:
        if entry.executed_qty <= 0:
            continue
        fills = [f.time for f in entry.fills if f.time is not None]
        moment = min(fills) if fills else entry.filled_at or entry.submitted_at
        if moment is not None:
            times.append(moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc))
    return min(times) if times else None


def invested(position: Position) -> float:
    return sum(entry.quote_spent for entry in position.entries if entry.executed_qty > 0)


def held_qty(position: Position) -> float:
    """Quantite recue aux achats, frais preleves sur la crypto achetee deduits."""
    return sum(entry.net_qty for entry in position.entries if entry.executed_qty > 0)


def buy_fees(position: Position, rates: dict) -> float:
    """Frais d'achat payes dans un autre actif que la crypto achetee, en USDT (BNB au cours fourni)."""
    base, quote = position.base_asset.upper(), position.quote_asset.upper()
    total = 0.0
    for entry in position.entries:
        if entry.executed_qty <= 0:
            continue
        for fee in entry.commissions:
            asset = fee.asset.upper()
            if asset == quote:
                total += fee.amount
            elif asset != base and asset in rates:
                total += fee.amount * rates[asset]
    return total


def btc_opens(klines: Callable[..., list], start: datetime, end: datetime) -> list[tuple[int, float]]:
    """(ouverture en ms, prix d'ouverture) des bougies 15 minutes de BTC entre `start` et `end`,
    lues par pages de 1000."""
    out: list[tuple[int, float]] = []
    cursor = int(start.timestamp() * 1000) // INTERVAL_MS * INTERVAL_MS
    stop = int(end.timestamp() * 1000)
    while cursor <= stop:
        page = klines(BTC, INTERVAL, start_time=cursor, limit=1000)
        if not page:
            break
        for kline in page:
            out.append((int(kline[0]), float(kline[1])))
        following = int(page[-1][0]) + INTERVAL_MS
        if following <= cursor:
            break
        cursor = following
    return out


def price_at(opens: list[tuple[int, float]], moment: datetime) -> Optional[float]:
    """Prix d'ouverture de la bougie qui contient `moment` ; None hors des bougies connues."""
    if not opens:
        return None
    ms = int(moment.timestamp() * 1000)
    index = bisect.bisect_right([t for t, _ in opens], ms) - 1
    if index < 0 or ms - opens[index][0] >= INTERVAL_MS:
        return None
    return opens[index][1]


def compare(positions: Iterable[Position], *, price_now: Callable[[str], Optional[float]],
            fee_rates_for: Callable[[Position], dict],
            btc_at: Callable[[datetime], Optional[float]]) -> MarketComparison:
    """Comparaison position par position ; les positions d'origine ne sont pas modifiees."""
    from .position_engine import recompute_position

    result = MarketComparison()
    btc_now = price_now(BTC)
    for position in positions:
        bought_at = first_fill_time(position)
        spent = invested(position)
        if bought_at is None or spent <= 0:
            continue                                   # aucun achat execute : rien a comparer
        if position.quote_asset.upper() != QUOTE:
            result.skipped.append(f"{position.symbol} (hors {QUOTE})")
            continue
        now_price = price_now(position.symbol)
        copy = position.model_copy(deep=True)
        if copy.is_open:
            if not now_price:
                result.skipped.append(f"{position.symbol} (prix actuel indisponible)")
                continue
            copy.metrics.current_price = now_price
        try:
            rates = fee_rates_for(position) or {}
        except Exception:  # noqa: BLE001 - frais BNB alors non deduits
            rates = {}
        recompute_position(copy, fee_rates=rates)
        bsm = copy.pnl.realized + (copy.pnl.unrealized if copy.is_open else 0.0)
        fees = buy_fees(position, rates)
        hold = held_qty(copy) * now_price - spent - fees if now_price else None
        btc_then = btc_at(bought_at)
        btc = spent * (btc_now / btc_then - 1.0) - fees if btc_now and btc_then else None
        result.rows.append(PositionComparison(
            symbol=position.symbol, bought_at=bought_at, invested=spent, bsm=bsm,
            hold=hold, btc=btc, is_open=copy.is_open,
        ))
    result.rows.sort(key=lambda r: r.bought_at)
    return result
