"""Strategy Engine — construction et calcul d'un plan de trade.

Responsabilites :
- resoudre les niveaux (Entry / TP / SL) depuis un mode prix OU pourcentage ;
- repartir le capital entre les Entries ;
- calculer quantites, prix moyen estime, gains et pertes ;
- produire des scenarios (E1 seule, E1+E2, toutes) ;
- tout valider avant lancement.

Aucune communication reseau ici : moteur pur, entierement testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from .models import (
    EntryReference,
    OrderType,
    PriceMode,
    SLMode,
    SLRuleAfterTP,
    SignalSource,
    TPExecutionPolicy,
    TPReference,
)
from .symbol_rules import SymbolRules, format_decimal


def _f(value: Any) -> float:
    return float(value)


# ==========================================================================
# Specification d'entree (ce que l'utilisateur a saisi)
# ==========================================================================


@dataclass
class EntrySpec:
    """Une Entry telle que saisie dans New Trade, avant resolution."""

    order_type: OrderType = OrderType.LIMIT
    price_mode: PriceMode = PriceMode.PERCENT
    reference: EntryReference = EntryReference.ENTRY_1
    price: Optional[float] = None
    offset_percent: Optional[float] = None
    capital_percent: float = 0.0
    expires_hours: Optional[float] = None


@dataclass
class TPSpec:
    """Un Take Profit tel que saisi."""

    price_mode: PriceMode = PriceMode.PERCENT
    reference: TPReference = TPReference.AVERAGE_PRICE
    price: Optional[float] = None
    target_percent: Optional[float] = None
    sell_percent: float = 0.0
    sl_rule_after_hit: SLRuleAfterTP = SLRuleAfterTP.NO_CHANGE
    sl_rule_value: Optional[float] = None


@dataclass
class SLSpec:
    """Le Stop Loss tel que saisi."""

    mode: SLMode = SLMode.AVERAGE_PERCENT
    value: float = -4.0


@dataclass
class StrategySpec:
    """Plan complet saisi par l'utilisateur."""

    symbol: str
    entries: list[EntrySpec] = field(default_factory=list)
    take_profits: list[TPSpec] = field(default_factory=list)
    stop_loss: SLSpec = field(default_factory=SLSpec)

    capital_mode: str = "FIXED"  # FIXED | PERCENT_OF_BALANCE | RISK_BASED
    capital_amount: float = 0.0
    capital_percent: float = 0.0
    risk_percent: float = 0.0

    available_quote: float = 0.0
    reserve_percent: float = 20.0
    current_price: float = 0.0

    tp_execution_policy: TPExecutionPolicy = TPExecutionPolicy.MARKET_ON_TRIGGER
    cancel_remaining_entries_on_first_tp: bool = False

    source: SignalSource = SignalSource.MANUAL
    source_name: str = ""
    preset_name: str = ""
    tags: list[str] = field(default_factory=list)


# ==========================================================================
# Resultats calcules
# ==========================================================================


@dataclass
class ResolvedEntry:
    sequence: int
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    capital_percent: float = 0.0
    quote_amount: float = 0.0
    raw_qty: float = 0.0
    qty: float = 0.0
    notional: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class ResolvedTP:
    sequence: int
    target_price: float = 0.0
    target_percent: float = 0.0
    sell_percent: float = 0.0
    estimated_qty: float = 0.0
    gain_estimated: float = 0.0
    sl_rule_after_hit: SLRuleAfterTP = SLRuleAfterTP.NO_CHANGE
    sl_rule_value: Optional[float] = None
    errors: list[str] = field(default_factory=list)


@dataclass
class ResolvedSL:
    mode: SLMode
    value: float
    price: float = 0.0
    percent_from_average: float = 0.0
    max_loss: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    """Scenarios A/B/C de la section 74."""

    label: str
    filled_entries: int
    average_price: float = 0.0
    invested: float = 0.0
    sl_price: float = 0.0
    max_loss: float = 0.0
    quantity: float = 0.0


