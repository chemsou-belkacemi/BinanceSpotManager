"""Filtres Binance : arrondis tickSize / stepSize et validations minQty / minNotional.

Aucune valeur n'est codee en dur : tout provient de /api/v3/exchangeInfo.
Les calculs passent par Decimal pour eviter les erreurs de flottant
(0.1 + 0.2, notation scientifique, etc.).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Optional

from .binance_client import BinanceSpotClient

#: Duree de vie du cache exchangeInfo par symbole (secondes).
CACHE_TTL_SECONDS = 3600


def _d(value: Any) -> Decimal:
    """Conversion robuste vers Decimal (accepte str, int, float, Decimal)."""
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Prix ou quantite non fini")
    return result


def format_decimal(value: Decimal) -> str:
    """Formate sans notation scientifique et sans zeros inutiles.

    Binance refuse "1E-5" : il faut "0.00001".
    """
    quantized = value.normalize()
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _step_decimals(step: Decimal) -> int:
    """Nombre de decimales significatives d'un pas (0.001 -> 3)."""
    step = step.normalize()
    exponent = step.as_tuple().exponent
    return max(0, -int(exponent))


class SymbolRulesError(ValueError):
    """Paire inexistante ou filtres illisibles."""


@dataclass(frozen=True)
class SymbolRules:
    """Filtres utiles d'un symbole Spot."""

    symbol: str
    base_asset: str
    quote_asset: str
    status: str

    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal

    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal

    market_step_size: Optional[Decimal] = None
    market_min_qty: Optional[Decimal] = None
    market_max_qty: Optional[Decimal] = None

    min_notional: Decimal = Decimal("0")
    max_num_orders: Optional[int] = None
    max_num_algo_orders: Optional[int] = None

    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # -- proprietes -----------------------------------------------------

    @property
    def is_trading(self) -> bool:
        return self.status.upper() == "TRADING"

    @property
    def price_decimals(self) -> int:
        return _step_decimals(self.tick_size)

    @property
    def qty_decimals(self) -> int:
        return _step_decimals(self.step_size)

    # -- arrondis -------------------------------------------------------

    def round_price(self, price: Any, *, mode: str = "nearest") -> Decimal:
        """Arrondit un prix sur le tickSize.

        mode : "nearest" (defaut), "down" (achat prudent), "up" (vente prudente).
        """
        value = _d(price)
        if self.tick_size <= 0:
            return value
        rounding = {
            "nearest": ROUND_HALF_UP,
            "down": ROUND_FLOOR,
            "up": ROUND_CEILING,
        }.get(mode, ROUND_HALF_UP)
        steps = (value / self.tick_size).to_integral_value(rounding=rounding)
        return (steps * self.tick_size).quantize(self.tick_size)

    def round_qty(self, qty: Any, *, market: bool = False) -> Decimal:
        """Arrondit une quantite vers le BAS sur le stepSize.

        Toujours vers le bas : ne jamais vendre/acheter plus que prevu.
        """
        value = _d(qty)
        step = self.market_step_size if (market and self.market_step_size) else self.step_size
        if step is None or step <= 0:
            return value
        steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
        return (steps * step).quantize(step)

    def price_str(self, price: Any, *, mode: str = "nearest") -> str:
        return format_decimal(self.round_price(price, mode=mode))

    def qty_str(self, qty: Any, *, market: bool = False) -> str:
        return format_decimal(self.round_qty(qty, market=market))

    # -- validations ----------------------------------------------------

    def check_price(self, price: Any) -> list[str]:
        value = _d(price)
        errors: list[str] = []
        if value <= 0:
            errors.append("prix <= 0")
            return errors
        if self.min_price > 0 and value < self.min_price:
            errors.append(f"prix < minPrice ({format_decimal(self.min_price)})")
        if self.max_price > 0 and value > self.max_price:
            errors.append(f"prix > maxPrice ({format_decimal(self.max_price)})")
        if self.tick_size > 0 and (value % self.tick_size) != 0:
            errors.append(f"prix non aligne sur tickSize ({format_decimal(self.tick_size)})")
        return errors

    def check_qty(self, qty: Any, *, market: bool = False) -> list[str]:
        value = _d(qty)
        errors: list[str] = []
        min_qty = self.market_min_qty if (market and self.market_min_qty) else self.min_qty
        max_qty = self.market_max_qty if (market and self.market_max_qty) else self.max_qty
        step = self.market_step_size if (market and self.market_step_size) else self.step_size

        if value <= 0:
            errors.append("quantite <= 0")
            return errors
        if min_qty and value < min_qty:
            errors.append(f"quantite < minQty ({format_decimal(min_qty)})")
        if max_qty and max_qty > 0 and value > max_qty:
            errors.append(f"quantite > maxQty ({format_decimal(max_qty)})")
        if step and step > 0 and (value % step) != 0:
            errors.append(f"quantite non alignee sur stepSize ({format_decimal(step)})")
        return errors

    def check_notional(self, price: Any, qty: Any) -> list[str]:
        notional = _d(price) * _d(qty)
        if self.min_notional > 0 and notional < self.min_notional:
            return [
                f"notional {format_decimal(notional)} < minNotional "
                f"({format_decimal(self.min_notional)})"
            ]
        return []

    def validate_order(self, price: Any, qty: Any, *, market: bool = False) -> list[str]:
        """Valide un couple (prix, quantite). Retourne la liste des erreurs."""
        errors: list[str] = []
        if not market:
            errors += self.check_price(price)
        errors += self.check_qty(qty, market=market)
        errors += self.check_notional(price, qty)
        return errors

    def below_minimums(self, qty: Any, price: Any, *, market: bool = False) -> bool:
        """Reliquat trop petit pour un ordre Binance : nul une fois arrondi au stepSize, sous minQty, ou sous
        minNotional au prix donne. Ni vendable ni protegeable par un stop. Prix inconnu (<= 0) : seules les
        quantites comptent, jamais une supposition sur la valeur."""
        quantity = self.round_qty(qty, market=market)
        if quantity <= 0:
            return True
        min_qty = self.market_min_qty if (market and self.market_min_qty) else self.min_qty
        if min_qty and quantity < min_qty:
            return True
        try:
            price_d = _d(price) if price else Decimal("0")
        except (ArithmeticError, ValueError):
            price_d = Decimal("0")
        return bool(self.min_notional > 0 and price_d > 0 and quantity * price_d < self.min_notional)

    # -- aides de sizing ------------------------------------------------

    def qty_for_quote(self, quote_amount: Any, price: Any, *, market: bool = False) -> Decimal:
        """Quantite achetable avec `quote_amount`, arrondie au stepSize."""
        price_d = _d(price)
        if price_d <= 0:
            return Decimal("0")
        return self.round_qty(_d(quote_amount) / price_d, market=market)

    def min_quote_for_order(self, price: Any) -> Decimal:
        """Montant quote minimum respectant a la fois minQty et minNotional."""
        price_d = _d(price)
        by_qty = self.min_qty * price_d
        return max(by_qty, self.min_notional)


