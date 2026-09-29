"""Text signal inbox, explicit preview and guarded Demo submission."""
from pathlib import Path
import sys
import time

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.command_store import account_scope
from binance_spot_manager.config import get_settings
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.signal_parser import ParsedSignal, TEMPLATES, parse_signal
from binance_spot_manager.signal_plan import prepare_signal
from binance_spot_manager.signal_sizing import (
    SignalSizingPolicy,
    suggest_signal_budget_from_account,
)
from binance_spot_manager.telegram_signals import chat_allowlist, import_telegram
from binance_spot_manager.position_store import get_settings_store
from ui_common import banner, get_service, load_rules, page_header, sidebar_status, colored_pnl

st.set_page_config(page_title="Signaux — Binance Demo", page_icon=":material/description:", layout="wide")
page_header("Signaux", "Texte → vérification → confirmation → worker Binance Demo")
settings = get_settings()
banner(settings)
sidebar_status(settings)
if not settings.is_demo:
    st.error("Signaux réservés à Binance Demo.")
    st.stop()
scope = account_scope(settings)
inbox = SignalInbox()
service = get_service()
preferences = get_settings_store().load()

if preferences.get("signal_auto_execute_enabled", False):
    st.warning(
        "Exécution Telegram automatique active : les nouveaux signaux valides peuvent "
        "être envoyés au worker sans confirmation sur cette page."
    )
else:
    st.info("Aucun message n'est exécuté dès sa réception. Le budget et la validité du signal doivent être confirmés. Un seul signal par texte.")
with st.form("signal_input"):
    template = st.selectbox("Modèle", list(TEMPLATES), format_func=TEMPLATES.get)
    raw = st.text_area("Coller le signal", height=220, max_chars=20000)
    analyze = st.form_submit_button("Analyser et enregistrer")
if analyze:
    if not raw.strip():
        st.warning("Coller un signal avant l'analyse.")
    elif template != "auto" and parse_signal(raw).template != template:
        st.error("Le texte ne correspond pas au modèle sélectionné. Choisir Automatique ou le bon modèle.")
    else:
        item = inbox.receive(scope, raw, template=template)
        st.session_state["selected_signal"] = item["id"]
        st.success("Analyse enregistrée. Les doublons retrouvent le même signal.")

with st.expander("Recevoir depuis Telegram"):
    automatic = bool(preferences.get("signal_telegram_auto_enabled", False))
    st.caption("Le bot doit avoir accès aux messages, ou les recevoir par transfert. La réception seule ne crée jamais d'ordre.")
    if automatic:
        diagnostics = getattr(service.runtime(), "telegram_diagnostics", {})
        state = diagnostics.get("state", "EN ATTENTE")
        st.info(f"Relève automatique assurée par le worker · état : {state}")
        if diagnostics.get("last_error"):
            st.error(diagnostics["last_error"])
        if st.button("Actualiser la boîte de réception", icon=":material/refresh:"):
            st.rerun()
    elif st.button("Relever les messages Telegram"):
        try:
            if not preferences.get("signal_telegram_enabled", False):
                raise ValueError("Réception désactivée dans Settings → Signaux.")
            chats = chat_allowlist(preferences.get("signal_telegram_chats", ""))
            items = import_telegram(settings.telegram_bot_token, chats, inbox, scope)
            st.success(f"{len(items)} message(s) autorisé(s) traité(s). Aucun ordre envoyé.")
        except ValueError as exc:
            st.error(str(exc))

rows = inbox.recent(scope)
if not rows:
    st.caption("Aucun signal reçu pour ce compte Demo.")
    st.stop()
by_id = {row["id"]: row for row in rows}
selected = st.session_state.get("selected_signal")
ids = list(by_id)
selected = st.selectbox("Signal à examiner", ids, index=ids.index(selected) if selected in ids else 0,
    format_func=lambda key: f"{by_id[key]['parsed']['symbol'] or 'Non reconnu'} · {by_id[key]['source']} · {key[:8]}")
row = by_id[selected]
if row.get("auto_state") == "REJECTED":
    st.warning(f"Exécution automatique refusée : {row.get('auto_detail') or 'raison indisponible'}")