@dataclass
class StrategyPlan:
    """Plan resolu et pret a etre converti en Position."""

    symbol: str
    base_asset: str = ""
    quote_asset: str = "USDT"

    capital_total: float = 0.0
    capital_allocated: float = 0.0
    capital_remaining: float = 0.0
    capital_reserved: float = 0.0

    entries: list[ResolvedEntry] = field(default_factory=list)
    take_profits: list[ResolvedTP] = field(default_factory=list)
    stop_loss: ResolvedSL = field(default_factory=lambda: ResolvedSL(SLMode.AVERAGE_PERCENT, -4.0))

    estimated_average_price: float = 0.0
    estimated_total_qty: float = 0.0
    sell_percent_total: float = 0.0
    gain_total_estimated: float = 0.0
    loss_max_estimated: float = 0.0
    risk_percent_of_capital: float = 0.0

    scenarios: list[Scenario] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    tp_execution_policy: TPExecutionPolicy = TPExecutionPolicy.MARKET_ON_TRIGGER
    cancel_remaining_entries_on_first_tp: bool = False
    current_price: float = 0.0

    # -- accesseurs pratiques -------------------------------------------

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def errors_text(self) -> list[str]:
        return list(self.errors)

    def entry_prices(self) -> list[float]:
        return [e.price for e in self.entries]


# ==========================================================================
# Repartitions de capital
# ==========================================================================


def equal_split(count: int) -> list[float]:
    if count <= 0:
        return []
    share = 100.0 / count
    return [share] * count


def progressive_split(count: int) -> list[float]:
    """Poids croissants (DCA : plus on descend, plus on achete)."""
    if count <= 0:
        return []
    weights = [Decimal(i + 1) for i in range(count)]
    total = sum(weights)
    return [float((w / total) * 100) for w in weights]


def degressive_split(count: int) -> list[float]:
    """Poids decroissants (plus lourd en premier)."""
    if count <= 0:
        return []
    weights = [Decimal(count - i) for i in range(count)]
    total = sum(weights)
    return [float((w / total) * 100) for w in weights]


def recommended_split(count: int) -> list[float]:
    """Repartition initiale des Entries selon leur nombre."""
    if count <= 0:
        return []
    if count == 1:
        return [100.0]
    if count == 2:
        return [70.0, 30.0]
    if count == 3:
        return [50.0, 30.0, 20.0]
    return degressive_split(count)


def normalize_split(percents: list[float]) -> list[float]:
    """Remet une repartition a 100 % en conservant les proportions."""
    total = sum(percents)
    if total <= 0:
        return equal_split(len(percents))
    return [p * 100.0 / total for p in percents]


SPLIT_PRESETS = {
    "EQUAL": equal_split,
    "PROGRESSIVE": progressive_split,
    "DEGRESSIVE": degressive_split,
}


# ==========================================================================
# Resolution des niveaux
# ==========================================================================


def resolve_entry_price(
    spec: EntrySpec,
    *,
    current_price: float,
    reference_prices: dict[EntryReference, Optional[float]],
) -> Optional[float]:
    """Resout le prix d'une Entry (mode prix fixe ou pourcentage)."""
    if spec.order_type is OrderType.MARKET:
        # Une Entry Market s'execute au prix courant.
        return current_price

    if spec.price_mode is PriceMode.FIXED_PRICE:
        return spec.price if spec.price is not None else None

    if spec.offset_percent is None:
        return None

    reference = reference_prices.get(spec.reference)
    if reference is None:
        return None
    return reference * (1.0 + spec.offset_percent / 100.0)


def resolve_entry_reference(
    index: int,
    spec: EntrySpec,
    *,
    resolved: list[Optional[float]],
    current_price: float,
) -> Optional[float]:
    """Choisit la valeur de reference pour le % d'ecart d'une Entry."""
    if spec.reference is EntryReference.CURRENT_PRICE:
        return current_price
    if spec.reference is EntryReference.ENTRY_1:
        return resolved[0] if resolved and resolved[0] is not None else current_price
    # PREVIOUS_ENTRY
    for price in reversed(resolved[:index]):
        if price is not None:
            return price
    return current_price


def resolve_tp_price(
    spec: TPSpec,
    *,
    average_price: float,
    first_entry_price: float,
    creation_price: float,
) -> Optional[float]:
    """Resout le prix cible d'un TP selon sa reference."""
    if spec.price_mode is PriceMode.FIXED_PRICE:
        return spec.price if spec.price is not None else None

    if spec.target_percent is None:
        return None

    references = {
        TPReference.AVERAGE_PRICE: average_price,
        TPReference.ENTRY_1: first_entry_price,
        TPReference.CURRENT_PRICE_AT_CREATION: creation_price,
    }
    base = references.get(spec.reference) or 0.0
    if base <= 0:
        return None
    return base * (1.0 + spec.target_percent / 100.0)


