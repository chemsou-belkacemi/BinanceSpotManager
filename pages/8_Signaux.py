"""Text signal inbox, explicit preview and guarded Demo submission."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager import signal_routing
from binance_spot_manager import csi_client
from binance_spot_manager.candle_stop import kline_interval
from binance_spot_manager.command_store import account_scope
from binance_spot_manager.config import get_settings
from binance_spot_manager.models import EventType
from binance_spot_manager.risk_engine import RiskLimits
from binance_spot_manager.signal_auto_execution import candle_backup_percent, channel_name
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.signal_parser import ParsedSignal, TEMPLATES, parse_signal
from binance_spot_manager.signal_plan import TRAIL_STOP_KEY, prepare_signal, signal_identity, signal_sl_after_tp
from binance_spot_manager.signal_sizing import (
    SignalSizingPolicy,
    suggest_signal_budget_from_account,
)
from binance_spot_manager.telegram_signals import chat_allowlist, import_telegram
from binance_spot_manager.position_store import get_settings_store
from ui_common import banner, get_service, load_rules, page_header, sidebar_status, colored_pnl

from ui_common import require_login  # noqa: E402

# Connexion exigee avant tout affichage (comptes : scripts/creer_compte.py).
require_login()

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
        "Exécution automatique active : seuls les signaux sans motif de revue partent sans "
        "confirmation. Risque élevé, confiance faible ou inconnue : « À confirmer » sur cette page."
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
        try:
            item = inbox.receive(scope, raw, template=template)
        except ValueError as exc:  # signal CSI hors dépôt TXT, notamment
            st.error(str(exc))
        else:
            st.session_state["selected_signal"] = item["id"]
            st.success("Analyse enregistrée. Les doublons retrouvent le même signal.")

with st.expander("Recevoir depuis Telegram"):
    from binance_spot_manager.telegram_commands import owner_id

    # Le worker lit le bot dès que la relève automatique OU les commandes Telegram sont actives : un second lecteur
    # (ce bouton) déclencherait des conflits chez Telegram (un seul lecteur par bot).
    automatic = bool(preferences.get("signal_telegram_auto_enabled", False)) or (
        owner_id(preferences, settings.telegram_chat_id) is not None)
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

if preferences.get("signal_drop_enabled", False):
    drop_diagnostics = getattr(service.runtime(), "telegram_diagnostics", {}).get("drop", {})
    st.caption(
        f"Dépôt direct (générateur ML) actif · état : {drop_diagnostics.get('state', 'EN ATTENTE')} · "
        f"importés : {drop_diagnostics.get('imported_total', 0)} · rejetés : {drop_diagnostics.get('rejected_total', 0)}"
    )
    if drop_diagnostics.get("last_error"):
        st.error(drop_diagnostics["last_error"])

# Once per session (a new version restarts sessions): signals refused by an older parser are re-read.
if not st.session_state.get("signals_refreshed"):
    inbox.refresh_refused(scope)
    st.session_state["signals_refreshed"] = True
rows = inbox.recent(scope)
if not rows:
    st.caption("Aucun signal reçu pour ce compte Demo.")
    st.stop()
now = time.time()
review_count = sum(1 for item in rows if signal_routing.is_pending_review(item, now=now))
only_review = st.toggle(f"Seulement à confirmer ({review_count})", key="signal_review_filter")
visible = [item for item in rows if not only_review or signal_routing.is_pending_review(item, now=now)]
if not visible:
    st.caption("Aucun signal à confirmer.")
    st.stop()
by_id = {item["id"]: item for item in visible}
selected = st.session_state.get("selected_signal")
ids = list(by_id)
selected = st.selectbox("Signal à examiner", ids, index=ids.index(selected) if selected in ids else 0,
    format_func=lambda key: (f"{signal_routing.row_state_label(by_id[key], now=now)} · "
                             f"{by_id[key]['parsed']['symbol'] or 'Non reconnu'} · "
                             f"{channel_name(by_id[key], preferences) or by_id[key]['source']} · {key[:8]}"))
row = by_id[selected]
stored_route = signal_routing.decision_from_json(row.get("route") or "")
if row.get("auto_state") == "REVIEW" and row["payload"] is None:
    st.warning(f"À confirmer : {row.get('auto_detail') or 'motifs indisponibles'}")
    if stored_route:
        for category, reasons in signal_routing.grouped_reasons(stored_route.reasons).items():
            st.markdown(f"**{signal_routing.CATEGORY_LABELS.get(category, category)}**")
            for reason in reasons:
                bound = (f" (valeur {reason.value:.2f} · seuil {reason.threshold:.2f})"
                         if isinstance(reason.value, (int, float)) and isinstance(reason.threshold, (int, float)) else "")
                st.markdown(f"- {reason.message}{bound}")
    st.caption("Confiance = déclaration (statut CSI, groupe de confiance, liste d'actifs validés), pas une mesure. "
               "Les seuils de risque décident qui confirme ; ils ne disent rien du résultat.")
elif row.get("auto_state") == "REJECTED":
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
source_date_label = "Date source (UTC)"
source_date = parsed.published_at or "non vérifiée"
if row.get("source") == "telegram" and float(row.get("source_timestamp") or 0) > 0:
    source_date_label = "Date Telegram (UTC)"
    source_date = datetime.fromtimestamp(
        float(row["source_timestamp"]), tz=timezone.utc,
    ).isoformat()
with st.expander("Texte original"):
    st.text(row["raw"])
st.write(f"Modèle : {TEMPLATES.get(parsed.template, parsed.template)} · Direction : {parsed.direction or 'inconnue'} · Plateforme : {parsed.exchange or 'non précisée'}")
st.write({"Paire": parsed.symbol, "Entrées": parsed.entries, "TP": parsed.targets,
          "SL": parsed.stop, "Mention SL": parsed.stop_timeframe or "aucune",
          "Trader / canal": channel_name(row, preferences) or row["source"], source_date_label: source_date})
if parsed.is_csi:
    # Contrat CSI : la fenêtre et l'écart sont recontrôlés par le worker avant l'achat.
    def _utc(stamp):
        return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    st.write({"SIGNAL_ID": parsed.signal_id, "Valide de (UTC)": _utc(parsed.valid_from),
              "Acceptation jusqu'à (UTC)": _utc(parsed.expires_at),
              "Entrée valable jusqu'à (UTC)": _utc(parsed.entry_expires_at),
              "Écart max à ENTRY_1 (bps)": parsed.max_entry_deviation_bps,
              "Poids des TP": parsed.tp_weights,
              "Politique de sortie": f"{parsed.exit_policy_id} ({parsed.exit_policy_hash})",
              "Statut de validation": parsed.validation_status, "Actualités": parsed.news_status})
    if parsed.validation_status != "DEMO_ELIGIBLE":
        st.warning("Statut de validation autre que DEMO_ELIGIBLE : jamais exécuté automatiquement, "
                   "confirmation manuelle avec acquittement explicite.")
    if row["payload"] is None and time.time() < parsed.expires_at:
        st.info(f"À confirmer avant {_utc(parsed.expires_at)} (EXPIRES_AT).")
for warning in parsed.warnings:
    st.warning(warning)
for error in parsed.errors:
    st.error(error)
if parsed.errors:
    st.stop()

with st.container(border=True):
    st.markdown(
        "**Avis de CSI** — analyse indépendante (vetos, taux de base historique de la même géométrie, "
        "bilan du groupe). Un avis ne crée aucun ordre : il peut seulement retenir une exécution automatique."
    )
    if row.get("csi_verdict"):
        st.markdown(
            f"{csi_client.VERDICT_ICONS.get(row['csi_verdict'], '')} "
            f"**{csi_client.verdict_label(row['csi_verdict'])}** · {row.get('csi_evaluated_at') or ''}"
        )
        st.write(row.get("csi_detail") or "")
    else:
        st.caption("Pas encore d'avis CSI pour ce signal.")
    if st.button(
        "Actualiser l'avis de CSI" if row.get("csi_verdict") else "Demander l'avis de CSI",
        key=f"csi_{selected}", icon=":material/psychology:",
    ):
        try:
            # Signal collé à la main = validé par le propriétaire (sa paire peut être ajoutée chez CSI) ;
            # signal reçu par Telegram = non validé : CSI n'ajoute rien.
            opinion = csi_client.CsiClient.from_env().evaluate(
                row["raw"], source=csi_client.source_label(row, preferences),
                user_validated=row.get("source") != "telegram",
            )
            inbox.set_csi_opinion(scope, selected, opinion.verdict, opinion.summary, opinion.evaluated_at)
            st.rerun()
        except csi_client.CsiUnavailable as exc:
            st.warning(f"Avis CSI indisponible : {exc}")
        except ValueError as exc:
            st.warning(str(exc))
    st.caption(
        "Un taux de base historique ne dit pas si ce signal réussira. Détail : page CSI. "
        "Paire hors univers sur un signal Telegram : le coller sur la page CSI vaut validation de la paire."
    )

if row["payload"]:
    st.info("Ce signal a déjà été confirmé. Il ne peut pas créer une seconde demande.")
    command = service.commands.get_by_request_key(scope, f"signal:{selected}")
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

if parsed.is_csi and time.time() >= parsed.expires_at:
    st.error("EXPIRES_AT dépassé : ce signal CSI n'est plus confirmable (le worker le refuserait).")
    st.stop()

st.subheader("Préparer l'exécution")
if parsed.is_csi:
    st.caption("Entrée LIMIT unique valable jusqu'à ENTRY_EXPIRES_AT. Parts des TP selon TP_WEIGHTS, dernier TP à 100 % du restant. "
               "La politique de sortie (EXIT_POLICY_ID) fixe le déplacement du SL. Les TP sont surveillés par le worker.")
else:
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
candle = kline_interval(parsed.stop_timeframe) if parsed.stop_timeframe else None
if not parsed.stop_timeframe:
    touch, stop_ready = True, True
elif candle:
    stop_choice = st.radio(
        "Déclenchement du SL",
        [f"À la clôture d'une bougie {candle}, comme le signal (surveillée par le worker)",
         f"Stop au prix : dès que le prix touche {parsed.stop}"],
        key=f"stop_mode_{selected}",
    )
    touch, stop_ready = stop_choice.startswith("Stop au prix"), True
else:
    touch = st.checkbox(f"Je choisis un stop au prix {parsed.stop} (clôture « {parsed.stop_timeframe} » non reconnue)",
                        key=f"touch_{selected}")
    stop_ready = touch
trail_stop = bool(preferences.get(TRAIL_STOP_KEY, True))
sl_after_tp = signal_sl_after_tp(preferences.get("signal_sl_after_tp"))
signature = (scope, selected, budget, validity, touch, trail_stop, sl_after_tp)
if st.button("Vérifier sur Binance Demo et simuler", disabled=not (budget > 0 and validity and stop_ready)):
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
            touch_stop=touch, validity_confirmed=validity, trail_stop=trail_stop,
            sl_after_tp=sl_after_tp, account_scope=scope, signal_key=signal_identity(row),
            cancel_entry_if_tp1_first=bool(preferences.get("signal_cancel_entry_if_tp1_first", False)),
            source_name=channel_name(row, preferences),
            candle_backup_percent=candle_backup_percent(preferences))
        # Même lecture du risque que le routage automatique (et que le worker pour les limites dures).
        try:
            limits = service.risk_limits()
            limits = limits if isinstance(limits, RiskLimits) else RiskLimits(
                min_reserve_percent=float(getattr(limits, "min_reserve_percent", 20.0)))
            positions_now = service.positions.list_all()
            if getattr(service.positions, "read_errors", None):
                raise ValueError("stockage des positions illisible")
            live_reasons, live_metrics = signal_routing.assess_risk(
                kind=signal_routing.signal_kind(row), payload=payload,
                plan_average_price=plan.estimated_average_price, current_price=current_price or 0,
                balances=balances, prices=service.client.get_prices(), positions=positions_now,
                active_commands=service.commands.active(scope, "SUBMIT_POSITION"), limits=limits,
                policy=signal_routing.RoutingPolicy.from_mapping(preferences, limits), raw=row.get("raw") or "",
            )
        except Exception as exc:  # noqa: BLE001 - risque non mesurable : acquittement exigé
            live_reasons = [signal_routing.Reason("D_RISK", signal_routing.DONNEES, f"Risque non évaluable : {exc}")]
            live_metrics = {}
        st.session_state["signal_preview"] = (signature, plan, payload, live_reasons, live_metrics,
                                              sizing_suggestion.budget if sizing_suggestion else None)
    except Exception as exc:
        st.error(f"Simulation refusée : {exc}")
preview = st.session_state.get("signal_preview")
if preview and preview[0] == signature:
    _, plan, payload, live_reasons, live_metrics, budget_proposed = preview
    st.dataframe([{"Entrée": entry.sequence, "Prix limite": entry.price, "Quantité": entry.qty,
                   "Montant": entry.notional} for entry in plan.entries], hide_index=True)
    st.dataframe([{"TP": tp.sequence, "Prix": tp.target_price, "Part initiale (%)": tp.sell_percent,
                   "SL après ce TP": tp.sl_rule_value if tp.sl_rule_value else "inchangé"}
                  for tp in plan.take_profits], hide_index=True)
    candle_interval = payload["position"]["stop_loss"].get("candle_interval")
    if parsed.is_csi:
        st.caption(f"Signal CSI : le SL après chaque TP suit la politique de sortie {parsed.exit_policy_id}.")
    elif trail_stop:
        st.caption("Suivi du SL activé (Settings → Signaux) : SL à l'Entry 1 après TP1, puis deux TP en arrière à partir de TP3"
                   + (" ; le SL déplacé devient un stop au prix." if candle_interval else "."))
    else:
        st.caption(f"Suivi du SL désactivé : après un TP, le SL suit la règle « {sl_after_tp.value} » (Settings → Signaux).")
    if candle_interval:
        st.info(f"SL à la clôture {candle_interval} : aucun ordre stop sur Binance. Le worker vend au marché si une "
                f"bougie {candle_interval} clôture à {plan.stop_loss.price} ou dessous ; la vente peut se faire sous le "
                "SL, et la position n'est pas protégée si le worker est arrêté.")
    st.markdown(f"SL : {plan.stop_loss.price} · Perte théorique au SL hors frais/glissement : "
                + colored_pnl(-abs(plan.loss_max_estimated), f"{-abs(plan.loss_max_estimated):.4f} {plan.quote_asset}"))
    st.caption("Simulation valable 120 secondes. Le worker recontrôle prix, solde, risque et frais avant tout achat. Une simulation valide peut encore être refusée.")
    st.markdown("**Risque (même calcul que le routage automatique)**")
    if live_metrics:
        st.dataframe([{
            "Risque au stop frais compris (%)": round(live_metrics["risk_pct_with_costs"], 3),
            "Risque total projeté, entrées au repos comprises (%)": round(live_metrics["projected_total_risk_pct"], 3),
            "Exposition de ce trade (%)": round(live_metrics["exposure_pct"], 2),
            "Distance du stop (%)": round(live_metrics["stop_distance_pct"], 2),
            "Entrée déjà dépassée (%)": round(live_metrics["entry_gap_pct"], 2),
        }], hide_index=True)
    hard_refusals = [reason for reason in live_reasons if reason.code == "R2_HARD_LIMIT"]
    for reason in hard_refusals:
        st.error(reason.message)
    # Motifs à acquitter : ceux du routage enregistré et ceux de la simulation, un par catégorie.
    to_acknowledge = {reason.code: reason for reason in (stored_route.reasons if stored_route else [])}
    to_acknowledge |= {reason.code: reason for reason in live_reasons}
    csi_status = to_acknowledge.pop("C_CSI_STATUS", None)
    if parsed.is_csi and parsed.validation_status != "DEMO_ELIGIBLE" and csi_status is None:
        csi_status = signal_routing.Reason("C_CSI_STATUS", signal_routing.CONFIANCE,
                                           f"Statut CSI {parsed.validation_status}")
    acknowledged = []
    all_acknowledged = True
    if csi_status is not None:
        ticked = st.checkbox(f"Je confirme ce signal CSI non DEMO_ELIGIBLE (statut {parsed.validation_status}) : "
                             "jamais exécuté automatiquement, décision manuelle", key=f"ack_csi_{selected}")
        all_acknowledged &= ticked
        acknowledged += ["C_CSI_STATUS"] if ticked else []
    labels = {signal_routing.CONFIANCE: "une confiance faible ou inconnue", signal_routing.RISQUE: "un risque élevé",
              signal_routing.DONNEES: "des données incomplètes", signal_routing.CONTRAT: "un motif de contrat"}
    for category, reasons in signal_routing.grouped_reasons(to_acknowledge.values()).items():
        ticked = st.checkbox(f"Je confirme malgré {labels.get(category, category)} : "
                             + " · ".join(reason.message for reason in reasons), key=f"ack_{category}_{selected}")
        all_acknowledged &= ticked
        acknowledged += [reason.code for reason in reasons] if ticked else []
    # Une case par simulation : le position_id d'un signal est désormais stable, une nouvelle
    # simulation ne doit jamais hériter de la confirmation de la précédente.
    confirm = st.checkbox("Je confirme ces achats LIMIT et cette stratégie sur Binance Demo",
                          key=f"confirm_{payload['position']['position_id']}_{payload['signal_confirmation_expires_at']}")
    if st.button("Transmettre au worker Demo", disabled=not (confirm and all_acknowledged) or bool(hard_refusals)):
        try:
            if time.time() >= payload["signal_confirmation_expires_at"]:
                raise ValueError("Simulation expirée : relancer la vérification.")
            if "signal_v1" not in service.runtime().command_capabilities:
                raise ValueError("Redémarrer le worker avant de confirmer un signal (nouveaux garde-fous).")
            if "independent_positions_v1" not in service.runtime().command_capabilities:
                raise ValueError("Redémarrer le worker pour activer les positions indépendantes.")
            if (payload["position"]["stop_loss"].get("trigger") == "CANDLE_CLOSE"
                    and "candle_stop_v1" not in service.runtime().command_capabilities):
                raise ValueError("Redémarrer le worker pour activer le SL à la clôture de bougie.")
            status = service.worker_status()
            if not status.running or status.heartbeat_age is None or status.heartbeat_age >= 20:
                raise ValueError("Démarrer le worker avant de confirmer un signal.")
            confirmed = dict(payload) | {"confirmation_mode": "MANUAL",
                                         "acknowledged_reason_codes": sorted(acknowledged),
                                         "budget_proposed": budget_proposed}
            if parsed.is_csi:
                confirmed["signal_validation_status"] = parsed.validation_status
            frozen = inbox.freeze(scope, selected, confirmed)
            command = service.submit_command("SUBMIT_POSITION", frozen, request_key=f"signal:{selected}")
            service.events.append(
                EventType.SIGNAL_MANUAL_CONFIRMED,
                f"Signal confirmé à la main : {parsed.symbol}",
                symbol=parsed.symbol, signal_id=selected, command_id=command["id"],
                acknowledged_reason_codes=sorted(acknowledged), budget=budget, budget_proposed=budget_proposed,
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Commande non confirmée : {exc}. Consulter Opérations avant toute autre action.")
