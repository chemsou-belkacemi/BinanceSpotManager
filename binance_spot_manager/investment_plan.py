"""Plan simple d'investissement Spot Demo avec une seule sortie autonome.

Deux choix exclusifs : TP limit GTC ou SL stop-limit. Aucun faux SL à zéro.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .models import (
    Entry, EventType, OrderType, Position, PositionStatus, PriceMode,
    SLMode, SLStatus, TPExecutionPolicy, TakeProfit,
)
from .symbol_rules import SymbolRules
from .risk_engine import PortfolioSnapshot, RiskEngine
from .strategy_engine import StrategyPlan
from .wallet_valuation import conversion_rate, value_wallet


@dataclass(frozen=True)
class InvestmentPreview:
    symbol: str
    base_asset: str
    quote_asset: str
    exit_mode: str
    quantity: float
    estimated_spend: float
    exit_price: float
    risk_quote: float
    errors: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class SimpleBuyPreview:
    symbol: str
    base_asset: str
    quote_asset: str
    quantity: float
    estimated_spend: float
    errors: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.errors


def investment_risk_context(
    preview: InvestmentPreview, *, capital: float, reserve_percent: float,
    balances: Mapping[str, Mapping[str, float]], prices: Mapping[str, float],
    positions: list[Position],
) -> tuple[StrategyPlan, PortfolioSnapshot, float]:
    """Valorise le plan et toutes les positions en USDT, sans parite supposee."""
    quote_rate = conversion_rate(preview.quote_asset, "USDT", prices)
    if quote_rate is None or quote_rate <= 0:
        raise ValueError(f"Taux {preview.quote_asset}/USDT indisponible sur Binance Demo")
    wallet = value_wallet(balances, prices)
    if wallet.unpriced_usdt:
        raise ValueError(
            "Valorisation USDT impossible pour : " + ", ".join(wallet.unpriced_usdt)
        )
    if wallet.total_usdt <= 0:
        raise ValueError("Portefeuille Binance Demo non valorisable en USDT")
    available_quote = float(balances.get(preview.quote_asset, {}).get("free") or 0)
    if available_quote < 0:
        raise ValueError("Solde libre invalide")
    quote_rates: dict[str, float] = {}
    for position in positions:
        if not position.is_open:
            continue
        rate = conversion_rate(position.quote_asset, "USDT", prices)
        if rate is None or rate <= 0:
            raise ValueError(f"Taux {position.quote_asset}/USDT indisponible pour le risque")
        quote_rates[position.quote_asset] = rate
    snapshot = RiskEngine.snapshot(
        positions, available_quote * quote_rate, quote_rates=quote_rates,
    )
    # Le portefeuille entier sert de denominateur, y compris les autres actifs
    # Spot ; le solde libre dans la devise achetee reste la base de la reserve.
    snapshot.capital_committed = max(wallet.total_usdt - snapshot.quote_balance, 0.0)
    snapshot.capital_pending = 0.0
    plan = StrategyPlan(
        symbol=preview.symbol,
        quote_asset="USDT",
        capital_total=capital * quote_rate,
        capital_reserved=available_quote * reserve_percent / 100 * quote_rate,
        loss_max_estimated=-preview.risk_quote * quote_rate,
        errors=list(preview.errors),
    )
    return plan, snapshot, quote_rate


def preview_simple_buy(
    rules: SymbolRules, *, current_price: float, capital: float,
    available_quote: float, reserve_percent: float,
) -> SimpleBuyPreview:
    """Achat Market seul : le budget est libellé dans l'actif de cotation."""
    errors: list[str] = []
    if not rules.is_trading:
        errors.append("Paire non négociable")
    if current_price <= 0 or capital <= 0:
        errors.append("Prix ou budget invalide")
    if capital > max(available_quote * (1 - reserve_percent / 100), 0) + 1e-9:
        errors.append(f"Solde {rules.quote_asset} insuffisant après réserve")
    quantity = float(rules.round_qty(capital * 0.98 / current_price, market=True)) if current_price > 0 else 0.0
    if quantity <= 0:
        errors.append("Quantité achetable nulle")
    else:
        errors.extend(rules.check_qty(quantity, market=True))
        errors.extend(rules.check_notional(current_price, quantity))
    return SimpleBuyPreview(
        rules.symbol, rules.base_asset, rules.quote_asset,
        quantity, quantity * current_price, tuple(errors),
    )