elif row.get("auto_state") == "PROCESSING":
    st.info("Exécution automatique en cours de préparation par le worker.")
if row["payload"] is None and st.button("Réanalyser ce signal", help="Reprendre le texte enregistré avec les formats actuellement reconnus."):
    try:
        inbox.reanalyse(scope, selected)
        st.session_state.pop("signal_preview", None)
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))
parsed = ParsedSignal(**row["parsed"])
with st.expander("Texte original"):
    st.text(row["raw"])
st.write(f"Modèle : {TEMPLATES.get(parsed.template, parsed.template)} · Direction : {parsed.direction or 'inconnue'} · Plateforme : {parsed.exchange or 'non précisée'}")
st.write({"Paire": parsed.symbol, "Entrées": parsed.entries, "TP": parsed.targets,
          "SL": parsed.stop, "Mention SL": parsed.stop_timeframe or "aucune",
          "Date source (UTC)": parsed.published_at or "non vérifiée"})
for warning in parsed.warnings:
    st.warning(warning)
for error in parsed.errors:
    st.error(error)
if parsed.errors:
    st.stop()

if row["payload"]:
    st.info("Ce signal a déjà été confirmé. Il ne peut pas créer une seconde demande.")
    commands = service.commands.list_recent(scope)
    command = next((c for c in commands if c["request_key"] == f"signal:{selected}"), None)
    if command:
        st.write(f"Commande {command['id']} : {command['state']}")
        st.write(command["result"])
    elif row["payload"]["signal_confirmation_expires_at"] > time.time():
        if st.button("Transmettre la confirmation conservée au worker"):
            try:
                service.submit_command("SUBMIT_POSITION", row["payload"], request_key=f"signal:{selected}")
                st.rerun()
            except Exception as exc:
                st.error(f"Transmission impossible : {exc}")
    else:
        st.warning("Confirmation expirée sans transmission. Vérifier Opérations ; aucun rejeu automatique.")
    st.stop()

st.subheader("Préparer l'exécution")
st.caption("Entrées LIMIT : budget réparti également, expiration après 24 h. TP répartis également sur la position, dernier TP à 100 % du restant. Les entrées encore ouvertes sont annulées après TP1. Les TP sont surveillés par le worker : cette page ne crée pas un OCO par tranche.")
st.warning("Une limite d'achat au-dessus du marché peut être exécutée immédiatement. Les fills partiels et les frais peuvent réduire les quantités réellement vendables. Le worker doit rester actif pour la stratégie.")
quote_asset = "USDC" if parsed.symbol.endswith("USDC") else "USDT"
sizing_policy = SignalSizingPolicy.from_mapping(preferences)
sizing_suggestion = None
sizing_warning = ""
try:
    sizing_balances = service.client.get_balances()
    sizing_prices = service.client.get_prices()
    sizing_suggestion, unpriced_assets = suggest_signal_budget_from_account(
        sizing_policy,
        balances=sizing_balances,
        prices=sizing_prices,
        quote_asset=quote_asset,
        reserve_percent=service.risk_limits().min_reserve_percent,
    )
    if unpriced_assets:
        sizing_warning = (
            "Valorisation partielle : actifs sans cours USDT exclus du capital total : "
            + ", ".join(unpriced_assets)
        )
except Exception as exc:
    sizing_warning = f"Budget automatique indisponible : {exc}"

default_budget = sizing_suggestion.budget if sizing_suggestion else 0.0
budget_key = f"budget_{selected}"
if sizing_suggestion and st.button(
    "Recalculer le budget proposé",
    icon=":material/calculate:",
    help="Relit les soldes et réapplique la stratégie enregistrée.",
):
    st.session_state[budget_key] = float(default_budget)
