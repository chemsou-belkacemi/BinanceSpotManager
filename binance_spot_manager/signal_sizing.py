"""Safe, user-configurable budget suggestions for reviewed signals.

The policy proposes the budget of a manual confirmation and sets the budget of
an automatic one. A signal with any review reason (high risk, low or unknown
confidence) always requires the explicit Demo confirmation on the Signals page.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite
from typing import Mapping, Any

from .wallet_valuation import conversion_rate, value_wallet


SIZING_MODES = {"FIXED", "PERCENT", "ADAPTIVE"}


def _flag(mapping: Mapping[str, Any], key: str, default: bool) -> bool:
    value = mapping.get(key, default)
    return value if isinstance(value, bool) else default


def _number(mapping: Mapping[str, Any], key: str, default: float,
            minimum: float, maximum: float) -> float:
    try:
        value = float(mapping.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if isfinite(value) and minimum <= value <= maximum else default


@dataclass(frozen=True)
class SignalSizingPolicy:
    mode: str = "ADAPTIVE"
    fixed_budget: float = 100.0
    capital_percent: float = 5.0
    low_balance_threshold_percent: float = 30.0
    low_balance_budget_percent: float = 2.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "SignalSizingPolicy":
        values = values if isinstance(values, Mapping) else {}
        mode = str(values.get("signal_sizing_mode", "ADAPTIVE")).upper()
        if mode not in SIZING_MODES:
            mode = "ADAPTIVE"
        return cls(
            mode=mode,
            fixed_budget=_number(values, "signal_fixed_budget", 100.0, 0.01, 1_000_000_000),
            capital_percent=_number(values, "signal_capital_percent", 5.0, 0.01, 100.0),
            low_balance_threshold_percent=_number(
                values, "signal_low_balance_threshold_percent", 30.0, 0.01, 100.0,
            ),
            low_balance_budget_percent=_number(
                values, "signal_low_balance_budget_percent", 2.0, 0.01, 100.0,
            ),
        )


@dataclass(frozen=True)
class SignalBudgetSuggestion:
    budget: float
    requested_budget: float
    applied_percent: float | None
    free_capital_percent: float
    total_capital: float
    available_quote: float
    usable_quote: float
    reduced: bool
    capped_by_reserve: bool


def suggest_signal_budget(policy: SignalSizingPolicy, *, available_quote: float,
                          total_capital: float, reserve_percent: float) -> SignalBudgetSuggestion:
    """Resolve a quote-asset budget without ever consuming the configured reserve."""
    available = max(float(available_quote), 0.0)
    total = max(float(total_capital), available, 0.0)
    reserve = min(max(float(reserve_percent), 0.0), 100.0)
    usable = available * (1 - reserve / 100)
    free_percent = available / total * 100 if total > 0 else 0.0

    reduced = policy.mode == "ADAPTIVE" and free_percent < policy.low_balance_threshold_percent
    applied_percent: float | None = None
    if policy.mode == "FIXED":
        requested = policy.fixed_budget
    else:
        applied_percent = (
            policy.low_balance_budget_percent if reduced else policy.capital_percent
        )
        requested = total * applied_percent / 100

    capped = requested > usable
    # Stablecoin budgets are displayed and submitted to the cent; always round down.
    budget = floor(max(min(requested, usable), 0.0) * 100) / 100
    return SignalBudgetSuggestion(
        budget=budget,
        requested_budget=requested,
        applied_percent=applied_percent,
        free_capital_percent=free_percent,
        total_capital=total,
        available_quote=available,
        usable_quote=usable,
        reduced=reduced,
        capped_by_reserve=capped,
    )


def suggest_signal_budget_from_account(policy: SignalSizingPolicy, *, balances,
                                       prices, quote_asset: str,
                                       reserve_percent: float) -> tuple[SignalBudgetSuggestion, tuple[str, ...]]:
    """Value the Spot wallet in the signal quote asset, then apply the policy."""
    quote_asset = quote_asset.upper()
    wallet = value_wallet(balances, prices)
    usdt_to_quote = conversion_rate("USDT", quote_asset, prices)
    if usdt_to_quote is None:
        raise ValueError(f"Conversion USDT/{quote_asset} indisponible pour calculer le budget")
    total_quote = wallet.total_usdt * usdt_to_quote
    available_quote = float(balances.get(quote_asset, {}).get("free") or 0)
    suggestion = suggest_signal_budget(
        policy,
        available_quote=available_quote,
        total_capital=total_quote,
        reserve_percent=reserve_percent,
    )
    return suggestion, wallet.unpriced_usdt


# ==========================================================================
# Taille selon le risque (désactivée par défaut, Settings → Signaux)
# ==========================================================================


@dataclass(frozen=True)
class RiskSizingPolicy:
    """Budget calculé pour perdre le même montant à chaque stop : risque × capital ÷ distance du stop.

    Désactivée par défaut : le budget vient alors de SignalSizingPolicy. Activée, le budget remplace celui de la
    stratégie de budget (part réduite ADAPTIVE comprise), borné par un plafond en % du capital et par la réserve.
    La perte visée est hors frais : le contrôle « risque au stop » (frais compris) s'applique ensuite. Un signal
    sans stop utilisable n'est pas dimensionnable : il part en revue, jamais au budget par défaut."""

    enabled: bool = False
    risk_percent: float = 0.4            # sous le seuil de revue « risque au stop » (0,5 % par défaut), frais compris
    max_budget_percent: float = 20.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "RiskSizingPolicy":
        values = values if isinstance(values, Mapping) else {}
        return cls(
            enabled=_flag(values, "signal_risk_sizing_enabled", False),
            risk_percent=_number(values, "signal_risk_percent", 0.4, 0.01, 5.0),
            max_budget_percent=_number(values, "signal_risk_max_budget_percent", 20.0, 1.0, 100.0),
        )


