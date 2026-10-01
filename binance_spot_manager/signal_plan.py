"""Convert a reviewed signal to the existing worker's guarded command protocol."""
from dataclasses import replace
import math
import re
import time

from .models import OrderType, PriceMode, SLMode, SLRuleAfterTP, SignalSource
from .position_engine import PositionEngine
from .signal_parser import ParsedSignal
from .strategy_engine import EntrySpec, SLSpec, StrategyEngine, StrategySpec, TPSpec


def automatic_signal_selection(parsed: ParsedSignal, *, entry_count=1, tp_count=2):
    """Apply the saved automatic-execution limits without mutating the inbox row."""
    try:
        entry_count = min(max(int(entry_count), 1), 20)
    except (TypeError, ValueError):
        entry_count = 1
    try:
        tp_count = min(max(int(tp_count), 1), 20)
    except (TypeError, ValueError):
        tp_count = 2
    return replace(
        parsed,
        entries=list(parsed.entries[:entry_count]),
        targets=list(parsed.targets[:tp_count]),
        warnings=list(parsed.warnings),
        errors=list(parsed.errors),
    )


def custom_signal_allocations(raw, count: int, label="niveaux") -> list[float]:
    """Parse a user distribution such as ``70;30`` or ``50/30/20``."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"Répartition personnalisée des {label} absente.")
    decimal_comma = ";" in text or "/" in text
    parts = re.split(r"[;/]", text) if decimal_comma else text.split(",")
    try:
        values = [
            float(part.strip().rstrip("%").replace(",", ".") if decimal_comma
                  else part.strip().rstrip("%"))
            for part in parts
        ]
    except ValueError as exc:
        raise ValueError(f"Répartition personnalisée des {label} invalide.") from exc
    if len(values) != count:
        raise ValueError(
            f"La répartition des {label} doit contenir {count} pourcentage(s)."
        )
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError(f"Chaque pourcentage des {label} doit être positif.")
    if not math.isclose(sum(values), 100.0, abs_tol=0.01):
        raise ValueError(f"La répartition des {label} doit totaliser 100 %.")
    return values


def automatic_entry_allocations(count: int, mode="EQUAL", custom="") -> list[float]:
    """Return percentages of the signal budget allocated to selected entries."""
    if count <= 0:
        return []
    if str(mode).upper() == "CUSTOM":
        return custom_signal_allocations(custom, count, "entrées")
    return [100.0 / count] * count


def automatic_tp_allocations(count: int, mode="EARLY", custom="") -> list[float]:
    """Return percentages of the initial position allocated to selected TP."""
    if count <= 0:
        return []
    if str(mode).upper() == "CUSTOM":
        return custom_signal_allocations(custom, count, "TP")
    if str(mode).upper() == "EQUAL":
        return [100.0 / count] * count
    presets = {1: [100.0], 2: [70.0, 30.0], 3: [50.0, 30.0, 20.0]}
    if count in presets:
        return presets[count]
    weights = list(range(count, 0, -1))
    total = float(sum(weights))
    return [100.0 * weight / total for weight in weights]


TRAIL_STOP_KEY = "signal_trail_stop"


def trailing_stop_rules(entries, targets, enabled=True):
    """SL rule attached to each TP: after TP1 the SL moves to Entry 1, from TP3 it trails two
    targets behind (TP3 → TP1, TP4 → TP2…). Disabled: the signal's stop never moves.

    The rules are stored in the position when it is created, so a change of the setting only
    affects new trades; open trades keep the behaviour they started with.
    """
    rules = []
    for index in range(len(targets)):
        if not enabled or index == 1:
            rules.append((SLRuleAfterTP.NO_CHANGE, None))
        elif index == 0:
            rules.append((SLRuleAfterTP.FIXED_PRICE, entries[0]))
        else:
            rules.append((SLRuleAfterTP.FIXED_PRICE, targets[index - 2]))
    return rules


def prepare_signal(parsed: ParsedSignal, rules, *, budget, available_quote, reserve_percent,
                   current_price, signal_id, source="manual", touch_stop=False,
                   validity_confirmed=False, entry_allocations=None, tp_allocations=None,
                   trail_stop=True):
    if parsed.errors:
        raise ValueError(" ; ".join(parsed.errors))
    if not validity_confirmed:
        raise ValueError("La validité et l'âge du signal doivent être vérifiés manuellement.")
    if parsed.stop_timeframe and not touch_stop:
        raise ValueError("SL conditionnel : confirmer explicitement un stop au prix, ou ne pas exécuter.")
    if rules.symbol != parsed.symbol or not rules.is_trading or rules.quote_asset not in {"USDT", "USDC"}:
        raise ValueError("Paire Binance Demo incompatible")
    if not math.isfinite(budget) or budget <= 0 or not math.isfinite(current_price) or current_price <= 0:
        raise ValueError("Budget et prix positifs requis")
    entries = [float(rules.round_price(p, mode="down")) for p in parsed.entries]
    targets = [float(rules.round_price(p, mode="up")) for p in parsed.targets]
    stop = float(rules.round_price(parsed.stop, mode="up"))
    if not stop < min(entries) <= max(entries) < min(targets) or current_price <= stop or current_price >= min(targets):
        raise ValueError("Prix arrondis incohérents, SL ou premier TP déjà atteint.")
    if len(set(targets)) != len(targets):
        raise ValueError("Deux TP se confondent après arrondi Binance.")
    entry_allocations = list(
        entry_allocations or [100.0 / len(entries)] * len(entries)
    )
    if (len(entry_allocations) != len(entries) or any(
            not math.isfinite(value) or value <= 0 for value in entry_allocations
    ) or not math.isclose(sum(entry_allocations), 100.0, abs_tol=0.01)):
        raise ValueError("La répartition des entrées doit contenir un pourcentage positif par entrée et totaliser 100 %.")
    allocations = list(tp_allocations or [100.0 / len(targets)] * len(targets))
    if (len(allocations) != len(targets) or any(
            not math.isfinite(value) or value <= 0 for value in allocations
    ) or not math.isclose(sum(allocations), 100.0, abs_tol=0.01)):
        raise ValueError("La répartition des TP doit contenir un pourcentage positif par TP et totaliser 100 %.")
    spec = StrategySpec(
        symbol=parsed.symbol, capital_amount=budget, available_quote=available_quote,
        reserve_percent=reserve_percent, current_price=current_price,
        entries=[
            EntrySpec(order_type=OrderType.LIMIT, price_mode=PriceMode.FIXED_PRICE,
                      price=price, capital_percent=allocation, expires_hours=24)
            for price, allocation in zip(entries, entry_allocations)
        ],
        take_profits=[
            TPSpec(price_mode=PriceMode.FIXED_PRICE, price=price, sell_percent=allocation,
                   sl_rule_after_hit=rule, sl_rule_value=value)
            for price, allocation, (rule, value)
            in zip(targets, allocations, trailing_stop_rules(entries, targets, trail_stop))
        ],
        stop_loss=SLSpec(mode=SLMode.FIXED_PRICE, value=stop),
        source=SignalSource(source), source_name=f"Signal {parsed.template}",
        cancel_remaining_entries_on_first_tp=True, tags=["signal", signal_id],
    )
    plan = StrategyEngine(rules).build(spec)
    if not plan.is_valid:
        raise ValueError(" ; ".join(plan.errors))
    # Check the smallest complete entry too, since TP1 may hit before other entries fill.
    # A partial fill or actual commissions may still reduce the sellable amount later.
    minimum_quantity = min(e.qty for e in plan.entries) * .99 * min(allocations) / 100.0
    for target in targets:
        errors = rules.validate_order(target, rules.round_qty(minimum_quantity, market=True), market=True)
        if errors:
            raise ValueError("Budget insuffisant pour les tranches TP (marge de quantité 1 %) : " + " ; ".join(errors))
    position = PositionEngine(rules).from_plan(plan, spec)
    for group in position.source_groups:
        group.signal_id = signal_id
    for entry in position.entries:
        entry.signal_id = signal_id
    # Existing engine applies each percentage to the remaining position. Convert
    # the user's initial-position allocation so the last selected TP closes it.
    remaining_percent = 100.0
    for index, (tp, allocation) in enumerate(zip(position.take_profits, allocations)):
        tp.sell_percent = (
            100.0 if index + 1 == len(allocations)
            else min(100.0, allocation / remaining_percent * 100.0)
        )
        remaining_percent -= allocation
    payload = {"position": position.model_dump(mode="json"),
               "independent_position": True,
               "entry_ids": [e.entry_id for e in position.entries], "reference_price": current_price,
               "signal_confirmation_expires_at": time.time() + 120}
    return plan, payload
