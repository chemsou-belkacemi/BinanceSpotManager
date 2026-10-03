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
