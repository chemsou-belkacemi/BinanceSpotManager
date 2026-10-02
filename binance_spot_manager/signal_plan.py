"""Convert a reviewed signal to the existing worker's guarded command protocol."""
from dataclasses import replace
from datetime import datetime, timezone
import math
import re
import time

from .candle_stop import kline_interval
from .models import OrderType, PriceMode, SLMode, SLRuleAfterTP, SLTrigger, SignalSource
from .position_engine import PositionEngine
from .signal_parser import BSM_EXIT_POLICIES, BSM_EXIT_POLICY_HASHES, ParsedSignal
from .strategy_engine import EntrySpec, SLSpec, StrategyEngine, StrategySpec, TPSpec

#: Règles de SL après TP proposées pour les signaux : aucune ne demande de valeur.
SIGNAL_SL_AFTER_TP_RULES = (
    SLRuleAfterTP.NO_CHANGE,
    SLRuleAfterTP.BREAK_EVEN,
    SLRuleAfterTP.BREAK_EVEN_WITH_FEES,
    SLRuleAfterTP.PREVIOUS_TP,
)

#: Règle d'arrêt d'une politique CSI → règle de SL après TP du moteur existant.
#: Seules les politiques réellement exécutées par BSM (signal_parser.BSM_EXIT_POLICIES)
#: sont reconnues : aucune règle n'est devinée.
STOP_RULE_SL_AFTER_TP = {
    "FIXED": SLRuleAfterTP.NO_CHANGE,
    # Prix moyen d'achat réel, après le premier TP rempli (règle posée sur chaque TP sauf le dernier).
    "BREAK_EVEN_AVG_FILL_AFTER_FIRST_TP": SLRuleAfterTP.BREAK_EVEN,
}


def signal_sl_after_tp(value) -> SLRuleAfterTP:
    """Règle enregistrée dans les préférences ; toute valeur inconnue → NO_CHANGE."""
    try:
        rule = SLRuleAfterTP(value)
    except ValueError:
        return SLRuleAfterTP.NO_CHANGE
    return rule if rule in SIGNAL_SL_AFTER_TP_RULES else SLRuleAfterTP.NO_CHANGE


def exit_policy_sl_rule(policy_id, policy_hash=None) -> SLRuleAfterTP:
    """Règle de SL imposée par EXIT_POLICY_ID ; politique non exécutée ou empreinte différente → ValueError."""
    rules = BSM_EXIT_POLICIES.get(str(policy_id))
    if rules is None:
        raise ValueError(f"EXIT_POLICY_ID {policy_id} non exécutée par BinanceSpotManager ; aucune règle de SL déduite")
    if policy_hash is not None and policy_hash != BSM_EXIT_POLICY_HASHES[str(policy_id)]:
        raise ValueError(f"EXIT_POLICY_HASH {policy_hash} différent de l'empreinte exécutée pour {policy_id}")
    return STOP_RULE_SL_AFTER_TP[rules["stop_rule"]]