@dataclass(frozen=True)
class RiskBudget:
    budget: float
    risk_amount: float            # perte visée au stop, hors frais, en devise de cotation
    stop_distance_pct: float      # (entrée moyenne − stop) ÷ entrée moyenne
    average_entry: float
    cap_budget: float             # plafond : max_budget_percent du capital
    capped_by: str                # "" | "plafond" | "réserve"


def sizing_stop(stop, *, candle_close: bool, backup_percent: float):
    """Prix où la perte est bornée : le stop lui-même au toucher ; pour un SL à la clôture de bougie, le stop de
    secours chez Binance (backup_percent sous le niveau, même calcul que StopLoss.binance_stop_price), car la vente
    à la clôture peut se faire jusque-là. Sans stop de secours, la perte n'a pas de borne : None."""
    if stop is None:
        return None
    if not candle_close:
        return float(stop)
    if not backup_percent or backup_percent <= 0:
        return None
    return float(stop) * (1.0 - min(float(backup_percent), 50.0) / 100.0)


def average_entry_price(entries, weights=None) -> float:
    """Entrée moyenne pondérée par les parts (en %, même ordre), sinon à parts égales. Une entrée invalide rend le
    calcul impossible (0) plutôt que de décaler les parts."""
    prices = [float(e) for e in entries]
    if not prices or not all(isfinite(price) and price > 0 for price in prices):
        return 0.0
    parts = [float(w) for w in (weights or [])]
    if len(parts) != len(prices) or sum(parts) <= 0:
        parts = [1.0] * len(prices)
    # Pour un même montant par entrée, la quantité achetée est budget×part ÷ prix : prix moyen = Σparts ÷ Σ(part/prix).
    return sum(parts) / sum(part / price for part, price in zip(parts, prices))


def risk_based_budget(policy: RiskSizingPolicy, *, entries, stop, total_capital: float, usable_quote: float,
                      weights=None) -> RiskBudget | None:
    """Budget tel que la perte au stop (hors frais) vaille risk_percent du capital ; None si non calculable
    (réglage inactif, capital nul, stop absent ou au-dessus de l'entrée moyenne)."""
    if not policy.enabled:
        return None
    total = float(total_capital)
    average = average_entry_price(entries, weights)
    if not (isfinite(total) and total > 0 and average > 0 and stop is not None):
        return None
    stop_price = float(stop)
    if not (isfinite(stop_price) and 0 < stop_price < average):
        return None
    distance = (average - stop_price) / average
    risk_amount = total * policy.risk_percent / 100
    wanted = risk_amount / distance
    cap = total * policy.max_budget_percent / 100
    usable = max(float(usable_quote), 0.0)
    budget, capped_by = wanted, ""
    if budget > cap:
        budget, capped_by = cap, "plafond"
    if budget > usable:
        budget, capped_by = usable, "réserve"
    return RiskBudget(budget=floor(max(budget, 0.0) * 100) / 100, risk_amount=risk_amount,
                      stop_distance_pct=distance * 100, average_entry=average, cap_budget=cap, capped_by=capped_by)


# ==========================================================================
# Trader ou canal perdant : « à confirmer » ou taille réduite (désactivé par défaut)
# ==========================================================================

CHANNEL_ACTIONS = {"REVIEW", "REDUCE"}


@dataclass(frozen=True)
class ChannelPolicy:
    """Règle du trader ou canal perdant (Settings → Signaux → Canal perdant).

    Jugé perdant : au moins `min_trades` positions terminées et un résultat net (frais compris) inférieur ou égal à
    −`min_loss`. Action : REVIEW (ses signaux passent « à confirmer ») ou REDUCE (ils partent avec `kept_percent` %
    du budget, et la réduction est notée dans les métriques du routage)."""

    enabled: bool = False
    min_trades: int = 30
    min_loss: float = 0.0
    action: str = "REVIEW"
    kept_percent: float = 50.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "ChannelPolicy":
        values = values if isinstance(values, Mapping) else {}
        action = str(values.get("signal_channel_action", "REVIEW")).upper()
        return cls(
            enabled=_flag(values, "signal_channel_review_enabled", False),
            min_trades=int(_number(values, "signal_channel_review_min_trades", 30, 10, 500)),
            min_loss=_number(values, "signal_channel_min_loss", 0.0, 0.0, 1_000_000_000),
            action=action if action in CHANNEL_ACTIONS else "REVIEW",
            kept_percent=_number(values, "signal_channel_kept_percent", 50.0, 10.0, 90.0),
        )

    def reduced(self, budget: float) -> float:
        return floor(max(float(budget), 0.0) * self.kept_percent / 100 * 100) / 100
