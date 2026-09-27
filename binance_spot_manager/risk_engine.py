"""Risk Engine — limites portefeuille appliquees avant d'ouvrir une position.

Verifie (section 72) :
  - risque maximum par position ;
  - exposition maximum par paire ;
  - risque total maximum ;
  - nombre maximum de positions ouvertes ;
  - reserve minimum en quote.

Le moteur ne decide pas a la place de l'utilisateur : il retourne une liste
de refus explicites, affiches tels quels dans New Trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import Position
from .strategy_engine import StrategyPlan


@dataclass
class RiskLimits:
    """Limites issues des Settings, surchargeables dans la page Settings."""

    max_risk_per_position_percent: float = 1.0
    max_total_risk_percent: float = 5.0
    max_open_positions: int = 5
    max_exposure_per_symbol_percent: float = 25.0
    min_reserve_percent: float = 20.0


@dataclass
class PortfolioSnapshot:
    """Etat actuel du portefeuille, calcule depuis les positions ouvertes."""

    quote_balance: float = 0.0
    capital_committed: float = 0.0
    capital_pending: float = 0.0
    open_positions: int = 0
    exposure_by_symbol: dict[str, float] = field(default_factory=dict)
    total_risk_quote: float = 0.0

    @property
    def total_capital(self) -> float:
        return self.quote_balance + self.capital_committed + self.capital_pending

    @property
    def total_risk_percent(self) -> float:
        total = self.total_capital
        return (self.total_risk_quote / total * 100.0) if total > 0 else 0.0


@dataclass
class RiskReport:
    """Verdict du Risk Engine."""

    accepted: bool = True
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    planned_risk_quote: float = 0.0
    planned_risk_percent: float = 0.0
    planned_exposure_percent: float = 0.0
    projected_total_risk_percent: float = 0.0
    capital_free_after: float = 0.0

    #: message impose par le cahier des charges quand le risque est trop grand
    REFUSAL_MESSAGE = "SECURITE : risque portefeuille depasse"

    def refuse(self, message: str) -> None:
        self.accepted = False
        if message not in self.refusals:
            self.refusals.append(message)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


class RiskEngine:
    def __init__(self, limits: Optional[RiskLimits] = None) -> None:
        self.limits = limits or RiskLimits()

    # -- snapshot -------------------------------------------------------

    @staticmethod
    def snapshot(
        positions: list[Position],
        quote_balance: float,
    ) -> PortfolioSnapshot:
        """Construit l'etat portefeuille a partir des positions ouvertes."""
        snapshot = PortfolioSnapshot(quote_balance=quote_balance)

        for position in positions:
            if not position.is_open:
                continue
            snapshot.open_positions += 1
            snapshot.capital_committed += position.metrics.capital_committed
            snapshot.capital_pending += position.metrics.capital_pending

            exposure = position.metrics.capital_committed + position.metrics.capital_pending
            snapshot.exposure_by_symbol[position.symbol] = (
                snapshot.exposure_by_symbol.get(position.symbol, 0.0) + exposure
            )
            snapshot.total_risk_quote += abs(position.metrics.max_loss_at_sl)

        return snapshot

    # -- evaluation -----------------------------------------------------

    def evaluate(
        self,
        plan: StrategyPlan,
        snapshot: PortfolioSnapshot,
        *,
        symbol: str,
    ) -> RiskReport:
        report = RiskReport()
        if not plan.is_valid:
            report.refuse("Plan invalide : corriger les erreurs de strategie avant lancement")
            return report

        total_capital = max(snapshot.total_capital, 0.0)
        if total_capital <= 0:
            report.warn("Portefeuille vide : limites de risque non evaluables")
            return report

        planned_risk = abs(plan.loss_max_estimated)
        planned_exposure = plan.capital_total

        report.planned_risk_quote = planned_risk
        report.planned_risk_percent = planned_risk / total_capital * 100.0
        report.planned_exposure_percent = planned_exposure / total_capital * 100.0
        report.projected_total_risk_percent = (
            (snapshot.total_risk_quote + planned_risk) / total_capital * 100.0
        )
        report.capital_free_after = (
            snapshot.quote_balance - planned_exposure - plan.capital_reserved
        )

        # 1. Risque par position
        if report.planned_risk_percent > self.limits.max_risk_per_position_percent:
            report.refuse(
                f"{RiskReport.REFUSAL_MESSAGE} — risque de cette position "
                f"{report.planned_risk_percent:.2f} % > "
                f"{self.limits.max_risk_per_position_percent:.2f} %"
            )

        # 2. Risque total
        if report.projected_total_risk_percent > self.limits.max_total_risk_percent:
            report.refuse(
                f"{RiskReport.REFUSAL_MESSAGE} — risque total apres ce trade "
                f"{report.projected_total_risk_percent:.2f} % > "
                f"{self.limits.max_total_risk_percent:.2f} %"
            )

        # 3. Nombre de positions
        if snapshot.open_positions >= self.limits.max_open_positions:
            report.refuse(
                f"Nombre maximum de positions ouvertes atteint "
                f"({snapshot.open_positions}/{self.limits.max_open_positions})"
            )

        # 4. Exposition par paire
        current_exposure = snapshot.exposure_by_symbol.get(symbol.upper(), 0.0)
        projected_symbol_exposure = (current_exposure + planned_exposure) / total_capital * 100.0
        if projected_symbol_exposure > self.limits.max_exposure_per_symbol_percent:
            report.refuse(
                f"Exposition {symbol.upper()} apres ce trade "
                f"{projected_symbol_exposure:.2f} % > "
                f"{self.limits.max_exposure_per_symbol_percent:.2f} %"
            )

        # 5. Reserve
        if report.capital_free_after < 0:
            report.refuse(
                f"Reserve de capital violee : il resterait "
                f"{report.capital_free_after:.2f} (reserve demandee "
                f"{plan.capital_reserved:.2f})"
            )

        if report.planned_exposure_percent > 50:
            report.warn(
                f"Ce trade expose {report.planned_exposure_percent:.1f} % du portefeuille"
            )
        if snapshot.open_positions + 1 >= self.limits.max_open_positions:
            report.warn("Prochaine position : limite de positions atteinte")

        return report