def tp_sell_percents(weights) -> list[float]:
    """Parts de la position (poids initiaux) → pourcentages du RESTANT attendus par le moteur.

    Le moteur applique chaque pourcentage à la quantité restante : la tranche i
    vaut w_i / (1 − Σ_{j<i} w_j) × 100 ; le dernier TP clôture toujours à 100 %.
    """
    percents = []
    consumed = 0.0
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            percents.append(100.0)
        else:
            percents.append(weight / (1.0 - consumed) * 100.0)
        consumed += weight
    return percents


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
    targets behind (TP3 → TP1, TP4 → TP2…); a moved candle-close SL becomes a price stop.
    Disabled: the signal's stop never moves and keeps its mode (candle close stays candle close).

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
                   trail_stop=True, sl_after_tp=SLRuleAfterTP.NO_CHANGE):
    """Stop après TP : signal CSI → règle de sa politique de sortie (contrat) ; signal texte → stop suiveur
    (`trail_stop`, TP1 → entrée, TPk → TP(k−2)) s'il est activé, sinon la règle `sl_after_tp` appliquée à chaque TP
    sauf le dernier (par défaut NO_CHANGE : le stop ne bouge pas)."""
    sl_after_tp = SLRuleAfterTP(sl_after_tp)
    if sl_after_tp not in SIGNAL_SL_AFTER_TP_RULES:
        raise ValueError("Règle de SL après TP non disponible pour les signaux.")
    if parsed.errors:
        raise ValueError(" ; ".join(parsed.errors))
    if parsed.signal_version not in {1, 3}:
        raise ValueError(f"Contrat CSI version {parsed.signal_version} retiré : seul SIGNAL_VERSION=3 est exécuté.")
    if parsed.is_csi:
        # Le contrat porte sa propre politique de sortie et ses deux expirations.
        sl_after_tp = exit_policy_sl_rule(parsed.exit_policy_id, parsed.exit_policy_hash)
        if len(parsed.entries) != 1:
            raise ValueError("EXIT_POLICY : une seule entrée exécutée par BinanceSpotManager.")
        if not (math.isfinite(parsed.expires_at) and parsed.expires_at > 0
                and math.isfinite(parsed.entry_expires_at) and parsed.entry_expires_at >= parsed.expires_at):
            raise ValueError("EXPIRES_AT / ENTRY_EXPIRES_AT absents ou incohérents.")
        if not math.isfinite(parsed.max_entry_deviation_bps) or parsed.max_entry_deviation_bps < 0:
            raise ValueError("MAX_ENTRY_DEVIATION_BPS invalide.")
    if not validity_confirmed:
        raise ValueError("La validité et l'âge du signal doivent être vérifiés manuellement.")
    # A timed SL waits for its candle close (no Binance stop order) unless a price stop is chosen.
    candle_interval = ""
    if parsed.stop_timeframe and not touch_stop:
        candle_interval = kline_interval(parsed.stop_timeframe) or ""
        if not candle_interval:
            raise ValueError(
                f"SL conditionnel « {parsed.stop_timeframe} » : bougie inconnue, choisir explicitement "
                "un stop au prix, ou ne pas exécuter."
            )
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
    # Le dernier TP clôture la position : aucune règle de SL après lui.
    uniform_rules = [(sl_after_tp if index < len(targets) - 1 else SLRuleAfterTP.NO_CHANGE, None)
                     for index in range(len(targets))]
    if parsed.is_csi:
        # Contrat CSI : une seule entrée (vérifié plus haut), parts des TP = TP_WEIGHTS, règle de la politique,
        # entrée valable jusqu'à ENTRY_EXPIRES_AT (posé plus bas), pas 24 h après la préparation.
        weights = [float(w) for w in parsed.tp_weights]
        if weights and (len(weights) != len(targets) or any(w <= 0 for w in weights)
                        or abs(sum(weights) - 1) > 1e-9):
            raise ValueError("TP_WEIGHTS incohérents avec les TP du signal.")
        entry_allocations = [100.0 / len(entries)] * len(entries)
        allocations = [w * 100 for w in weights] if weights else [100.0 / len(targets)] * len(targets)
        stop_rules = uniform_rules
        entry_expires_hours = None
    else:
        stop_rules = trailing_stop_rules(entries, targets, True) if trail_stop else uniform_rules
        entry_expires_hours = 24
        entry_allocations = list(entry_allocations or [100.0 / len(entries)] * len(entries))
        allocations = list(tp_allocations or [100.0 / len(targets)] * len(targets))
    if (len(entry_allocations) != len(entries) or any(
            not math.isfinite(value) or value <= 0 for value in entry_allocations
    ) or not math.isclose(sum(entry_allocations), 100.0, abs_tol=0.01)):
        raise ValueError("La répartition des entrées doit contenir un pourcentage positif par entrée et totaliser 100 %.")
    if (len(allocations) != len(targets) or any(
            not math.isfinite(value) or value <= 0 for value in allocations
    ) or not math.isclose(sum(allocations), 100.0, abs_tol=0.01)):
        raise ValueError("La répartition des TP doit contenir un pourcentage positif par TP et totaliser 100 %.")
    spec = StrategySpec(
        symbol=parsed.symbol, capital_amount=budget, available_quote=available_quote,
        reserve_percent=reserve_percent, current_price=current_price,
        entries=[
            EntrySpec(order_type=OrderType.LIMIT, price_mode=PriceMode.FIXED_PRICE,
                      price=price, capital_percent=allocation, expires_hours=entry_expires_hours)
            for price, allocation in zip(entries, entry_allocations)
        ],
        take_profits=[
            TPSpec(price_mode=PriceMode.FIXED_PRICE, price=price, sell_percent=allocation,
                   sl_rule_after_hit=rule, sl_rule_value=value)
            for price, allocation, (rule, value) in zip(targets, allocations, stop_rules)
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
    # The smallest TP slice is the smallest quantity that must remain sellable.
    minimum_quantity = min(e.qty for e in plan.entries) * .99 * min(allocations) / 100.0
    for target in targets:
        errors = rules.validate_order(target, rules.round_qty(minimum_quantity, market=True), market=True)
        if errors:
            raise ValueError("Budget insuffisant pour les tranches TP (marge de quantité 1 %) : " + " ; ".join(errors))
    position = PositionEngine(rules).from_plan(plan, spec)
    if candle_interval:
        position.stop_loss.trigger = SLTrigger.CANDLE_CLOSE
        position.stop_loss.candle_interval = candle_interval
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
    if parsed.is_csi:
        # Deux expirations : EXPIRES_AT borne l'acceptation (gelée, recontrôlée avant
        # l'achat), ENTRY_EXPIRES_AT borne l'ordre d'entrée non rempli.
        expiry = datetime.fromtimestamp(parsed.entry_expires_at, tz=timezone.utc)
        for entry in position.entries:
            entry.expires_at = expiry
        # Politique : une tranche de TP sous les minimums Binance est reportée sur le TP suivant.
        position.automation.merge_below_minimum_tp = True
        payload["position"] = position.model_dump(mode="json")
        # Gelés dans la commande : le worker les recontrôle juste avant l'achat.
        payload |= {
            "signal_valid_from": parsed.valid_from,
            "signal_expires_at": parsed.expires_at,
            "entry_expires_at": parsed.entry_expires_at,
            "max_entry_deviation_bps": parsed.max_entry_deviation_bps,
            "signal_entry_price": parsed.entries[0],
            "signal_external_id": parsed.signal_id,
            "exit_policy_id": parsed.exit_policy_id,
            "exit_policy_hash": parsed.exit_policy_hash,
        }
    return plan, payload
