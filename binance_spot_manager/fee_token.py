"""Surveillance prudente du BNB utilise pour les frais Binance Demo."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .models import EventType
from .wallet_valuation import conversion_rate


@dataclass(frozen=True)
class AccountCommission:
    symbol: str
    standard: Mapping[str, float]
    tax: Mapping[str, float]
    special: Mapping[str, float]
    bnb_enabled: bool
    discount_multiplier: float

    @classmethod
    def from_response(cls, data: Mapping[str, Any], symbol: str) -> "AccountCommission":
        if not isinstance(data, Mapping) or data.get("symbol") != symbol.upper():
            raise ValueError("Commissions Binance : paire incoherente")

        def rates(name):
            source = data.get(name)
            if not isinstance(source, Mapping):
                raise ValueError("Commissions Binance incompletes")
            result = {}
            for key in ("maker", "taker", "buyer", "seller"):
                value = float(source[key])
                if not math.isfinite(value) or value < 0:
                    raise ValueError("Taux de commission Binance invalide")
                result[key] = value
            return result

        discount = data["discount"]
        multiplier = float(discount["discount"])
        if not math.isfinite(multiplier) or not 0 <= multiplier <= 1:
            raise ValueError("Reduction Binance invalide")
        enabled = (discount.get("enabledForAccount") is True
                   and discount.get("enabledForSymbol") is True
                   and discount.get("discountAsset") == "BNB")
        return cls(symbol.upper(), rates("standardCommission"), rates("taxCommission"),
                   rates("specialCommission"), enabled, multiplier)

    def percent(self, side: str, liquidity: str, *, pay_in_bnb: bool) -> float:
        """La reduction ne s'applique qu'a la commission standard."""
        if side not in {"BUY", "SELL"} or liquidity not in {"maker", "taker"}:
            raise ValueError("Sens ou liquidite invalide")
        role = "buyer" if side == "BUY" else "seller"
        standard = self.standard[liquidity] + self.standard[role]
        extra = self.tax[liquidity] + self.tax[role] + self.special[liquidity] + self.special[role]
        multiplier = self.discount_multiplier if pay_in_bnb and self.bnb_enabled else 1.0
        return (standard * multiplier + extra) * 100

    def conservative_buy_percent(self) -> float:
        return max(self.percent("BUY", kind, pay_in_bnb=True) for kind in ("maker", "taker"))


def _finite_non_negative(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0 else default


@dataclass(frozen=True)
class FeeTokenPolicy:
    """Regles locales ; elles ne changent aucun parametre du compte Binance."""

    enabled: bool = True
    alert_threshold_usdt: float = 5.0
    block_new_buys: bool = True
    estimated_fee_percent: float = 0.10
    safety_multiplier: float = 1.25

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "FeeTokenPolicy":
        values = values if isinstance(values, Mapping) else {}
        return cls(
            enabled=bool(values.get("bnb_fee_monitor_enabled", True)),
            alert_threshold_usdt=min(
                _finite_non_negative(values.get("bnb_fee_alert_threshold_usdt", 5.0), 5.0), 1_000_000.0
            ),
            block_new_buys=bool(values.get("bnb_fee_block_new_buys", True)),
            safety_multiplier=min(
                max(_finite_non_negative(values.get("bnb_fee_safety_multiplier", 1.25), 1.25), 1.0),
                10.0,
            ),
        )


@dataclass(frozen=True)
class FeeTokenAssessment:
    enabled: bool
    free_bnb: float
    locked_bnb: float
    alert_threshold_usdt: float
    estimated_required_bnb: float
    required_bnb: float
    sufficient: bool
    conversion_available: bool
    reason: str
    free_usdt: float | None = None
    locked_usdt: float | None = None
    required_usdt: float | None = None

    @property
    def low(self) -> bool:
        return self.enabled and self.conversion_available and not self.sufficient


def assess_bnb_fees(
    balances: Mapping[str, Mapping[str, float]],
    prices: Mapping[str, float],
    policy: FeeTokenPolicy,
    *,
    quote_asset: str | None = None,
    quote_notional: float = 0.0,
) -> FeeTokenAssessment:
    """Evalue le solde libre et, pour un achat, une estimation conservative."""
    amounts = balances.get("BNB", {}) if isinstance(balances, Mapping) else {}
    free = _finite_non_negative(amounts.get("free", 0), 0.0)
    locked = _finite_non_negative(amounts.get("locked", 0), 0.0)
    if not policy.enabled:
        return FeeTokenAssessment(False, free, locked, policy.alert_threshold_usdt, 0, 0, True, True,
                                  "Surveillance BNB desactivee")

    notional = _finite_non_negative(quote_notional, 0.0)
    estimated = 0.0
    usdt_rate = conversion_rate("BNB", "USDT", prices)
    conversion_available = usdt_rate is not None
    if notional > 0 and quote_asset and policy.estimated_fee_percent > 0:
        rate = conversion_rate("BNB", quote_asset.upper(), prices)
        if rate is None or rate <= 0:
            conversion_available = False
        else:
            fee_in_quote = notional * policy.estimated_fee_percent / 100 * policy.safety_multiplier
            estimated = fee_in_quote / rate

    threshold_bnb = policy.alert_threshold_usdt / usdt_rate if usdt_rate else 0
    required = max(threshold_bnb, estimated)
    free_usdt = free * usdt_rate if usdt_rate else None
    locked_usdt = locked * usdt_rate if usdt_rate else None
    required_usdt = required * usdt_rate if usdt_rate and conversion_available else None
    sufficient = conversion_available and free >= required
    if not conversion_available:
        reason = "Cours de conversion indisponible : reserve de frais non verifiable"
    elif free < required:
        reason = f"Reserve de frais insuffisante : {free_usdt:.2f} USDT disponibles, {required_usdt:.2f} USDT requis"
    else:
        reason = f"Reserve de frais suffisante : {free_usdt:.2f} USDT disponibles"
    return FeeTokenAssessment(
        True, free, locked, policy.alert_threshold_usdt, estimated, required,
        sufficient, conversion_available, reason, free_usdt, locked_usdt, required_usdt,
    )


class FeeTokenMonitor:
    """Controle periodique avec alerte uniquement lors d'un changement d'etat."""

    def __init__(
        self,
        client,
        events,
        settings_supplier: Callable[[], Mapping[str, Any]],
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        self.client = client
        self.events = events
        self.settings_supplier = settings_supplier
        self.interval_seconds = max(float(interval_seconds), 5.0)
        self._last_check = 0.0
        self._was_low: bool | None = None
        self._unavailable = False

    def check(self, *, force: bool = False) -> FeeTokenAssessment | None:
        now = time.monotonic()
        if not force and now - self._last_check < self.interval_seconds:
            return None
        self._last_check = now
        policy = FeeTokenPolicy.from_mapping(self.settings_supplier())
        if not policy.enabled:
            self._was_low = None
            return assess_bnb_fees({}, {}, policy)

        try:
            assessment = assess_bnb_fees(
                self.client.get_balances(), {"BNBUSDT": self.client.get_price("BNBUSDT")}, policy,
            )
            if not assessment.conversion_available:
                raise ValueError(assessment.reason)
        except Exception:
            # Une panne du controle de reserve ne doit pas interrompre le suivi TP/SL.
            if not self._unavailable:
                self.events.append(EventType.ERROR, "Reserve de frais en USDT indisponible : lecture Binance impossible",
                                   level="WARNING")
            self._unavailable = True
            return None
        self._unavailable = False
        if assessment.low and self._was_low is not True:
            self.events.append(
                EventType.FEE_TOKEN_LOW,
                f"Alerte reserve de frais : {assessment.reason}",
                level="WARNING",
                free_bnb=assessment.free_bnb,
                locked_bnb=assessment.locked_bnb,
                free_usdt=assessment.free_usdt,
                threshold_usdt=assessment.alert_threshold_usdt,
            )
        elif not assessment.low and self._was_low is True:
            self.events.append(
                EventType.FEE_TOKEN_RECOVERED,
                f"Reserve de frais retablie : {assessment.free_usdt:.2f} USDT disponibles",
                level="INFO",
                free_bnb=assessment.free_bnb,
                free_usdt=assessment.free_usdt,
                threshold_usdt=assessment.alert_threshold_usdt,
            )
        self._was_low = assessment.low
        return assessment