def resolve_sl_price(
    spec: SLSpec,
    *,
    average_price: float,
    first_entry_price: float,
    last_entry_price: float,
) -> Optional[float]:
    """Resout le prix SL selon le mode (section 8)."""
    if spec.mode is SLMode.FIXED_PRICE:
        return spec.value if spec.value else None

    bases = {
        SLMode.AVERAGE_PERCENT: average_price,
        SLMode.ENTRY1_PERCENT: first_entry_price,
        SLMode.LAST_ENTRY_PERCENT: last_entry_price,
    }
    base = bases.get(spec.mode) or 0.0
    if base <= 0:
        return None
    return base * (1.0 + spec.value / 100.0)


def percent_change(new_price: float, reference_price: float) -> float:
    """Variation en % entre deux prix (pour l'affichage prix <-> %)."""
    if not reference_price:
        return 0.0
    return (new_price / reference_price - 1.0) * 100.0


# ==========================================================================
# Capital
# ==========================================================================


def compute_capital(spec: StrategySpec) -> tuple[float, float, list[str]]:
    """Retourne (capital_total, capital_reserve, erreurs).

    Les trois modes de la section 23 :
      - FIXED               : montant fixe en quote ;
      - PERCENT_OF_BALANCE  : pourcentage du solde disponible ;
      - RISK_BASED          : capital deduit du risque portefeuille et du SL.
    """
    errors: list[str] = []
    available = max(spec.available_quote, 0.0)
    reserve = available * max(spec.reserve_percent, 0.0) / 100.0
    usable = max(available - reserve, 0.0)

    if spec.capital_mode == "FIXED":
        capital = max(spec.capital_amount, 0.0)
    elif spec.capital_mode == "PERCENT_OF_BALANCE":
        capital = usable * max(spec.capital_percent, 0.0) / 100.0
    elif spec.capital_mode == "RISK_BASED":
        # Risque en quote = solde total * % de risque.
        risk_quote = available * max(spec.risk_percent, 0.0) / 100.0
        sl_percent = abs(spec.stop_loss.value) if spec.stop_loss.mode is not SLMode.FIXED_PRICE else 0.0
        if spec.stop_loss.mode is SLMode.FIXED_PRICE and spec.current_price > 0:
            sl_percent = abs(percent_change(spec.stop_loss.value, spec.current_price))
        if sl_percent <= 0:
            errors.append("SL requis pour le sizing par risque (distance inconnue)")
            capital = 0.0
        else:
            capital = risk_quote / (sl_percent / 100.0)
    else:
        errors.append(f"Mode de capital inconnu : {spec.capital_mode}")
        capital = 0.0

    if capital <= 0:
        errors.append("Capital a engager nul")
    if capital > usable:
        errors.append(
            f"Capital superieur au disponible hors reserve "
            f"({format_decimal(Decimal(str(usable)))} {spec.quote_asset if hasattr(spec, 'quote_asset') else 'USDT'})"
        )
    return capital, reserve, errors


# ==========================================================================
# Moteur principal
# ==========================================================================


