"""Convert a reviewed signal to the existing worker's guarded command protocol."""
import math
import time

from .models import OrderType, PriceMode, SLMode, SLRuleAfterTP, SignalSource
from .position_engine import PositionEngine
from .signal_parser import ParsedSignal
from .strategy_engine import EntrySpec, SLSpec, StrategyEngine, StrategySpec, TPSpec

#: Règles de SL après TP proposées pour les signaux : aucune ne demande de valeur.
SIGNAL_SL_AFTER_TP_RULES = (
    SLRuleAfterTP.NO_CHANGE,
    SLRuleAfterTP.BREAK_EVEN,
    SLRuleAfterTP.BREAK_EVEN_WITH_FEES,
    SLRuleAfterTP.PREVIOUS_TP,
)


def signal_sl_after_tp(value) -> SLRuleAfterTP:
    """Règle enregistrée dans les préférences ; toute valeur inconnue → NO_CHANGE."""
    try:
        rule = SLRuleAfterTP(value)
    except ValueError:
        return SLRuleAfterTP.NO_CHANGE
    return rule if rule in SIGNAL_SL_AFTER_TP_RULES else SLRuleAfterTP.NO_CHANGE


def prepare_signal(parsed: ParsedSignal, rules, *, budget, available_quote, reserve_percent,
                   current_price, signal_id, source="manual", touch_stop=False, validity_confirmed=False,
                   sl_after_tp=SLRuleAfterTP.NO_CHANGE):
    sl_after_tp = SLRuleAfterTP(sl_after_tp)
    if sl_after_tp not in SIGNAL_SL_AFTER_TP_RULES:
        raise ValueError("Règle de SL après TP non disponible pour les signaux.")
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
    spec = StrategySpec(
        symbol=parsed.symbol, capital_amount=budget, available_quote=available_quote,
        reserve_percent=reserve_percent, current_price=current_price,
        entries=[EntrySpec(order_type=OrderType.LIMIT, price_mode=PriceMode.FIXED_PRICE,
                           price=p, capital_percent=100 / len(entries), expires_hours=24) for p in entries],
        # Le dernier TP clôture la position : aucune règle de SL après lui.
        take_profits=[TPSpec(price_mode=PriceMode.FIXED_PRICE, price=p, sell_percent=100 / len(targets),
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
    minimum_quantity = min(e.qty for e in plan.entries) * .99 / len(targets)
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
    for index, tp in enumerate(position.take_profits):
        tp.sell_percent = 100 / (len(targets) - index)
    payload = {"position": position.model_dump(mode="json"),
               "independent_position": True,
               "entry_ids": [e.entry_id for e in position.entries], "reference_price": current_price,
               "signal_confirmation_expires_at": time.time() + 120}
    return plan, payload
