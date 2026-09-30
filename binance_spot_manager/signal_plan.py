"""Convert a reviewed signal to the existing worker's guarded command protocol."""
from datetime import datetime, timezone
import math
import time

from .models import OrderType, PriceMode, SLMode, SLRuleAfterTP, SignalSource
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
    "BREAK_EVEN_AFTER_TP1": SLRuleAfterTP.BREAK_EVEN,
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


def prepare_signal(parsed: ParsedSignal, rules, *, budget, available_quote, reserve_percent,
                   current_price, signal_id, source="manual", touch_stop=False, validity_confirmed=False,
                   sl_after_tp=SLRuleAfterTP.NO_CHANGE):
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
    # Parts initiales de chaque TP : TP_WEIGHTS du contrat CSI, sinon parts égales.
    weights = [float(w) for w in parsed.tp_weights] if parsed.is_csi else []
    if weights and (len(weights) != len(targets) or any(w <= 0 for w in weights)
                    or abs(sum(weights) - 1) > 1e-9):
        raise ValueError("TP_WEIGHTS incohérents avec les TP du signal.")
    if not weights:
        weights = [1 / len(targets)] * len(targets)
    spec = StrategySpec(
        symbol=parsed.symbol, capital_amount=budget, available_quote=available_quote,
        reserve_percent=reserve_percent, current_price=current_price,
        # CSI : l'entrée expire à ENTRY_EXPIRES_AT (posé plus bas), pas 24 h après la préparation.
        entries=[EntrySpec(order_type=OrderType.LIMIT, price_mode=PriceMode.FIXED_PRICE,
                           price=p, capital_percent=100 / len(entries),
                           expires_hours=None if parsed.is_csi else 24) for p in entries],
        # Le dernier TP clôture la position : aucune règle de SL après lui.
        take_profits=[TPSpec(price_mode=PriceMode.FIXED_PRICE, price=p, sell_percent=weights[index] * 100,
                             sl_rule_after_hit=sl_after_tp if index < len(targets) - 1 else SLRuleAfterTP.NO_CHANGE)
                      for index, p in enumerate(targets)],
        stop_loss=SLSpec(mode=SLMode.FIXED_PRICE, value=stop),
        source=SignalSource(source), source_name=f"Signal {parsed.template}",
        cancel_remaining_entries_on_first_tp=True, tags=["signal", signal_id],
    )
    plan = StrategyEngine(rules).build(spec)
    if not plan.is_valid:
        raise ValueError(" ; ".join(plan.errors))
    # Check the smallest complete entry too, since TP1 may hit before other entries fill.
    # A partial fill or actual commissions may still reduce the sellable amount later.
    # The smallest weight is the smallest slice that must remain sellable.
    minimum_quantity = min(e.qty for e in plan.entries) * .99 * min(weights)
    for target in targets:
        errors = rules.validate_order(target, rules.round_qty(minimum_quantity, market=True), market=True)
        if errors:
            raise ValueError("Budget insuffisant pour les tranches TP (marge de quantité 1 %) : " + " ; ".join(errors))
    position = PositionEngine(rules).from_plan(plan, spec)
    for group in position.source_groups:
        group.signal_id = signal_id
    for entry in position.entries:
        entry.signal_id = signal_id
    # Existing engine applies each percentage to the REMAINING position, not the initial one.
    for tp, percent in zip(position.take_profits, tp_sell_percents(weights)):
        tp.sell_percent = percent
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
            "signal_expires_at": parsed.expires_at,
            "entry_expires_at": parsed.entry_expires_at,
            "max_entry_deviation_bps": parsed.max_entry_deviation_bps,
            "signal_entry_price": parsed.entries[0],
            "signal_external_id": parsed.signal_id,
            "exit_policy_id": parsed.exit_policy_id,
            "exit_policy_hash": parsed.exit_policy_hash,
        }
    return plan, payload