class StrategyEngine:
    """Transforme une StrategySpec en StrategyPlan valide (ou en erreurs)."""

    def __init__(self, rules: Optional[SymbolRules] = None) -> None:
        self.rules = rules

    # -- plan -----------------------------------------------------------

    def build(self, spec: StrategySpec) -> StrategyPlan:
        plan = StrategyPlan(
            symbol=spec.symbol.upper(),
            base_asset=self.rules.base_asset if self.rules else "",
            quote_asset=self.rules.quote_asset if self.rules else "USDT",
            current_price=spec.current_price,
            tp_execution_policy=spec.tp_execution_policy,
            cancel_remaining_entries_on_first_tp=spec.cancel_remaining_entries_on_first_tp,
        )

        if not spec.entries:
            plan.errors.append("Aucune Entry definie")
        if not spec.take_profits:
            plan.errors.append("Aucun Take Profit defini")

        capital, reserve, capital_errors = compute_capital(spec)
        plan.capital_total = capital
        plan.capital_reserved = reserve
        plan.errors.extend(capital_errors)

        self._resolve_entries(spec, plan)
        self._resolve_average(spec, plan)
        self._resolve_tps(spec, plan)
        self._resolve_sl(spec, plan)
        self._build_scenarios(spec, plan)
        self._validate(spec, plan)
        return plan

    # -- entries --------------------------------------------------------

    def _resolve_entries(self, spec: StrategySpec, plan: StrategyPlan) -> None:
        resolved_prices: list[Optional[float]] = []

        for index, entry_spec in enumerate(spec.entries):
            price = resolve_entry_price(
                entry_spec,
                current_price=spec.current_price,
                reference_prices={
                    EntryReference.ENTRY_1: None,
                    EntryReference.PREVIOUS_ENTRY: None,
                    EntryReference.CURRENT_PRICE: spec.current_price,
                },
            )

            # Resolution dependante des autres Entries (ENRY_1 / PREVIOUS_ENTRY)
            if entry_spec.price_mode is PriceMode.PERCENT:
                reference = resolve_entry_reference(
                    index,
                    entry_spec,
                    resolved=resolved_prices,
                    current_price=spec.current_price,
                )
                if reference is not None and entry_spec.offset_percent is not None:
                    price = reference * (1.0 + entry_spec.offset_percent / 100.0)

            resolved_prices.append(price)

            resolved = ResolvedEntry(
                sequence=index + 1,
                order_type=entry_spec.order_type,
                price=price or 0.0,
                capital_percent=entry_spec.capital_percent,
            )
            if price is None or price <= 0:
                resolved.errors.append(f"Entry {index + 1} : prix non resolvable")
            plan.entries.append(resolved)

        # Repartitions de capital une fois les prix connus
        for resolved in plan.entries:
            quote = plan.capital_total * resolved.capital_percent / 100.0
            resolved.quote_amount = quote
            if resolved.price > 0:
                resolved.raw_qty = quote / resolved.price
                resolved.qty = self._round_qty(resolved.raw_qty, resolved.order_type)
                resolved.notional = resolved.qty * resolved.price
            plan.capital_allocated += quote

        plan.capital_remaining = max(plan.capital_total - plan.capital_allocated, 0.0)

    # -- prix moyen -----------------------------------------------------

    def _resolve_average(self, spec: StrategySpec, plan: StrategyPlan) -> None:
        """Prix moyen pondere estime (section 32)."""
        total_qty = sum(e.qty for e in plan.entries)
        spent = sum(e.qty * e.price for e in plan.entries)
        plan.estimated_total_qty = total_qty
        plan.estimated_average_price = (spent / total_qty) if total_qty > 0 else 0.0

    # -- TP -------------------------------------------------------------

    def _resolve_tps(self, spec: StrategySpec, plan: StrategyPlan) -> None:
        first_entry = plan.entries[0].price if plan.entries else 0.0

        for index, tp_spec in enumerate(spec.take_profits):
            price = resolve_tp_price(
                tp_spec,
                average_price=plan.estimated_average_price,
                first_entry_price=first_entry,
                creation_price=spec.current_price,
            )
            resolved = ResolvedTP(
                sequence=index + 1,
                target_price=price or 0.0,
                target_percent=(
                    tp_spec.target_percent
                    if tp_spec.target_percent is not None
                    else percent_change(price or 0.0, plan.estimated_average_price)
                ),
                sell_percent=tp_spec.sell_percent,
                sl_rule_after_hit=tp_spec.sl_rule_after_hit,
                sl_rule_value=tp_spec.sl_rule_value,
            )
            if not price or price <= 0:
                resolved.errors.append(f"TP {index + 1} : prix non resolvable")
            elif plan.estimated_average_price > 0 and price <= plan.estimated_average_price:
                resolved.errors.append(f"TP {index + 1} : prix <= prix moyen (vente a perte)")

            resolved.estimated_qty = plan.estimated_total_qty * resolved.sell_percent / 100.0
            if plan.estimated_average_price > 0 and resolved.estimated_qty > 0:
                resolved.gain_estimated = (
                    resolved.estimated_qty * (resolved.target_price - plan.estimated_average_price)
                )
            plan.take_profits.append(resolved)

        plan.sell_percent_total = sum(t.sell_percent for t in plan.take_profits)
        plan.gain_total_estimated = sum(t.gain_estimated for t in plan.take_profits)

    # -- SL -------------------------------------------------------------

    def _resolve_sl(self, spec: StrategySpec, plan: StrategyPlan) -> None:
        first_entry = plan.entries[0].price if plan.entries else 0.0
        last_entry = plan.entries[-1].price if plan.entries else 0.0

        price = resolve_sl_price(
            spec.stop_loss,
            average_price=plan.estimated_average_price,
            first_entry_price=first_entry,
            last_entry_price=last_entry,
        )
        sl = ResolvedSL(
            mode=spec.stop_loss.mode,
            value=spec.stop_loss.value,
            price=price or 0.0,
        )

        if not price or price <= 0:
            sl.errors.append("SL : prix non resolvable")
        else:
            sl.percent_from_average = percent_change(price, plan.estimated_average_price)
            sl.max_loss = (
                plan.estimated_total_qty * (price - plan.estimated_average_price)
            )
            if sl.max_loss >= 0:
                sl.errors.append("SL : au-dessus du prix moyen, aucune protection reelle")
            if first_entry and price >= first_entry:
                sl.errors.append("SL : au-dessus de Entry 1")

        if plan.estimated_average_price > 0 and plan.capital_total > 0:
            plan.risk_percent_of_capital = abs(sl.max_loss) / plan.capital_total * 100.0

        plan.stop_loss = sl
        plan.loss_max_estimated = sl.max_loss

    # -- scenarios ------------------------------------------------------

    def _build_scenarios(self, spec: StrategySpec, plan: StrategyPlan) -> None:
        """Scenario A (E1 seule), B (E1+E2), C (toutes les Entries)."""
        if not plan.entries:
            return

        indices = [0] if len(plan.entries) == 1 else [0, min(1, len(plan.entries) - 1), len(plan.entries) - 1]
        labels = {0: "A — Entry 1 seule", 1: "B — Entry 1 + 2", 2: "C — toutes les Entries"}

        seen: set[int] = set()
        for slot, index in enumerate(indices):
            if index in seen:
                continue
            seen.add(index)
            subset = plan.entries[: index + 1]
            qty = sum(e.qty for e in subset)
            invested = sum(e.quote_amount for e in subset)
            average = (sum(e.qty * e.price for e in subset) / qty) if qty > 0 else 0.0

            sl_price = self._sl_price_for_average(spec, average, plan)
            loss = qty * (sl_price - average) if sl_price > 0 else 0.0

            plan.scenarios.append(
                Scenario(
                    label=labels.get(slot, f"Scenario {slot + 1}"),
                    filled_entries=index + 1,
                    average_price=average,
                    invested=invested,
                    sl_price=sl_price,
                    max_loss=loss,
                    quantity=qty,
                )
            )

    def _sl_price_for_average(
        self, spec: StrategySpec, average: float, plan: StrategyPlan
    ) -> float:
        first = plan.entries[0].price if plan.entries else 0.0
        last = plan.entries[-1].price if plan.entries else 0.0
        price = resolve_sl_price(
            spec.stop_loss,
            average_price=average,
            first_entry_price=first,
            last_entry_price=last,
        )
        return price or 0.0

    # -- validation -----------------------------------------------------

    def _validate(self, spec: StrategySpec, plan: StrategyPlan) -> None:
        # Entries : erreurs de resolution + regles Binance
        for resolved in plan.entries:
            plan.errors.extend(resolved.errors)
            if self.rules and resolved.price > 0 and resolved.qty > 0:
                market = resolved.order_type is OrderType.MARKET
                plan.errors.extend(
                    f"Entry {resolved.sequence} : {err}"
                    for err in self.rules.validate_order(
                        resolved.price, resolved.qty, market=market
                    )
                )

        # Allocations : total doit valoir 100 %
        allocation_total = sum(e.capital_percent for e in spec.entries)
        if abs(allocation_total - 100.0) > 0.01:
            plan.errors.append(
                f"Allocation des Entries = {allocation_total:.2f} % (attendu 100 %)"
            )

        # TP
        plan.errors.extend(
            f"TP {tp.sequence} : {err}" for tp in plan.take_profits for err in tp.errors
        )
        if plan.sell_percent_total > 100.0 + 0.01:
            plan.errors.append(
                f"Allocation des TP = {plan.sell_percent_total:.2f} % (> 100 %)"
            )
        if plan.sell_percent_total < 99.99:
            plan.warnings.append(
                f"Les TP ne vendent que {plan.sell_percent_total:.2f} % de la position"
            )

        # SL
        plan.errors.extend(f"SL : {err}" for err in plan.stop_loss.errors)

        # Coherence des niveaux
        prices = [e.price for e in plan.entries if e.price > 0]
        tp_prices = [t.target_price for t in plan.take_profits if t.target_price > 0]
        if prices and tp_prices and max(tp_prices) <= max(prices):
            plan.errors.append("TP : aucun TP au-dessus du prix d'entree le plus haut")
        if prices and plan.stop_loss.price > 0 and plan.stop_loss.price >= min(prices):
            plan.errors.append("SL : au-dessus de l'Entry la plus basse")

        # Capital
        if plan.capital_allocated > plan.capital_total + 1e-9:
            plan.errors.append("Capital alloue superieur au capital total")

    # -- aides ----------------------------------------------------------

    def _round_qty(self, qty: float, order_type: OrderType) -> float:
        if self.rules is None:
            return qty
        return _f(self.rules.round_qty(qty, market=order_type is OrderType.MARKET))