def _filters_by_type(symbol_info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f.get("filterType", ""): f for f in symbol_info.get("filters", [])}


def parse_symbol_rules(symbol_info: dict[str, Any]) -> SymbolRules:
    """Construit un SymbolRules depuis un bloc `symbols[0]` d'exchangeInfo."""
    filters = _filters_by_type(symbol_info)

    price_filter = filters.get("PRICE_FILTER", {})
    lot = filters.get("LOT_SIZE", {})
    market_lot = filters.get("MARKET_LOT_SIZE", {})
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}

    min_notional_raw = notional.get("minNotional") or notional.get("notional") or "0"

    max_orders = filters.get("MAX_NUM_ORDERS", {}).get("maxNumOrders")
    max_algo = filters.get("MAX_NUM_ALGO_ORDERS", {}).get("maxNumAlgoOrders")

    return SymbolRules(
        symbol=symbol_info["symbol"].upper(),
        base_asset=symbol_info.get("baseAsset", "").upper(),
        quote_asset=symbol_info.get("quoteAsset", "").upper(),
        status=symbol_info.get("status", "UNKNOWN"),
        tick_size=_d(price_filter.get("tickSize", "0")),
        min_price=_d(price_filter.get("minPrice", "0")),
        max_price=_d(price_filter.get("maxPrice", "0")),
        step_size=_d(lot.get("stepSize", "0")),
        min_qty=_d(lot.get("minQty", "0")),
        max_qty=_d(lot.get("maxQty", "0")),
        market_step_size=_d(market_lot["stepSize"]) if market_lot.get("stepSize") else None,
        market_min_qty=_d(market_lot["minQty"]) if market_lot.get("minQty") else None,
        market_max_qty=_d(market_lot["maxQty"]) if market_lot.get("maxQty") else None,
        min_notional=_d(min_notional_raw),
        max_num_orders=int(max_orders) if max_orders is not None else None,
        max_num_algo_orders=int(max_algo) if max_algo is not None else None,
        raw=symbol_info,
    )


class SymbolRulesCache:
    """Cache TTL des filtres par symbole (evite de spammer exchangeInfo)."""

    def __init__(self, client: BinanceSpotClient, ttl: int = CACHE_TTL_SECONDS) -> None:
        self._client = client
        self._ttl = ttl
        self._cache: dict[str, tuple[float, SymbolRules]] = {}
        self._missing: dict[str, float] = {}

    def get(self, symbol: str, *, refresh: bool = False) -> SymbolRules:
        """Retourne les filtres. Leve SymbolRulesError si la paire n'existe pas."""
        key = symbol.strip().upper()
        now = time.time()

        if not refresh:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self._ttl:
                return cached[1]
            missed_at = self._missing.get(key)
            if missed_at and now - missed_at < 60:
                raise SymbolRulesError(f"Paire inexistante sur Binance : {key}")

        info = self._client.get_symbol_info(key)
        if not info:
            self._missing[key] = now
            raise SymbolRulesError(f"Paire inexistante sur Binance : {key}")

        rules = parse_symbol_rules(info)
        self._cache[key] = (now, rules)
        self._missing.pop(key, None)
        return rules

    def try_get(self, symbol: str) -> Optional[SymbolRules]:
        """Variante non levante — pratique pour la validation d'un champ UI."""
        try:
            return self.get(symbol)
        except Exception:
            return None

    def clear(self) -> None:
        self._cache.clear()
        self._missing.clear()
