"""Valorisation indicative du portefeuille Spot Demo, sans ordre ni écriture."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Mapping


BRIDGES = ("USDT", "USDC", "BTC", "EUR", "ETH", "BNB", "FDUSD", "SOL")


def conversion_rate(
    asset: str, target: str, prices: Mapping[str, float]
) -> float | None:
    """Prix direct/inverse ou via un seul actif pivot, jamais un taux inventé."""
    asset, target = asset.upper(), target.upper()
    if asset == target:
        return 1.0

    def direct(base: str, quote: str) -> float | None:
        forward = float(prices.get(base + quote) or 0)
        if isfinite(forward) and forward > 0:
            return forward
        inverse = float(prices.get(quote + base) or 0)
        if isfinite(inverse) and inverse > 0:
            return 1 / inverse
        return None

    rate = direct(asset, target)
    if rate is not None:
        return rate
    for bridge in BRIDGES:
        if bridge in {asset, target}:
            continue
        first, second = direct(asset, bridge), direct(bridge, target)
        if first is not None and second is not None:
            return first * second
    return None


@dataclass(frozen=True)
class WalletAsset:
    asset: str
    free: float
    locked: float
    total: float
    price_usdt: float | None
    price_eur: float | None
    value_usdt: float | None
    value_eur: float | None


@dataclass(frozen=True)
class WalletValuation:
    assets: tuple[WalletAsset, ...]
    total_usdt: float
    total_eur: float
    unpriced_usdt: tuple[str, ...]
    unpriced_eur: tuple[str, ...]
    valued_at: datetime

    @property
    def complete(self) -> bool:
        return not self.unpriced_usdt and not self.unpriced_eur


def value_wallet(
    balances: Mapping[str, Mapping[str, float]], prices: Mapping[str, float]
) -> WalletValuation:
    rows: list[WalletAsset] = []
    missing_usdt: list[str] = []
    missing_eur: list[str] = []
    for asset, amounts in balances.items():
        free = float(amounts.get("free") or 0)
        locked = float(amounts.get("locked") or 0)
        total = free + locked
        if total <= 0:
            continue
        usdt = conversion_rate(asset, "USDT", prices)
        eur = conversion_rate(asset, "EUR", prices)
        if usdt is None:
            missing_usdt.append(asset)
        if eur is None:
            missing_eur.append(asset)
        rows.append(WalletAsset(
            asset=asset, free=free, locked=locked, total=total,
            price_usdt=usdt, price_eur=eur,
            value_usdt=total * usdt if usdt is not None else None,
            value_eur=total * eur if eur is not None else None,
        ))
    rows.sort(key=lambda row: (row.value_usdt or 0), reverse=True)
    return WalletValuation(
        assets=tuple(rows),
        total_usdt=sum(row.value_usdt or 0 for row in rows),
        total_eur=sum(row.value_eur or 0 for row in rows),
        unpriced_usdt=tuple(sorted(missing_usdt)),
        unpriced_eur=tuple(sorted(missing_eur)),
        valued_at=datetime.now(timezone.utc),
    )
