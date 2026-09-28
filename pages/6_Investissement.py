"""Investissement Spot Demo : achat Market et une seule sortie autonome."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.config import get_settings  # noqa: E402
from binance_spot_manager.investment_plan import (  # noqa: E402
    investment_risk_context, make_investment_position, preview_investment,
    preview_simple_buy,
)
from binance_spot_manager.models import (  # noqa: E402
    Commission, EntryStatus, EventType, SyncStatus,
)
from binance_spot_manager.position_engine import PositionEngine  # noqa: E402
from binance_spot_manager.risk_engine import RiskEngine  # noqa: E402
from ui_common import (  # noqa: E402
    banner, fmt_price, fmt_qty, get_service, load_rules,
    page_header, sidebar_status, symbol_status_box,
    submit_to_worker, new_command_confirmation,
)

settings = get_settings()
service = get_service()
st.set_page_config(page_title="Investissement — BinanceSpotManager", page_icon="📈", layout="wide")
page_header("Acheter sur Binance Demo", "Achat simple, ou investissement avec TP seul / SL seul")
banner(settings)
sidebar_status(settings)
new_command_confirmation("investment")

if not settings.is_demo:
    st.error("Cette page est réservée à Binance Demo.")
    st.stop()

purchase_mode = st.radio(
    "Type d'achat",
    ["Achat simple — sans sortie", "Investissement — TP seul", "Investissement — SL seul"],
    horizontal=True,
)
symbol = st.text_input("Paire (ex. BTCUSDT, BTCUSDC, ETHBTC)", value="BTCUSDT").strip().upper()
rules, rules_error = load_rules(symbol)
if rules_error or rules is None:
    st.error(rules_error or "Règles Binance indisponibles")
    st.stop()

portfolio = service.portfolio()
try:
    account_balances = service.client.get_balances() if settings.has_credentials else {}
except Exception as exc:
    st.error(f"Soldes Binance Demo indisponibles : {exc}")
    st.stop()
balances = {asset: values.get("free", 0.0) for asset, values in account_balances.items()}
if not account_balances and settings.dry_run:
    balances = dict(portfolio.base_balances)
    balances[settings.quote_asset] = portfolio.quote_free
symbol_status_box(symbol, rules, balances)
if not account_balances and not settings.dry_run:
    st.error("Solde Binance indisponible : achat désactivé.")
    st.stop()

price = service.current_price(symbol)
if not price or price <= 0:
    st.error("Prix actuel indisponible : achat désactivé.")
    st.stop()

available_quote = float(balances.get(rules.quote_asset, 0.0))
reserve_percent = float(settings.capital_reserve_percent)

if purchase_mode.startswith("Achat simple"):
    st.subheader("Achat simple")
    st.info(
        f"Tu achètes {rules.base_asset} avec {rules.quote_asset}. "
        "Aucun TP, aucun SL et aucune position suivie par le bot ne seront créés."
    )
    existing_simple = service.find_by_symbol(symbol)
    if existing_simple:
        st.warning(
            f"Une stratégie {symbol} existe déjà ({existing_simple.position_id}). "
            "Cet achat simple ira uniquement dans le portefeuille ; son OCO et sa position ne changent pas."
        )
    usable_simple = max(available_quote * (1 - reserve_percent / 100), 0.0)
    budget = st.number_input(
        f"Budget à dépenser approximativement ({rules.quote_asset})",
        min_value=0.0, value=float(min(100.0, usable_simple)), step=10.0,
        help="Achat Market par quantité, avec 2 % de marge : le montant final peut différer légèrement.",
    )
    st.caption(
        f"Solde libre : {fmt_price(available_quote)} {rules.quote_asset} · "
        f"réserve conservée : {reserve_percent:.0f} %"
    )
    simple = preview_simple_buy(
        rules, current_price=price, capital=budget,
        available_quote=available_quote, reserve_percent=reserve_percent,
    )
    st.write(
        f"Prix indicatif : {fmt_price(price)} {rules.quote_asset}/{rules.base_asset} · "
        f"quantité estimée : {fmt_qty(simple.quantity)} {rules.base_asset} · "
        f"dépense estimée : {fmt_price(simple.estimated_spend)} {rules.quote_asset}"
    )
    for error in simple.errors:
        st.error(error)

    signature = (symbol, round(budget, 10))
    if st.session_state.get("simple_buy_signature") != signature:
        st.session_state.simple_buy_signature = signature
        st.session_state.simple_buy_nonce = uuid.uuid4().hex
        st.session_state.simple_buy_done = False
    client_id = f"BSM-D-{symbol[:8]}-{st.session_state.simple_buy_nonce[:16]}-BUY"
    if st.session_state.simple_buy_done:
        st.success("Achat déjà envoyé pour cette confirmation. Vérifie le portefeuille avant un autre achat.")
        if st.button("Préparer un nouvel achat"):
            st.session_state.simple_buy_nonce = uuid.uuid4().hex
            st.session_state.simple_buy_done = False
            st.rerun()
    confirm_simple = st.checkbox(
        "Je confirme l'achat Market sur Binance Demo, sans TP ni SL"
        if not settings.dry_run else "Je confirme la simulation sans ordre réel",
        key="simple_buy_confirm",
    )
    if st.button(
        "Acheter simplement", type="primary",
        disabled=not (simple.eligible and confirm_simple and not st.session_state.simple_buy_done),
    ):
        try:
            latest_balances = service.client.get_balances() if settings.has_credentials else {}
        except Exception as exc:
            st.error(f"Solde Demo indisponible : {exc}. Aucun ordre envoyé.")
            st.stop()
        latest_available = (
            available_quote if settings.dry_run and not settings.has_credentials
            else float(latest_balances.get(rules.quote_asset, {}).get("free", 0))
        )
        latest_price = service.current_price(symbol)
        fresh = preview_simple_buy(
            rules, current_price=latest_price or 0, capital=budget,
            available_quote=latest_available, reserve_percent=reserve_percent,
        )
        if not fresh.eligible or fresh.quantity != simple.quantity:
            st.error("Prix ou solde modifié : vérifie le nouvel aperçu avant de recommencer.")
            st.stop()
        if submit_to_worker(
            "SIMPLE_BUY",
            {"symbol": symbol, "quantity": fresh.quantity, "budget": budget,
             "reference_price": latest_price, "client_order_id": client_id},
            confirmation_key=f"simple_buy_{st.session_state.simple_buy_nonce}",
        ):
            st.session_state.simple_buy_done = True
    st.stop()

if rules.quote_asset not in {"USDT", "USDC"}:
    st.error(
        "Les sorties TP/SL suivies acceptent les paires en USDT ou USDC. "
        "L'achat simple accepte aussi les autres devises de cotation."
    )
    st.stop()

existing = service.find_by_symbol(symbol)
if existing:
    st.warning(
        f"Une position {symbol} existe déjà ({existing.position_id}). "
        "Cette page ne crée pas de seconde position et ne change pas ses ordres."
    )
    st.stop()

st.subheader("1. Achat")
st.caption(f"Prix indicatif : {fmt_price(price)} {rules.quote_asset} · achat Market uniquement")
usable = max(available_quote * (1 - reserve_percent / 100), 0.0)
capital = st.number_input(
    f"Budget maximum indicatif ({rules.quote_asset})",
    min_value=0.0, value=float(min(100.0, usable)), step=10.0,
    help="La quantité achetée garde une marge de 2 % pour le mouvement du prix et les frais.",
)
st.caption(f"Solde libre : {fmt_price(available_quote)} · réserve : {reserve_percent:.0f} %")

st.subheader("2. Sortie")
exit_mode = "TP_ONLY" if "TP seul" in purchase_mode else "SL_ONLY"
default_exit = float(rules.round_price(
    price * (1.20 if exit_mode == "TP_ONLY" else 0.98),
    mode="up" if exit_mode == "TP_ONLY" else "down",
))
exit_price = st.number_input(
    f"{'Prix du TP' if exit_mode == 'TP_ONLY' else 'Prix de déclenchement du SL'} ({rules.quote_asset})",
    min_value=0.0, value=default_exit, step=float(rules.tick_size),
    key=f"investment_exit_{exit_mode}",
)
if exit_mode == "TP_ONLY":
    st.info("Après l'achat, le worker place un ordre de vente Limit GTC sur Binance. Aucun SL n'est créé.")
else:
    st.info("Après l'achat, le worker place un STOP_LOSS_LIMIT GTC sur Binance. Aucun TP n'est créé.")
    st.warning("Un SL déclenché n'assure pas une vente à ce prix : la limite peut rester sans exécution.")

preview = preview_investment(
    rules, current_price=price, capital=capital,
    available_quote=available_quote, reserve_percent=reserve_percent,
    exit_mode=exit_mode, exit_price=exit_price,
)
for error in preview.errors:
    st.error(error)

st.subheader("3. Vérification")
cols = st.columns(4)
cols[0].metric("Achat estimé", f"{fmt_qty(preview.quantity)} {rules.base_asset}")
cols[1].metric("Dépense estimée", f"{fmt_price(preview.estimated_spend)} {rules.quote_asset}")
cols[2].metric("Sortie", f"{fmt_price(preview.exit_price)} {rules.quote_asset}")
cols[3].metric(
    "Risque estimé" if exit_mode == "SL_ONLY" else "Capital exposé",
    f"{fmt_price(preview.risk_quote)} {rules.quote_asset}",
)
if exit_mode == "TP_ONLY":
    st.warning("Sans SL, la totalité du capital investi peut être perdue.")

try:
    risk_prices = service.client.get_prices()
    risk_plan, risk_snapshot, quote_rate = investment_risk_context(
        preview, capital=capital, reserve_percent=reserve_percent,
        balances=account_balances, prices=risk_prices,
        positions=service.positions.list_all(),
    )
    risk = RiskEngine(service.risk_limits()).evaluate(
        risk_plan, risk_snapshot, symbol=rules.symbol,
    )
    st.caption(
        f"Risque et exposition calculés en USDT · "
        f"1 {rules.quote_asset} = {quote_rate:.6f} USDT (Binance Demo)"
    )
except Exception as exc:
    st.error(f"Risque indisponible : {exc}. Achat avec sortie désactivé.")
    st.stop()
for refusal in risk.refusals:
    st.error(refusal)
for warning in risk.warnings:
    st.warning(warning)

worker = service.worker_status()
worker_ready = settings.dry_run or (
    worker.running and worker.heartbeat_age is not None and worker.heartbeat_age < 20
)
if not worker_ready:
    st.error("Worker inactif ou heartbeat ancien : démarre-le depuis le Dashboard avant l'achat.")

confirm = st.checkbox(
    "Je confirme l'achat et la sortie sur Binance Demo"
    if not settings.dry_run else "Je confirme la simulation DRY_RUN (aucun ordre envoyé)"
)
if st.button(
    "Acheter et préparer la sortie", type="primary",
    disabled=not (confirm and preview.eligible and risk.accepted and worker_ready),
):
    # Revalidation au clic : solde, prix, position existante et worker ont pu changer.
    latest_price = service.current_price(symbol)
    latest_portfolio = service.portfolio()
    latest_worker = service.worker_status()
    if service.find_by_symbol(symbol):
        st.error("Une position vient d'être créée sur cette paire. Achat annulé.")
        st.stop()
    if not settings.dry_run and (
        latest_portfolio.errors or not latest_worker.running
        or latest_worker.heartbeat_age is None or latest_worker.heartbeat_age >= 20
    ):
        st.error("Solde ou worker indisponible. Achat annulé.")
        st.stop()
    if not latest_price or latest_price <= 0:
        st.error("Prix indisponible. Achat annulé.")
        st.stop()
    try:
        latest_balances = service.client.get_balances()
        latest_prices = service.client.get_prices()
    except Exception as exc:
        st.error(f"Soldes ou taux Binance Demo indisponibles : {exc}. Achat annulé.")
        st.stop()
    latest_available = float(latest_balances.get(rules.quote_asset, {}).get("free") or 0)
    latest = preview_investment(
        rules, current_price=latest_price, capital=capital,
        available_quote=latest_available, reserve_percent=reserve_percent,
        exit_mode=exit_mode, exit_price=exit_price,
    )
    try:
        latest_risk_plan, latest_risk_snapshot, _ = investment_risk_context(
            latest, capital=capital, reserve_percent=reserve_percent,
            balances=latest_balances, prices=latest_prices,
            positions=service.positions.list_all(),
        )
        latest_risk = RiskEngine(service.risk_limits()).evaluate(
            latest_risk_plan, latest_risk_snapshot, symbol=rules.symbol,
        )
    except ValueError as exc:
        st.error(f"Risque indisponible : {exc}. Achat annulé.")
        st.stop()
    if not latest.eligible or not latest_risk.accepted:
        st.error("Le prix, le solde ou le risque a changé : vérifie le plan puis recommence.")
        st.stop()

    position = make_investment_position(latest, current_price=latest_price)
    submit_to_worker(
        "SUBMIT_POSITION",
        {"position": position.model_dump(mode="json"), "entry_ids": [e.entry_id for e in position.entries],
         "existing_id": None, "reference_price": latest_price},
        confirmation_key="investment",
    )
