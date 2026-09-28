"""New Trade — création d'une position Spot complète en 20 à 60 secondes.

Section 20 à 44 du cahier des charges. Deux modes : Quick Trade et Advanced Trade.
Tout est recalculé à la volée : prix, quantités, prix moyen, SL, gains par TP,
scénarios A/B/C, exposition portefeuille.

Au clic sur « Lancer », une demande durable est transmise au worker,
qui revalide le plan puis envoie les Entries via l'Execution Engine.
Les TP restent locaux : c'est le worker qui les surveille.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.config import get_settings  # noqa: E402
from binance_spot_manager.models import (  # noqa: E402
    EntryReference,
    EntryStatus,
    EventType,
    OrderType,
    PriceMode,
    SLMode,
    SLRuleAfterTP,
    SignalSource,
    TPExecutionPolicy,
    TPReference,
)
from binance_spot_manager.position_engine import PositionEngine  # noqa: E402
from binance_spot_manager.risk_engine import RiskEngine, RiskReport  # noqa: E402
from binance_spot_manager.strategy_engine import (  # noqa: E402
    EntrySpec,
    SLSpec,
    StrategyEngine,
    StrategySpec,
    TPSpec,
    degressive_split,
    equal_split,
    percent_change,
    progressive_split,
    recommended_split,
)
from ui_common import (  # noqa: E402
    banner,
    error_list,
    fmt_percent,
    fmt_price,
    fmt_qty,
    fmt_quote,
    get_service,
    live_market_price,
    live_price,
    load_rules,
    page_header,
    sidebar_status,
    symbol_status_box,
    warning_list,
    submit_to_worker,
    new_command_confirmation,
)

from ui_common import colored_pnl, pnl_metric, pnl_dataframe

settings = get_settings()
service = get_service()

st.set_page_config(page_title="New Trade — BinanceSpotManager", page_icon="🆕", layout="wide")
page_header("New Trade", "Construire une position Spot complète")
banner(settings)
sidebar_status(settings)

state = st.session_state.setdefault("nt", {})
mode = st.radio(
    "Mode",
    ["⚡ Quick Trade", "🔧 Advanced Trade"],
    horizontal=True,
    help="Quick Trade : l'essentiel en quelques champs. Advanced Trade : contrôle complet.",
)

# ==========================================================================
# 1. Paire
# ==========================================================================

st.subheader("1. Paire")

col_symbol, col_refresh = st.columns([4, 1])
symbol = col_symbol.text_input(
    "Symbole (ex. BTCUSDT)", value=state.get("symbol", "BTCUSDT"), key="nt_symbol"
).strip().upper()

rules, rules_error = load_rules(symbol)
price = None
balances: dict[str, float] = {}

if rules is not None:
    price = live_price(rules.symbol)
    portfolio = service.portfolio()
    balances = dict(portfolio.base_balances)
    balances[settings.quote_asset] = portfolio.quote_free

symbol_status_box(symbol, rules, balances)

if rules_error:
    st.stop()

existing_positions = service.find_all_by_symbol(symbol) if symbol else []
if existing_positions:
    st.info(
        f"{len(existing_positions)} position(s) déjà ouverte(s) sur {symbol}. "
        "Ce lancement créera une nouvelle position avec son propre ID, ses TP et son SL."
    )

quote_asset = rules.quote_asset
available_quote = balances.get(quote_asset, 0.0)
st.caption(f"Solde {quote_asset} disponible : {fmt_price(available_quote)}")

# ==========================================================================
# 2. Capital
# ==========================================================================

st.subheader("2. Capital")

capital_mode_label = st.radio(
    "Mode de capital",
    ["Montant fixe", "Pourcentage du solde", "Risque maximum"],
    horizontal=True,
)

reserve_percent = st.slider(
    "Réserve de capital à conserver (%)", 0, 80, int(settings.capital_reserve_percent)
)
usable = max(available_quote * (1 - reserve_percent / 100.0), 0.0)
st.caption(
    f"Utilisable après réserve : {fmt_price(usable)} "
    f"(réserve {fmt_price(available_quote - usable)})"
)

capital_mode = {
    "Montant fixe": "FIXED",
    "Pourcentage du solde": "PERCENT_OF_BALANCE",
    "Risque maximum": "RISK_BASED",
}[capital_mode_label]

capital_amount = 0.0
capital_percent = 0.0
risk_percent = 0.0

if capital_mode == "FIXED":
    default_amount = float(min(250.0, usable)) if usable else 0.0
    capital_amount = st.number_input(
        f"Montant ({quote_asset})", min_value=0.0, value=default_amount, step=10.0
    )
    if available_quote > 0:
        st.caption(
            f"{capital_amount / available_quote * 100:.1f} % du solde · "
            f"solde estimé après engagement : "
            f"{fmt_price(available_quote - capital_amount)}"
        )
elif capital_mode == "PERCENT_OF_BALANCE":
    capital_percent = st.slider("Pourcentage du solde utilisable (%)", 1, 100, 5)
    capital_amount = usable * capital_percent / 100.0
    st.caption(f"≈ {fmt_price(capital_amount)}")
else:
    risk_percent = st.slider(
        "Risque portefeuille (%)", 0.1, 5.0, float(settings.max_risk_per_position_percent), 0.1
    )
    st.caption(
        "Le capital est déduit de la distance au SL. "
        "La perte au SL ne dépassera pas ce pourcentage du solde total."
    )

# ==========================================================================
# 3. Entries
# ==========================================================================

st.subheader("3. Entries")

entry_count = st.number_input(
    "Nombre d'Entries", min_value=1, max_value=20, value=int(state.get("entry_count", 1))
)

split_preset = st.radio(
    "Répartition du capital",
    ["Recommandé", "Égal", "Progressif", "Dégressif", "Personnalisé"],
    horizontal=True,
)
auto_splits = {
    "Recommandé": recommended_split(int(entry_count)),
    "Égal": equal_split(int(entry_count)),
    "Progressif": progressive_split(int(entry_count)),
    "Dégressif": degressive_split(int(entry_count)),
}

split_signature = (int(entry_count), split_preset)
previous_signature = state.get("entry_split_signature")
missing_allocations = any(
    f"nt_alloc_{index}" not in st.session_state for index in range(int(entry_count))
)
if previous_signature != split_signature or missing_allocations:
    if (
        split_preset != "Personnalisé"
        or previous_signature is None
        or previous_signature[0] != int(entry_count)
        or missing_allocations
    ):
        shares = auto_splits.get(split_preset, equal_split(int(entry_count)))
        rounded = [round(share, 2) for share in shares[:-1]]
        rounded.append(round(100.0 - sum(rounded), 2))
        for index, share in enumerate(rounded):
            st.session_state[f"nt_alloc_{index}"] = share
    state["entry_split_signature"] = split_signature

entry_specs: list[EntrySpec] = []
allocation_total = 0.0

advanced = mode.startswith("🔧")

for index in range(int(entry_count)):
    label = f"Entry {index + 1}"
    with st.expander(label, expanded=index < 3):
        col_left, col_mid, col_right = st.columns(3)

        default_type = "MARKET" if (index == 0 and advanced is False) else "LIMIT"
        order_type_label = col_left.selectbox(
            "Type", ["MARKET", "LIMIT"], index=0 if default_type == "MARKET" else 1, key=f"nt_et_{index}"
        )
        order_type = OrderType.MARKET if order_type_label == "MARKET" else OrderType.LIMIT

        default_offset = [None, -2.0, -5.0][index] if index < 3 else None
        if order_type is OrderType.MARKET:
            col_mid.markdown("**Prix**")
            with col_mid:
                live_market_price(rules.symbol)
            price_mode = PriceMode.FIXED_PRICE
            offset = None
            fixed_price = price
            reference = EntryReference.ENTRY_1
        else:
            mode_label = col_mid.radio(
                "Définition du prix",
                ["Pourcentage", "Prix fixe"],
                index=0 if default_offset is not None or index > 0 else 1,
                horizontal=True,
                key=f"nt_emode_{index}",
            )
            reference = EntryReference.ENTRY_1
            if mode_label == "Pourcentage":
                price_mode = PriceMode.PERCENT
                if advanced or index > 0:
                    ref_label = col_mid.selectbox(
                        "Référence",
                        ["Entry 1", "Entry précédente", "Prix actuel"],
                        index=0,
                        key=f"nt_eref_{index}",
                    )
                    reference = {
                        "Entry 1": EntryReference.ENTRY_1,
                        "Entry précédente": EntryReference.PREVIOUS_ENTRY,
                        "Prix actuel": EntryReference.CURRENT_PRICE,
                    }[ref_label]
                offset = col_mid.number_input(
                    "Écart (%)",
                    value=float(default_offset if default_offset is not None else -1.0),
                    step=0.1,
                    format="%.2f",
                    key=f"nt_eoff_{index}",
                )
                fixed_price = None
            else:
                price_mode = PriceMode.FIXED_PRICE
                offset = None
                fixed_price = col_mid.number_input(
                    "Prix",
                    min_value=0.0,
                    value=float(price or 0.0),
                    step=float(rules.tick_size),
                    format="%.2f",
                    key=f"nt_eprice_{index}",
                )

        alloc = col_right.number_input(
            "Allocation (%)",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            key=f"nt_alloc_{index}",
        )

        spec = EntrySpec(
            order_type=order_type,
            price_mode=price_mode,
            reference=reference,
            price=fixed_price,
            offset_percent=offset,
            capital_percent=alloc,
        )
        entry_specs.append(spec)
        allocation_total += alloc

if abs(allocation_total - 100.0) > 0.01:
    st.error(f"❌ Total des allocations : {allocation_total:.2f} % (attendu 100 %)")
else:
    st.success(f"✅ Total des allocations : {allocation_total:.2f} %")

# ==========================================================================
# 4. Take Profits
# ==========================================================================

st.subheader("4. Take Profits")

tp_count = st.number_input("Nombre de TP", min_value=1, max_value=20, value=int(state.get("tp_count", 3)))
tp_defaults_percent = [2.34, 4.12, 18.0, 25.0, 35.0]
tp_defaults_sell = [20.0, 30.0, 50.0, 40.0, 60.0]

tp_specs: list[TPSpec] = []
sell_total = 0.0

for index in range(int(tp_count)):
    cols = st.columns([2, 2, 2, 1])
    cols[0].markdown(f"**TP {index + 1}**")

    target_percent = cols[1].number_input(
        "Cible (%)",
        value=float(tp_defaults_percent[index] if index < len(tp_defaults_percent) else 5.0 * (index + 1)),
        step=0.1,
        format="%.2f",
        key=f"nt_tp_{index}",
    )
    sell = cols[2].number_input(
        "Vente (%)",
        min_value=0.0,
        max_value=100.0,
        value=float(tp_defaults_sell[index] if index < len(tp_defaults_sell) else 25.0),
        step=5.0,
        key=f"nt_tpsell_{index}",
    )

    if advanced:
        rule_label = cols[3].selectbox(
            "SL après",
            ["Aucun", "Break-even", "Break-even + frais", "TP précédent", "Prix fixe", "Pourcentage"],
            key=f"nt_tprule_{index}",
        )
    else:
        rule_label = ["Aucun", "Break-even", "TP précédent", "TP précédent"][min(index, 3)]

    rule = {
        "Aucun": SLRuleAfterTP.NO_CHANGE,
        "Break-even": SLRuleAfterTP.BREAK_EVEN,
        "Break-even + frais": SLRuleAfterTP.BREAK_EVEN_WITH_FEES,
        "TP précédent": SLRuleAfterTP.PREVIOUS_TP,
        "Prix fixe": SLRuleAfterTP.FIXED_PRICE,
        "Pourcentage": SLRuleAfterTP.CUSTOM_PERCENT,
    }[rule_label]

    rule_value = None
    if rule in {SLRuleAfterTP.FIXED_PRICE, SLRuleAfterTP.CUSTOM_PERCENT} and advanced:
        rule_value = st.number_input(
            "Valeur de la règle SL",
            value=0.0,
            step=0.1,
            key=f"nt_tpruleval_{index}",
            help="Prix pour « Prix fixe », pourcentage sous le prix moyen pour « Pourcentage ».",
        )

    tp_specs.append(
        TPSpec(
            price_mode=PriceMode.PERCENT,
            reference=TPReference.AVERAGE_PRICE,
            target_percent=target_percent,
            sell_percent=sell,
            sl_rule_after_hit=rule,
            sl_rule_value=rule_value,
        )
    )
    sell_total += sell

if abs(sell_total - 100.0) > 0.01:
    if sell_total > 100.0:
        st.error(f"❌ Les TP vendent {sell_total:.2f} % (> 100 %)")
    else:
        st.warning(f"⚠️ Les TP vendent {sell_total:.2f} % — le reste resterait ouvert")
else:
    st.success("✅ Les TP vendent 100 % de la position")

# ==========================================================================
# 5. Stop Loss
# ==========================================================================

st.subheader("5. Stop Loss")

sl_cols = st.columns([2, 2])

sl_mode_label = sl_cols[0].selectbox(
    "Mode du SL",
    ["Pourcentage sous le prix moyen", "Pourcentage sous Entry 1", "Pourcentage sous la dernière Entry", "Prix fixe"],
)
sl_mode = {
    "Pourcentage sous le prix moyen": SLMode.AVERAGE_PERCENT,
    "Pourcentage sous Entry 1": SLMode.ENTRY1_PERCENT,
    "Pourcentage sous la dernière Entry": SLMode.LAST_ENTRY_PERCENT,
    "Prix fixe": SLMode.FIXED_PRICE,
}[sl_mode_label]

sl_default = -4.0 if sl_mode is not SLMode.FIXED_PRICE else 0.0
sl_value = sl_cols[1].number_input(
    "Prix" if sl_mode is SLMode.FIXED_PRICE else "Écart (%)",
    value=float(sl_default),
    step=0.1,
    format="%.2f",
)

execution_policy = st.radio(
    "Exécution des TP",
    ["MARKET_ON_TRIGGER", "LIMIT_ON_TRIGGER"],
    horizontal=True,
    help=(
        "MARKET_ON_TRIGGER : vente au marché dès la cible. "
        "LIMIT_ON_TRIGGER : vente limitée immédiate (FOK) ; si elle ne remplit pas, "
        "le SL est restauré et le worker réessaie au prochain cycle."
    ),
)

# ==========================================================================
# Simulation
# ==========================================================================

st.caption("Le prix et la simulation se rafraîchissent automatiquement toutes les 1 s.")

@st.fragment(run_every="1s")
def simulation_panel():
    price = live_price(rules.symbol)
    spec = StrategySpec(
        symbol=rules.symbol,
        entries=entry_specs,
        take_profits=tp_specs,
        stop_loss=SLSpec(mode=sl_mode, value=sl_value),
        capital_mode=capital_mode,
        capital_amount=capital_amount,
        capital_percent=capital_percent,
        risk_percent=risk_percent,
        available_quote=available_quote,
        reserve_percent=float(reserve_percent),
        current_price=float(price or 0.0),
        tp_execution_policy=(
            TPExecutionPolicy.MARKET_ON_TRIGGER
            if execution_policy == "MARKET_ON_TRIGGER"
            else TPExecutionPolicy.LIMIT_ON_TRIGGER
        ),
        source=SignalSource.MANUAL,
        source_name="manual",
    )

    plan = StrategyEngine(rules).build(spec)

    # Arrondi au tick AVANT validation : un prix deduit d'un pourcentage tombe
    # rarement sur un multiple exact du tickSize, et Binance refuserait l'ordre.
    for resolved_entry in plan.entries:
        if resolved_entry.price > 0:
            resolved_entry.price = float(rules.round_price(resolved_entry.price, mode="down"))
    for resolved_tp in plan.take_profits:
        if resolved_tp.target_price > 0:
            resolved_tp.target_price = float(
                rules.round_price(resolved_tp.target_price, mode="down")
            )
    if plan.stop_loss.price > 0:
        plan.stop_loss.price = float(rules.round_price(plan.stop_loss.price, mode="down"))

    # Les erreurs de filtre dependent des prix arrondis : on relance la validation.
    plan.errors = [
        e for e in plan.errors if "tickSize" not in e and "minNotional" not in e
    ]
    for resolved_entry in plan.entries:
        if resolved_entry.price > 0 and resolved_entry.qty > 0:
            plan.errors.extend(
                f"Entry {resolved_entry.sequence} : {err}"
                for err in rules.validate_order(
                    resolved_entry.price,
                    resolved_entry.qty,
                    market=resolved_entry.order_type.value == "MARKET",
                )
            )

    st.divider()
    st.subheader("Simulation")

    if plan.errors:
        error_list(plan.errors)
        warning_list(plan.warnings)
        st.stop()

    warning_list(plan.warnings)

    # -- carte de résumé (section 42) ------------------------------------------

    left, right = st.columns([2, 3])

    with left:
        st.markdown(f"### {plan.symbol}")
        st.markdown(
            f"""
    **Prix actuel** : {fmt_price(price)} {quote_asset}

    **Solde {quote_asset}** : {fmt_price(available_quote)}

    **Capital** : {fmt_price(plan.capital_total)} {quote_asset}

    **Capital en réserve** : {fmt_price(plan.capital_reserved)}
    """
        )
        for entry in plan.entries:
            st.markdown(
                f"**E{entry.sequence}** — {fmt_quote(entry.quote_amount, quote_asset)} "
                f"@ {fmt_price(entry.price)} → {fmt_qty(entry.qty)} {rules.base_asset}"
            )
        st.markdown(
            f"""
    **Prix moyen estimé** : {fmt_price(plan.estimated_average_price)}

    **Quantité totale** : {fmt_qty(plan.estimated_total_qty)} {rules.base_asset}

    **SL** : {fmt_price(plan.stop_loss.price)} ({fmt_percent(plan.stop_loss.percent_from_average)})

    **Perte max au SL** : {colored_pnl(-abs(plan.loss_max_estimated), fmt_quote(-abs(plan.loss_max_estimated), quote_asset))}

    **Risque** : {plan.risk_percent_of_capital:.2f} % du capital engagé
    """
        )

    with right:
        st.markdown("### Take Profits")
        tp_rows = []
        for tp in plan.take_profits:
            tp_rows.append(
                {
                    "TP": tp.sequence,
                    "Cible": fmt_percent(tp.target_percent),
                    "Prix": fmt_price(tp.target_price),
                    "Vente": f"{tp.sell_percent:.0f} %",
                    "Quantité": fmt_qty(tp.estimated_qty),
                    "Gain estimé": fmt_quote(tp.gain_estimated, quote_asset),
                    "SL après": tp.sl_rule_after_hit.value,
                }
            )
        pnl_dataframe(tp_rows, width="stretch", hide_index=True)
        pnl_metric(st, "Gain total estimé", plan.gain_total_estimated, fmt_quote(plan.gain_total_estimated, quote_asset))

    # -- scénarios (section 74) ------------------------------------------------

    with st.expander("Scénarios d'exécution", expanded=False):
        for scenario in plan.scenarios:
            st.markdown(
                f"**{scenario.label}** — investi {fmt_quote(scenario.invested, quote_asset)} · "
                f"prix moyen {fmt_price(scenario.average_price)} · "
                f"SL {fmt_price(scenario.sl_price)} · "
                f"perte max {fmt_quote(scenario.max_loss, quote_asset)}"
            )

    # -- aperçu graphique (section 75) -----------------------------------------

    with st.expander("Aperçu des niveaux", expanded=False):
        levels: list[tuple[str, float]] = []
        for tp in reversed(plan.take_profits):
            levels.append((f"TP{tp.sequence}", tp.target_price))
        levels.append(("Prix moyen", plan.estimated_average_price))
        for entry in reversed(plan.entries):
            levels.append((f"E{entry.sequence}", entry.price))
        levels.append(("SL", plan.stop_loss.price))

        max_level = max(v for _, v in levels if v > 0) if levels else 1.0
        for label, value in levels:
            if value <= 0:
                continue
            width = max(int(value / max_level * 100), 2)
            st.markdown(f"`{label:>10}` {fmt_price(value)}")
            st.progress(min(width, 100) / 100)

    # -- risque portefeuille (section 72) --------------------------------------

    try:
        risk = RiskEngine(service.risk_limits()).evaluate(
            plan, service.risk_snapshot(available_quote), symbol=rules.symbol
        )
    except Exception as exc:
        risk = RiskReport()
        risk.refuse(f"Risque non calculable : {exc}")

    st.markdown("### Exposition après ce trade")
    cols = st.columns(4)
    cols[0].metric("Exposition", f"{risk.planned_exposure_percent:.1f} %")
    cols[1].metric("Risque de cette position", f"{risk.planned_risk_percent:.2f} %")
    cols[2].metric("Risque total projeté", f"{risk.projected_total_risk_percent:.2f} %")
    cols[3].metric("Capital libre après", fmt_price(risk.capital_free_after))

    for refusal in risk.refusals:
        st.error(f"❌ {refusal}")
    warning_list(risk.warnings)
    return spec, plan, risk, price


spec, plan, risk, price = simulation_panel()

# ==========================================================================
# Lancement
# ==========================================================================

st.divider()
st.subheader("Lancement")

col_a, col_b, col_c = st.columns([2, 2, 1])
preset_name = col_a.text_input("Nom du preset (optionnel)", "")
source_name = col_b.text_input("Source / label", "manual")
tags_raw = col_c.text_input("Tags", "", help="Séparés par des virgules")

action_label = "Lancer la position"
ready = risk.accepted and plan.is_valid

new_command_confirmation("new_trade")
confirm = st.checkbox(
    "Je confirme le lancement sur Binance Demo" if not settings.dry_run else
    "Je confirme la simulation (DRY_RUN : aucun ordre ne sera envoyé)"
)
if st.button(action_label, type="primary", disabled=not (confirm and ready)):
    spec.preset_name = preset_name
    spec.source_name = source_name or "manual"
    spec.tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    engine = PositionEngine(rules)
    proposed = engine.from_plan(plan, spec)
    submit_to_worker(
        "SUBMIT_POSITION",
        {"position": proposed.model_dump(mode="json"),
         "entry_ids": [e.entry_id for e in proposed.entries],
         "existing_id": None, "independent_position": True,
         "reference_price": float(price or 0)},
        confirmation_key="new_trade",
    )
    st.caption("Le worker revalide prix, solde et risque avant envoi. La demande expire après deux minutes.")