budget = st.number_input(
    f"Budget total ({quote_asset})",
    min_value=0.0,
    value=float(default_budget),
    step=10.0,
    key=budget_key,
)
if sizing_suggestion:
    if sizing_policy.mode == "FIXED":
        sizing_text = f"montant fixe de {sizing_policy.fixed_budget:.2f} {quote_asset}"
    else:
        sizing_text = f"{sizing_suggestion.applied_percent:.2f} % du portefeuille"
        if sizing_suggestion.reduced:
            sizing_text += " (part réduite active)"
    st.caption(
        f"Proposition automatique : {sizing_suggestion.budget:.2f} {quote_asset} · "
        f"{sizing_text} · libre : {sizing_suggestion.free_capital_percent:.1f} % · "
        f"réserve conservée : {service.risk_limits().min_reserve_percent:.1f} %. "
        "Tu peux modifier ce montant avant la simulation."
    )
    if sizing_suggestion.capped_by_reserve:
        st.warning("Le budget proposé a été plafonné pour conserver la réserve de capital.")
if sizing_warning:
    st.warning(sizing_warning)
validity = st.checkbox("J'ai vérifié la date source et ce signal est encore valable maintenant", key=f"valid_{selected}")
touch = st.checkbox(f"Je choisis un stop au prix {parsed.stop}, sans attendre une clôture {parsed.stop_timeframe}",
                    key=f"touch_{selected}") if parsed.stop_timeframe else True
signature = (scope, selected, budget, validity, touch)
if st.button("Vérifier sur Binance Demo et simuler", disabled=not (budget > 0 and validity and touch)):
    st.session_state.pop("signal_preview", None)
    try:
        rules, error = load_rules(parsed.symbol)
        if error or rules is None:
            raise ValueError(error or "Paire indisponible")
        balances = service.client.get_balances()
        current_price = service.current_price(parsed.symbol)
        plan, payload = prepare_signal(parsed, rules, budget=budget,
            available_quote=float(balances.get(rules.quote_asset, {}).get("free", 0)),
            reserve_percent=service.risk_limits().min_reserve_percent,
            current_price=current_price or 0, signal_id=selected, source=row["source"],
            touch_stop=touch, validity_confirmed=validity)
        st.session_state["signal_preview"] = (signature, plan, payload)
    except Exception as exc:
        st.error(f"Simulation refusée : {exc}")
preview = st.session_state.get("signal_preview")
if preview and preview[0] == signature:
    _, plan, payload = preview
    st.dataframe([{"Entrée": entry.sequence, "Prix limite": entry.price, "Quantité": entry.qty,
                   "Montant": entry.notional} for entry in plan.entries], hide_index=True)
    st.dataframe([{"TP": tp.sequence, "Prix": tp.target_price, "Part initiale (%)": tp.sell_percent}
                  for tp in plan.take_profits], hide_index=True)
    st.markdown(f"SL : {plan.stop_loss.price} · Perte théorique au SL hors frais/glissement : "
                + colored_pnl(-abs(plan.loss_max_estimated), f"{-abs(plan.loss_max_estimated):.4f} {plan.quote_asset}"))
    st.caption("Simulation valable 120 secondes. Le worker recontrôle prix, solde, risque et frais avant tout achat. Une simulation valide peut encore être refusée.")
    confirm = st.checkbox("Je confirme ces achats LIMIT et cette stratégie sur Binance Demo", key=f"confirm_{payload['position']['position_id']}")
    if st.button("Transmettre au worker Demo", disabled=not confirm):
        try:
            if time.time() >= payload["signal_confirmation_expires_at"]:
                raise ValueError("Simulation expirée : relancer la vérification.")
            if "signal_v1" not in service.runtime().command_capabilities:
                raise ValueError("Redémarrer le worker avant de confirmer un signal (nouveaux garde-fous).")
            if "independent_positions_v1" not in service.runtime().command_capabilities:
                raise ValueError("Redémarrer le worker pour activer les positions indépendantes.")
            status = service.worker_status()
            if not status.running or status.heartbeat_age is None or status.heartbeat_age >= 20:
                raise ValueError("Démarrer le worker avant de confirmer un signal.")
            frozen = inbox.freeze(scope, selected, payload)
            service.submit_command("SUBMIT_POSITION", frozen, request_key=f"signal:{selected}")
            st.rerun()
        except Exception as exc:
            st.error(f"Commande non confirmée : {exc}. Consulter Opérations avant toute autre action.")