def preview_investment(
    rules: SymbolRules, *, current_price: float, capital: float,
    available_quote: float, reserve_percent: float, exit_mode: str,
    exit_price: float,
) -> InvestmentPreview:
    """Valide le montant et la sortie, avec une marge sur l'achat Market."""
    errors: list[str] = []
    if exit_mode not in {"TP_ONLY", "SL_ONLY"}:
        errors.append("Mode de sortie inconnu")
    if not rules.is_trading:
        errors.append("Paire non négociable")
    if current_price <= 0 or capital <= 0:
        errors.append("Prix ou capital invalide")
    usable = max(available_quote * (1 - reserve_percent / 100), 0.0)
    if capital > usable + 1e-9:
        errors.append("Capital supérieur au solde disponible après réserve")

    # 2 % conservés pour mouvement de prix et frais avant l'ordre Market.
    quantity = float(rules.round_qty(capital * 0.98 / current_price, market=True)) if current_price > 0 else 0.0
    spend = quantity * current_price
    if quantity <= 0:
        errors.append("Quantité achetable nulle")
    else:
        errors.extend(rules.check_qty(quantity, market=True))
        errors.extend(rules.check_notional(current_price, quantity))

    rounded_exit = float(rules.round_price(exit_price, mode="up" if exit_mode == "TP_ONLY" else "down"))
    if rounded_exit <= 0:
        errors.append("Prix de sortie invalide")
    elif exit_mode == "TP_ONLY" and rounded_exit <= current_price:
        errors.append("Le TP doit être au-dessus du prix d'achat estimé")
    elif exit_mode == "SL_ONLY" and rounded_exit >= current_price:
        errors.append("Le SL doit être sous le prix d'achat estimé")
    if rounded_exit > 0:
        errors.extend(rules.check_price(rounded_exit))
        # Estimation prudente : la commission d'achat peut réduire la base reçue.
        sell_qty = rules.round_qty(quantity * 0.998)
        errors.extend(rules.check_qty(sell_qty))
        limit_price = (
            float(rules.round_price(rounded_exit * 0.997, mode="down"))
            if exit_mode == "SL_ONLY" else rounded_exit
        )
        errors.extend(rules.check_price(limit_price))
        errors.extend(rules.check_notional(limit_price, sell_qty))
    risk = spend if exit_mode == "TP_ONLY" else max(quantity * (current_price - rounded_exit), 0.0)
    return InvestmentPreview(
        rules.symbol, rules.base_asset, rules.quote_asset,
        exit_mode, quantity, spend, rounded_exit, risk, tuple(errors)
    )


def make_investment_position(preview: InvestmentPreview, *, current_price: float) -> Position:
    if not preview.eligible:
        raise ValueError("Plan d'investissement invalide")
    position = Position(
        order_identity_version=2,
        symbol=preview.symbol, base_asset=preview.base_asset, quote_asset=preview.quote_asset,
        status=PositionStatus.PENDING_ENTRIES, environment="DEMO",
        creation_price=current_price, planned_capital=preview.estimated_spend,
        preset_name="Investissement long terme",
        tags=["long_term"],
    )
    position.entries.append(Entry(
        sequence_number=1, order_type=OrderType.MARKET,
        price_mode=PriceMode.FIXED_PRICE, resolved_price=current_price,
        capital_percent=100, quote_amount=preview.estimated_spend,
        requested_qty=preview.quantity, binance_qty=preview.quantity,
    ))
    if preview.exit_mode == "TP_ONLY":
        position.take_profits.append(TakeProfit(
            sequence_number=1, price_mode=PriceMode.FIXED_PRICE,
            target_price=preview.exit_price, sell_percent=100,
            execution_policy=TPExecutionPolicy.LIMIT_GTC,
        ))
        position.stop_loss.status = SLStatus.NONE
        position.stop_loss.resolved_price = None
    else:
        position.stop_loss.mode = SLMode.FIXED_PRICE
        position.stop_loss.value = preview.exit_price
        position.stop_loss.resolved_price = preview.exit_price
        position.stop_loss.status = SLStatus.PLANNED
    position.log(
        EventType.POSITION_CREATED,
        f"Investissement {preview.symbol} créé ({preview.exit_mode})",
        exit_mode=preview.exit_mode,
    )
    return position
