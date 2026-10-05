"""Settings — réglages, presets, sécurité, notifications, diagnostic."""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.binance_client import BinanceSpotClient  # noqa: E402
from binance_spot_manager.config import (  # noqa: E402
    ALLOWED_DEMO_BASE_URLS,
    reload_settings,
)
from binance_spot_manager.notification_engine import channel_summary  # noqa: E402
from binance_spot_manager.fee_token import AccountCommission, FeeTokenPolicy, assess_bnb_fees  # noqa: E402
from binance_spot_manager.command_store import account_scope  # noqa: E402
from binance_spot_manager.browser_notifications import (  # noqa: E402
    PERMISSION_HTML, browser_alert_preferences, notification_html,
)
from ui_common import (  # noqa: E402
    alert_tone,
    banner,
    fmt_price,
    get_service,
    page_header,
    sidebar_status,
)

from ui_common import require_login  # noqa: E402

# Connexion exigee avant tout affichage (comptes : scripts/creer_compte.py).
require_login()

settings = st.session_state.setdefault("settings_obj", None) or reload_settings()
service = get_service()

st.set_page_config(page_title="Settings — BinanceSpotManager", page_icon="⚙️", layout="wide")
page_header("Settings", "Configuration, presets et diagnostic")
banner(settings)
sidebar_status(settings)

tabs = st.tabs(["Sécurité", "Worker & risque", "Presets", "Notifications", "Diagnostic", "Signaux"])

with tabs[5]:
    from binance_spot_manager.position_store import get_settings_store
    from binance_spot_manager.signal_plan import (
        TRAIL_STOP_KEY,
        automatic_entry_allocations,
        automatic_tp_allocations,
    )
    from binance_spot_manager.signal_sizing import SignalSizingPolicy
    from binance_spot_manager.telegram_signals import chat_allowlist
    from binance_spot_manager import csi_client
    from binance_spot_manager.csi_client import (
        GATE_ENABLED_KEY,
        GATE_HOLD_INDETERMINE_KEY,
        GATE_WHEN_UNAVAILABLE_KEY,
        SOURCE_NAMES_KEY,
        GatePolicy,
        source_names,
    )

    CSI_HOLD_LABEL = "Retenir le signal (prudent)"
    CSI_ALLOW_LABEL = "Exécuter quand même, sans avis"

    st.subheader("Réception des signaux Telegram")
    st.caption("Le worker peut relever automatiquement les messages autorisés. L'exécution directe se règle séparément plus bas. Le token existant n'est ni affiché ni modifié.")
    signal_preferences = get_settings_store().load()
    with st.form("telegram_signal_preferences"):
        signal_enabled = st.toggle("Autoriser l'import Telegram", value=bool(signal_preferences.get("signal_telegram_enabled", False)))
        signal_auto_enabled = st.toggle(
            "Relever automatiquement avec le worker",
            value=bool(signal_preferences.get("signal_telegram_auto_enabled", False)),
            help="Long polling de 20 s dans un thread séparé. Les messages arrivent immédiatement sans ralentir les TP/SL.",
        )
        signal_chats = st.text_input("Conversations autorisées (identifiants numériques séparés par des virgules)", value=signal_preferences.get("signal_telegram_chats", ""))
        st.caption("Exemple : -1001234567890, 123456789. Un seul lecteur getUpdates est autorisé ; un webhook actif empêche cette réception.")
        if st.form_submit_button("Enregistrer la réception des signaux"):
            try:
                if signal_auto_enabled and not signal_enabled:
                    raise ValueError("Activer d'abord l'import Telegram pour utiliser la relève automatique.")
                if signal_enabled:
                    chat_allowlist(signal_chats)
                    if not settings.telegram_bot_token:
                        raise ValueError("Token Telegram absent de la configuration actuelle.")
                get_settings_store().update({
                    "signal_telegram_enabled": signal_enabled,
                    "signal_telegram_auto_enabled": signal_auto_enabled,
                    "signal_telegram_chats": signal_chats.strip(),
                })
                st.success("Réglages enregistrés. Le worker les relit automatiquement.")
            except ValueError as exc:
                st.error(str(exc))

    telegram_diagnostics = getattr(service.runtime(), "telegram_diagnostics", {})
    if telegram_diagnostics:
        with st.container(border=True):
            state = telegram_diagnostics.get("state", "INCONNU")
            st.write(f"**État du lecteur** : `{state}`")
            st.caption(
                f"Messages importés depuis le démarrage : {telegram_diagnostics.get('received_total', 0)} · "
                f"Dernier lot : {telegram_diagnostics.get('last_batch_count', 0)} · "
                f"Échecs consécutifs : {telegram_diagnostics.get('failures', 0)}"
            )
            if telegram_diagnostics.get("last_error"):
                st.error(telegram_diagnostics["last_error"])

    st.divider()
    st.subheader("Dépôt direct (générateur ML)")
    st.caption(
        "Le worker relit `data/signal_drop/incoming/` à chaque cycle. Un fichier JSON (v1) "
        "passe par le même parseur strict que Telegram ; un fichier TXT `SIGNAL_VERSION=3` "
        "(CryptoSignalIntelligence) est contrôlé par le contrat CSI V3 (expirations, écart "
        "d'entrée, politique de sortie, idempotence). La réception seule ne crée aucun ordre."
    )
    with st.form("signal_drop_preferences"):
        drop_enabled = st.toggle(
            "Importer les signaux déposés",
            value=bool(signal_preferences.get("signal_drop_enabled", False)),
            key="signal_drop_enabled_toggle",
        )
        if st.form_submit_button("Enregistrer le dépôt direct"):
            update = {"signal_drop_enabled": bool(drop_enabled)}
            if not drop_enabled:
                # Fail closed : désactiver l'import retire aussi l'exécution automatique.
                update |= {"signal_drop_auto_enabled": False, "signal_drop_auto_enabled_since": 0.0}
            get_settings_store().update(update)
            signal_preferences = get_settings_store().load()
            st.success("Réglage enregistré. Le worker le relit automatiquement.")
    drop_diagnostics = telegram_diagnostics.get("drop", {}) if telegram_diagnostics else {}
    if drop_diagnostics:
        with st.container(border=True):
            st.write(f"**État du dépôt** : `{drop_diagnostics.get('state', 'INCONNU')}`")
            st.caption(
                f"Signaux importés depuis le démarrage : {drop_diagnostics.get('imported_total', 0)} · "
                f"Fichiers rejetés : {drop_diagnostics.get('rejected_total', 0)}"
            )
            if drop_diagnostics.get("last_error"):
                st.error(drop_diagnostics["last_error"])

    st.divider()
    st.subheader("SL après TP (signaux)")
    from binance_spot_manager.signal_plan import SIGNAL_SL_AFTER_TP_RULES, signal_sl_after_tp

    sl_rule_labels = {
        "NO_CHANGE": "Aucun changement",
        "BREAK_EVEN": "Prix moyen d'entrée (break-even)",
        "BREAK_EVEN_WITH_FEES": "Break-even frais inclus",
        "PREVIOUS_TP": "TP précédent",
    }
    with st.form("signal_sl_after_tp_preferences"):
        sl_rule_choice = st.selectbox(
            "Déplacer le SL après chaque TP (sauf le dernier)",
            [rule.value for rule in SIGNAL_SL_AFTER_TP_RULES],
            index=[rule.value for rule in SIGNAL_SL_AFTER_TP_RULES].index(
                signal_sl_after_tp(signal_preferences.get("signal_sl_after_tp")).value
            ),
            format_func=sl_rule_labels.get,
            key="signal_sl_after_tp_select",
            help="S'applique aux prochains signaux, manuels ou automatiques. Les positions existantes ne changent pas.",
        )
        if st.form_submit_button("Enregistrer la règle SL des signaux"):
            get_settings_store().update({"signal_sl_after_tp": sl_rule_choice})
            st.success("Règle enregistrée pour les prochains signaux.")

    st.divider()
    st.subheader("Budget des signaux")
    st.caption(
        "Ce réglage propose le budget lors de la simulation et fixe celui de l'exécution "
        "automatique. Un signal avec un motif de revue (risque élevé, confiance faible ou "
        "inconnue) reste toujours à confirmer à la main."
    )
    sizing = SignalSizingPolicy.from_mapping(signal_preferences)
    mode_labels = {
        "FIXED": "Montant fixe",
        "PERCENT": "% du portefeuille",
        "ADAPTIVE": "Adaptatif",
    }
    sizing_mode = st.segmented_control(
        "Méthode de calcul",
        list(mode_labels),
        default=sizing.mode,
        required=True,
        format_func=mode_labels.get,
        key="signal_sizing_mode_choice",
        width="stretch",
    )
    with st.form("signal_sizing_preferences"):
        fixed_budget = st.number_input(
            "Montant fixe par signal (devise de la paire)",
            min_value=0.01,
            max_value=1_000_000_000.0,
            value=float(sizing.fixed_budget),
            step=10.0,
            key="signal_fixed_budget_input",
            disabled=sizing_mode != "FIXED",
        )
        capital_percent = st.number_input(
            "Part normale du portefeuille (%)",
            min_value=0.01,
            max_value=100.0,
            value=float(sizing.capital_percent),
            step=0.5,
            disabled=sizing_mode == "FIXED",
        )
        low_threshold = st.number_input(
            "Activer la part réduite si le capital libre passe sous (%)",
            min_value=0.01,
            max_value=100.0,
            value=float(sizing.low_balance_threshold_percent),
            step=1.0,
            disabled=sizing_mode != "ADAPTIVE",
            help="Capital libre de la devise de la paire ÷ valeur totale du portefeuille.",
        )
        low_percent = st.number_input(
            "Part réduite du portefeuille (%)",
            min_value=0.01,
            max_value=100.0,
            value=float(sizing.low_balance_budget_percent),
            step=0.5,
            disabled=sizing_mode != "ADAPTIVE",
        )
        st.caption(
            "Exemple adaptatif : 5 % normalement ; si le libre passe sous 30 % du "
            "portefeuille, le budget descend à 2 %. La réserve de capital reste prioritaire."
        )
        if st.form_submit_button("Enregistrer le budget des signaux", type="primary"):
            if sizing_mode == "ADAPTIVE" and low_percent > capital_percent:
                st.error("La part réduite doit être inférieure ou égale à la part normale.")
            else:
                get_settings_store().update({
                    "signal_sizing_mode": sizing_mode,
                    "signal_fixed_budget": float(fixed_budget),
                    "signal_capital_percent": float(capital_percent),
                    "signal_low_balance_threshold_percent": float(low_threshold),
                    "signal_low_balance_budget_percent": float(low_percent),
                })
                st.success("Stratégie de budget enregistrée pour les prochains signaux.")

    st.divider()
    st.subheader("Suivi du stop loss")
    with st.form("signal_trail_stop_preferences"):
        trail_stop = st.toggle(
            "Remonter le SL au fil des TP",
            value=bool(signal_preferences.get(TRAIL_STOP_KEY, True)),
            key="signal_trail_stop_toggle",
            help=(
                "Activé (par défaut) : après TP1, le SL passe à l'Entry 1 ; à partir de TP3, il suit "
                "deux objectifs en arrière (TP3 → TP1, TP4 → TP2…) ; un SL de clôture de bougie déplacé "
                "devient un stop au prix sur Binance. Désactivé : le SL reste exactement "
                "là où le signal l'a placé pendant tout le trade, au même prix, et un SL de clôture de "
                "bougie n'est jamais converti en stop instantané."
            ),
        )
        st.caption(
            "S'applique aux nouveaux trades issus de signaux, manuels ou automatiques. "
            "Les trades ouverts gardent le comportement avec lequel ils ont démarré."
        )
        if st.form_submit_button("Enregistrer le suivi du SL"):
            get_settings_store().update({TRAIL_STOP_KEY: bool(trail_stop)})
            st.success("Suivi du SL enregistré pour les prochains signaux.")

    st.divider()
    st.subheader("Achat quand le TP1 est touché avant")
    with st.form("signal_tp1_first_preferences"):
        cancel_tp1_first = st.toggle(
            "Annuler l'ordre d'achat si le prix touche le TP1 avant qu'il soit exécuté",
            value=bool(signal_preferences.get("signal_cancel_entry_if_tp1_first", False)),
            key="signal_cancel_entry_if_tp1_first_toggle",
            help=(
                "Désactivé (par défaut) : l'ordre d'achat reste ouvert et peut être exécuté plus tard, après "
                "un repli ; ces trades sont marqués et comparés aux autres dans History (« Achats exécutés "
                "après un TP1 déjà touché »). Activé : le signal est considéré comme parti sans toi et l'achat "
                "est annulé. Les signaux CSI annulent toujours (contrat)."
            ),
        )
        if st.form_submit_button("Enregistrer le réglage d'achat"):
            get_settings_store().update({"signal_cancel_entry_if_tp1_first": bool(cancel_tp1_first)})
            st.success("Réglage enregistré pour les prochains signaux texte.")

    st.divider()
    st.subheader("Canal perdant")
    with st.form("signal_channel_review_preferences"):
        channel_review = st.toggle(
            "Mettre « À confirmer » les signaux d'un canal dont le résultat net est négatif",
            value=bool(signal_preferences.get("signal_channel_review_enabled", False)),
            key="signal_channel_review_toggle",
            help=("Le résultat net (frais compris) du canal d'origine est calculé sur ses positions terminées. "
                  "Sous le nombre minimal de positions, aucun jugement : trop peu de trades."),
        )
        channel_min = st.number_input(
            "Nombre minimal de positions terminées du canal avant de juger",
            min_value=10, max_value=500, step=5,
            value=int(signal_preferences.get("signal_channel_review_min_trades", 30)),
            key="signal_channel_review_min_input",
        )
        if st.form_submit_button("Enregistrer la règle du canal"):
            get_settings_store().update({"signal_channel_review_enabled": bool(channel_review),
                                         "signal_channel_review_min_trades": int(channel_min)})
            st.success("Réglage enregistré.")

    st.divider()
    st.subheader("Exécution automatique")
    auto_was_enabled = bool(signal_preferences.get("signal_auto_execute_enabled", False))
    auto_execute = st.toggle(
        "Envoyer automatiquement au worker les signaux sans motif de revue (Telegram, dépôt)",
        value=auto_was_enabled,
        key="signal_auto_execute_toggle",
        help=(
            "Chaque nouveau signal autorisé peut créer immédiatement ses ordres LIMIT "
            "sur Binance Demo avec le budget défini ci-dessus."
        ),
    )
    auto_touch_stop = st.toggle(
        "Interpréter les SL temporisés (15min, 1h, 4h…) comme des stops au toucher",
        value=bool(signal_preferences.get("signal_auto_touch_stop", False)),
        disabled=not auto_execute,
        key="signal_auto_touch_stop_toggle",
        help=(
            "Désactivé (par défaut) : « Stop: 0.93 (4h) » attend qu'une bougie 4h clôture à 0.93 ou dessous, "
            "surveillée par le worker, puis vend au marché. Activé : la protection part dès que le prix "
            "touche 0.93 (ordre stop sur Binance), sans attendre la clôture."
        ),
    )
    auto_entry_count = st.number_input(
        "Nombre maximal d'entrées repris du signal",
        min_value=1,
        max_value=20,
        value=int(signal_preferences.get("signal_auto_entry_count", 1)),
        disabled=not auto_execute,
        key="signal_auto_entry_count_input",
        help="Les premières entrées sont conservées. Avec 1, Entry2 et les suivantes sont ignorées.",
    )
    entry_distribution_labels = {
        "EQUAL": "Répartition égale",
        "CUSTOM": "Personnalisée",
    }
    saved_entry_distribution = str(
        signal_preferences.get("signal_auto_entry_distribution", "EQUAL")
    )
    if saved_entry_distribution not in entry_distribution_labels:
        saved_entry_distribution = "EQUAL"
    auto_entry_distribution = st.segmented_control(
        "Répartition du budget entre les entrées retenues",
        list(entry_distribution_labels),
        default=saved_entry_distribution,
        required=True,
        format_func=entry_distribution_labels.get,
        key="signal_auto_entry_distribution_choice",
        disabled=not auto_execute,
        width="stretch",
    )
    auto_entry_custom = st.text_input(
        "Pourcentages personnalisés des entrées",
        value=str(signal_preferences.get("signal_auto_entry_custom_percentages", "100")),
        key="signal_auto_entry_custom_input",
        disabled=not auto_execute or auto_entry_distribution != "CUSTOM",
        help="Une valeur par entrée retenue, séparée par ; ou /. Exemple : 30;70.",
    )
    auto_tp_count = st.number_input(
        "Nombre maximal de TP repris du signal",
        min_value=1,
        max_value=20,
        value=int(signal_preferences.get("signal_auto_tp_count", 2)),
        disabled=not auto_execute,
        key="signal_auto_tp_count_input",
        help="Les premiers objectifs sont conservés afin de viser une sortie plus proche.",
    )
    distribution_labels = {
        "EARLY": "Sécuriser tôt",
        "EQUAL": "Répartition égale",
        "CUSTOM": "Personnalisée",
    }
    saved_tp_distribution = str(
        signal_preferences.get("signal_auto_tp_distribution", "EARLY")
    )
    if saved_tp_distribution not in distribution_labels:
        saved_tp_distribution = "EARLY"
    auto_tp_distribution = st.segmented_control(
        "Répartition des ventes entre les TP retenus",
        list(distribution_labels),
        default=saved_tp_distribution,
        required=True,
        format_func=distribution_labels.get,
        key="signal_auto_tp_distribution_choice",
        disabled=not auto_execute,
        width="stretch",
    )
    if auto_tp_distribution == "EARLY":
        st.caption("Par défaut : 1 TP = 100 % · 2 TP = 70/30 · 3 TP = 50/30/20. Au-delà, la répartition reste dégressive.")
    elif auto_tp_distribution == "EQUAL":
        st.caption("Chaque TP reçoit la même part de la position initiale.")
    auto_tp_custom = st.text_input(
        "Pourcentages personnalisés des TP",
        value=str(signal_preferences.get("signal_auto_tp_custom_percentages", "70;30")),
        key="signal_auto_tp_custom_input",
        disabled=not auto_execute or auto_tp_distribution != "CUSTOM",
        help="Une valeur par TP retenu, séparée par ; ou /. Exemple : 70;30.",
    )
    auto_max_age = st.number_input(
        "Âge maximal d'un message automatique (minutes)",
        min_value=1,
        max_value=60,
        value=int(signal_preferences.get("signal_auto_max_age_minutes", 5)),
        disabled=not auto_execute,
        key="signal_auto_max_age_input",
        help="Les anciens messages et les anciens transferts sont conservés sans ordre.",
    )
    if auto_execute:
        st.warning(
            "Après activation, un nouveau signal reconnu provenant d'un chat autorisé "
            "sera simulé puis envoyé au worker sans confirmation sur la page Signaux. "
            "Le worker contrôle encore le prix, le solde, les frais et le risque."
        )
    authorization = True
    if auto_execute and not auto_was_enabled:
        authorization = st.checkbox(
            "J'autorise l'envoi automatique d'ordres sur Binance Demo",
            key="signal_auto_execute_authorization",
        )
    drop_auto_was_enabled = bool(signal_preferences.get("signal_drop_auto_enabled", False))
    drop_auto = st.toggle(
        "Exécuter aussi les signaux ML/dépôt direct",
        value=drop_auto_was_enabled,
        disabled=not auto_execute,
        key="signal_drop_auto_toggle",
        help="Autorisation séparée : sans elle, les fichiers déposés restent en revue manuelle.",
    )
    drop_authorization = True
    if auto_execute and drop_auto and not drop_auto_was_enabled:
        drop_authorization = st.checkbox(
            "J'autorise les signaux ML/dépôt à envoyer des ordres sur Binance Demo",
            key="signal_drop_auto_authorization",
        )
    if st.button("Enregistrer l'exécution automatique", type="primary"):
        try:
            drop_auto = bool(drop_auto and auto_execute)
            if auto_execute and not authorization:
                raise ValueError("Cocher l'autorisation explicite avant l'activation.")
            if drop_auto and not drop_authorization:
                raise ValueError("Cocher l'autorisation explicite des signaux ML/dépôt.")
            if drop_auto and not signal_preferences.get("signal_drop_enabled", False):
                raise ValueError("Activer et enregistrer d'abord le dépôt direct plus haut.")
            if auto_execute and not drop_auto and not (
                signal_preferences.get("signal_telegram_enabled", False)
                and signal_preferences.get("signal_telegram_auto_enabled", False)
            ):
                raise ValueError(
                    "Activer et enregistrer d'abord l'import Telegram automatique plus haut."
                )
            enabled_since = (
                float(signal_preferences.get("signal_auto_execute_enabled_since") or time.time())
                if auto_execute and auto_was_enabled else time.time() if auto_execute else 0.0
            )
            drop_since = (
                float(signal_preferences.get("signal_drop_auto_enabled_since") or time.time())
                if drop_auto and drop_auto_was_enabled else time.time() if drop_auto else 0.0
            )
            entry_allocations = automatic_entry_allocations(
                int(auto_entry_count), auto_entry_distribution, auto_entry_custom,
            )
            tp_allocations = automatic_tp_allocations(
                int(auto_tp_count), auto_tp_distribution, auto_tp_custom,
            )
            get_settings_store().update({
                "signal_auto_execute_enabled": bool(auto_execute),
                "signal_auto_execute_enabled_since": enabled_since,
                "signal_auto_touch_stop": bool(auto_touch_stop),
                "signal_auto_max_age_minutes": int(auto_max_age),
                "signal_drop_auto_enabled": drop_auto,
                "signal_drop_auto_enabled_since": drop_since,
                "signal_auto_entry_count": int(auto_entry_count),
                "signal_auto_entry_distribution": auto_entry_distribution,
                "signal_auto_entry_custom_percentages": ";".join(
                    f"{value:g}" for value in entry_allocations
                ),
                "signal_auto_tp_count": int(auto_tp_count),
                "signal_auto_tp_distribution": auto_tp_distribution,
                "signal_auto_tp_custom_percentages": ";".join(
                    f"{value:g}" for value in tp_allocations
                ),
            })
            st.success(
                ("Exécution automatique activée pour les nouveaux messages Telegram"
                 + (" et signaux ML/dépôt." if drop_auto else "."))
                if auto_execute else "Exécution automatique désactivée."
            )
        except ValueError as exc:
            st.error(str(exc))

    auto_diagnostics = telegram_diagnostics.get("auto_execution", {})
    if auto_diagnostics:
        st.caption(
            f"État : {auto_diagnostics.get('state', 'INCONNU')} · "
            f"mis en file : {auto_diagnostics.get('queued_total', 0)} · "
            f"refusés : {auto_diagnostics.get('rejected_total', 0)} · "
            f"à confirmer : {auto_diagnostics.get('review_total', 0)}"
        )
        if auto_diagnostics.get("last_detail"):
            st.caption(f"Dernier résultat : {auto_diagnostics['last_detail']}")

    st.divider()
    st.subheader("Confirmation manuelle ou exécution automatique")
    from binance_spot_manager import signal_routing
    from binance_spot_manager.event_store import EventStore
    from binance_spot_manager.models import EventType

    st.caption(
        "Règle : confirmation manuelle obligatoire si le risque est élevé OU si la confiance est "
        "faible ou inconnue. Un signal part automatiquement sur Binance Demo seulement si "
        "l'exécution automatique est autorisée ci-dessus et qu'il n'a aucun motif de revue. "
        "La confiance est une déclaration (groupe de confiance, actif validé, statut CSI "
        "DEMO_ELIGIBLE), jamais une mesure ; le dépôt JSON v1 est toujours manuel."
    )
    routing_prefs = get_settings_store().load()
    routing_limits = service.risk_limits()
    current_policy = signal_routing.RoutingPolicy.from_mapping(routing_prefs, routing_limits)
    allowed_chats = signal_routing.parse_chat_ids(routing_prefs.get("signal_telegram_chats", ""))
    try:
        auto_24h = len([c for c in service.commands.auto_commands(account_scope(settings), since=time.time() - 86_400)
                        if c["state"] not in signal_routing.NO_ORDER_STATES])
    except Exception:  # noqa: BLE001 - affichage seulement
        auto_24h = None
    with st.container(border=True):
        st.markdown("**État de préparation**")
        manual_mode = settings.run_mode.value == "DEMO_MANUAL" and current_policy.honor_demo_manual
        st.markdown(
            f"- Mode : `{settings.run_mode.value}`"
            + (" — tout signal demande une confirmation manuelle" if manual_mode else "") + "\n"
            f"- Autorisation datée : {'oui' if routing_prefs.get('signal_auto_execute_enabled_since') else 'non'}\n"
            f"- Groupes Telegram de confiance : {len(current_policy.trusted_chats)}\n"
            f"- Actifs validés pour l'automatique : {len(current_policy.base_assets)}\n"
            f"- Coupe-circuits : {auto_24h if auto_24h is not None else '?'} ordre(s) automatique(s) sur 24 h "
            f"(maximum {current_policy.max_auto_per_24h})"
            + (f" · réarmé le {time.strftime('%Y-%m-%d %H:%M', time.gmtime(current_policy.breaker_reset_at))} UTC"
               if current_policy.breaker_reset_at else "")
        )
    if routing_prefs.get("signal_auto_execute_enabled") and (
            not current_policy.trusted_chats or not current_policy.base_assets):
        st.warning("Exécution automatique active, mais aucun groupe de confiance ou aucun actif validé : "
                   "tous les signaux Telegram passeront « À confirmer ».")

    trusted_input = st.text_input(
        "Groupes Telegram de confiance (identifiants numériques, parmi les conversations autorisées)",
        value=", ".join(str(chat) for chat in sorted(signal_routing.parse_chat_ids(
            routing_prefs.get("signal_auto_trusted_chats", ())))),
        key="signal_trusted_chats_input",
        help="Déclaration du propriétaire, pas une mesure : un groupe de confiance peut envoyer de mauvais signaux.",
    )
    assets_input = st.text_input(
        "Actifs validés pour l'exécution automatique (liste vide = aucun actif autorisé)",
        value=", ".join(current_policy.base_assets),
        key="signal_base_assets_input",
    )
    st.caption("Pré-rempli avec les 16 actifs de base de CryptoSignalIntelligence, à valider (univers halal). "
               "Un actif hors liste passe « À confirmer » avec un avertissement. Les signaux CSI ne sont pas "
               "contrôlés contre cette liste : leur univers est filtré en amont.")
    honor_input = st.toggle("DEMO_MANUAL : tout signal demande une confirmation manuelle",
                            value=current_policy.honor_demo_manual, key="signal_honor_demo_manual_toggle")
    notify_input = st.toggle("Notifier les signaux à confirmer (Telegram/e-mail, sans aucune ligne de prix)",
                             value=current_policy.notify_review, key="signal_review_notify_toggle")
    with st.expander("Seuils de revue (valeurs prudentes par défaut)"):
        max_risk_input = st.number_input("Risque au stop frais compris (% du portefeuille, plafonné à la limite dure)",
                                         min_value=0.01, max_value=100.0, value=float(current_policy.max_risk_percent),
                                         step=0.1, key="signal_review_max_risk_input")
        share_input = st.number_input("Part du risque total maximum (0,05 à 1)", min_value=0.05, max_value=1.0,
                                      value=float(current_policy.total_risk_share), step=0.05,
                                      key="signal_review_total_share_input")
        min_stop_input = st.number_input("Distance minimale du stop (%)", min_value=0.0, max_value=50.0,
                                         value=float(current_policy.min_stop_percent), step=0.1,
                                         key="signal_review_min_stop_input")
        max_stop_input = st.number_input("Distance maximale du stop (%)", min_value=0.1, max_value=100.0,
                                         value=float(current_policy.max_stop_percent), step=0.5,
                                         key="signal_review_max_stop_input")
        gap_input = st.number_input("Entrée déjà dépassée (%)", min_value=0.0, max_value=10.0,
                                    value=float(current_policy.marketable_gap_percent), step=0.1,
                                    key="signal_review_gap_input")
        same_asset_input = st.toggle("Revue si le même actif est déjà ouvert ou en file",
                                     value=current_policy.same_asset_review, key="signal_review_same_asset_toggle")
        volatility_input = st.toggle("Revue si VOLATILITY_REGIME=HIGH (CSI)",
                                     value=current_policy.csi_high_volatility_review,
                                     key="signal_review_volatility_toggle")
        max_auto_input = st.number_input("Ordres automatiques au plus sur 24 h", min_value=0, max_value=100,
                                         value=int(current_policy.max_auto_per_24h), step=1,
                                         key="signal_auto_max_per_24h_input")
        day_loss_input = st.number_input("Perte réalisée du jour déclenchant la revue (%)", min_value=0.01,
                                         max_value=100.0, value=float(current_policy.daily_loss_percent), step=0.5,
                                         key="signal_auto_daily_loss_input")
        streak_input = st.number_input("Pertes automatiques consécutives avant réarmement", min_value=1, max_value=50,
                                       value=int(current_policy.loss_streak), step=1,
                                       key="signal_auto_loss_streak_input")
    widen_authorization = st.checkbox("J'autorise l'élargissement de l'exécution automatique",
                                      key="signal_routing_widen_authorization",
                                      help="Obligatoire pour tout changement qui ne peut qu'augmenter l'automatique.")

    def save_routing(update: dict, message: str) -> None:
        new_policy = signal_routing.RoutingPolicy.from_mapping(routing_prefs | update, routing_limits)
        widened = current_policy.widened_by(new_policy)
        if widened and not widen_authorization:
            raise ValueError("Élargissement de l'automatique (" + ", ".join(widened)
                             + ") : cocher l'autorisation explicite.")
        get_settings_store().update(update)
        EventStore().append(EventType.SIGNAL_ROUTING_CHANGED, message, level="INFO",
                            changes=sorted(update), widened=widened)
        st.success("Routage enregistré. Le worker le relit à chaque cycle ; les signaux déjà décidés ne sont pas re-routés.")

    routing_buttons = st.columns(3)
    if routing_buttons[0].button("Enregistrer le routage des signaux", type="primary"):
        try:
            trusted_tokens = [token for token in trusted_input.replace(";", ",").split(",") if token.strip()]
            trusted = signal_routing.parse_chat_ids(trusted_input)
            if len(trusted) != len(trusted_tokens):
                raise ValueError("Groupes de confiance : identifiants numériques uniquement.")
            if not trusted <= allowed_chats:
                raise ValueError("Groupes de confiance hors des conversations autorisées à la réception : "
                                 + ", ".join(str(chat) for chat in sorted(trusted - allowed_chats)))
            asset_tokens = [token for token in assets_input.replace(";", ",").split(",") if token.strip()]
            assets = signal_routing.parse_assets(assets_input)
            if len(assets) != len({token.strip().upper() for token in asset_tokens}):
                raise ValueError("Actifs : codes en lettres et chiffres uniquement (ex. BTC, ETH).")
            if float(min_stop_input) >= float(max_stop_input):
                raise ValueError("La distance minimale du stop doit être inférieure à la maximale.")
            save_routing({
                "signal_auto_trusted_chats": sorted(trusted),
                "signal_auto_base_assets": list(assets),
                "signal_route_honor_demo_manual": bool(honor_input),
                "signal_review_notify": bool(notify_input),
                "signal_review_max_risk_percent": float(max_risk_input),
                "signal_review_total_risk_share": float(share_input),
                "signal_review_min_stop_percent": float(min_stop_input),
                "signal_review_max_stop_percent": float(max_stop_input),
                "signal_review_marketable_gap_percent": float(gap_input),
                "signal_review_same_asset": bool(same_asset_input),
                "signal_review_csi_high_volatility": bool(volatility_input),
                "signal_auto_max_per_24h": int(max_auto_input),
                "signal_auto_daily_loss_percent": float(day_loss_input),
                "signal_auto_loss_streak": int(streak_input),
            }, "Routage des signaux modifié")
        except ValueError as exc:
            st.error(str(exc))
    if routing_buttons[1].button("Rétablir les valeurs prudentes"):
        defaults = signal_routing.RoutingPolicy()
        try:
            save_routing({
                "signal_review_max_risk_percent": defaults.max_risk_percent,
                "signal_review_total_risk_share": defaults.total_risk_share,
                "signal_review_min_stop_percent": defaults.min_stop_percent,
                "signal_review_max_stop_percent": defaults.max_stop_percent,
                "signal_review_marketable_gap_percent": defaults.marketable_gap_percent,
                "signal_review_same_asset": True, "signal_review_csi_high_volatility": True,
                "signal_auto_max_per_24h": defaults.max_auto_per_24h,
                "signal_auto_daily_loss_percent": defaults.daily_loss_percent,
                "signal_auto_loss_streak": defaults.loss_streak,
                "signal_route_honor_demo_manual": True,
            }, "Seuils de revue des signaux rétablis")
        except ValueError as exc:
            st.error(str(exc))
    rearm_confirmation = st.checkbox("Je confirme le réarmement des coupe-circuits", key="signal_rearm_confirmation")
    if routing_buttons[2].button("Réarmer l'automatique", disabled=not rearm_confirmation):
        stamp = time.time()
        get_settings_store().update({"signal_auto_breaker_reset_at": stamp})
        EventStore().append(EventType.SIGNAL_ROUTING_CHANGED, "Coupe-circuits de l'automatique réarmés",
                            level="INFO", changes=["signal_auto_breaker_reset_at"])
        st.success("Coupe-circuits réarmés : seules les pertes automatiques suivantes comptent.")

    st.subheader("Avis CSI avant exécution automatique")
    st.caption(
        "CryptoSignalIntelligence évalue chaque signal Telegram (vetos, taux de base historique de la "
        "même géométrie, bilan du groupe). Il ne place aucun ordre : il peut seulement RETENIR un signal "
        "automatique pour confirmation manuelle, jamais l'envoyer tout seul. Détail : page CSI."
    )
    csi_policy = GatePolicy.from_mapping(signal_preferences)
    with st.form("signal_csi_gate_preferences"):
        csi_gate = st.toggle(
            "Demander l'avis de CSI avant toute exécution automatique",
            value=csi_policy.enabled,
            key="signal_csi_gate_toggle",
        )
        csi_hold_indetermine = st.toggle(
            "Retenir aussi les signaux jugés indéterminés",
            value=csi_policy.hold_indetermine,
            key="signal_csi_hold_indetermine_toggle",
            help="Refusé et Défavorable sont toujours retenus. Indéterminé : pas assez d'éléments pour préférer ce signal au hasard.",
        )
        csi_unavailable = st.radio(
            "Si CSI est injoignable",
            [CSI_HOLD_LABEL, CSI_ALLOW_LABEL],
            index=1 if csi_policy.allow_when_unavailable else 0,
            key="signal_csi_when_unavailable_choice",
        )
        csi_names = st.text_area(
            "Noms des groupes Telegram (identifiant=nom, un par ligne)",
            value=str(signal_preferences.get(SOURCE_NAMES_KEY, "")),
            key="signal_csi_source_names_input",
            height=90,
            help="Sert au bilan par groupe chez CSI. Exemple : -1001234567890=Suhaib. Sans nom : « telegram <identifiant> ».",
        )
        if st.form_submit_button("Enregistrer l'avis CSI"):
            try:
                source_names(csi_names)
                get_settings_store().update({
                    GATE_ENABLED_KEY: bool(csi_gate),
                    GATE_HOLD_INDETERMINE_KEY: bool(csi_hold_indetermine),
                    GATE_WHEN_UNAVAILABLE_KEY: "ALLOW" if csi_unavailable == CSI_ALLOW_LABEL else "HOLD",
                    SOURCE_NAMES_KEY: csi_names.strip(),
                })
                st.success("Réglage enregistré. Le worker le relit automatiquement.")
            except ValueError as exc:
                st.error(str(exc))
    csi_health, csi_failure = csi_client.CsiClient.from_env().probe()
    if csi_health is None:
        st.warning(
            f"CSI injoignable : {csi_failure}. Tant que CSI ne répond pas, les signaux automatiques sont "
            + ("exécutés sans avis (réglage)." if csi_policy.allow_when_unavailable
               else "retenus pour confirmation manuelle.")
        )
    else:
        (st.success if csi_health.get("ready") else st.info)(f"CSI : {csi_health.get('detail', '')}")

# ==========================================================================
# Sécurité
# ==========================================================================

with tabs[0]:
    from binance_spot_manager import licence
    from binance_spot_manager.key_vault import KeyVault, VaultError

    st.subheader("Mes clés API Binance Demo")
    st.caption(
        "Saisies ici, dans TON instance : chiffrées sur place (AES-256-GCM) avec une clé maîtresse "
        "rangée hors des données. Personne d'autre ne les reçoit, et elles ne sont jamais réaffichées."
    )
    vault = KeyVault()
    key_status = vault.binance_status()
    if "bsm_keys_message" in st.session_state:
        st.success(st.session_state.pop("bsm_keys_message"))
    open_positions = service.summary()["open"]
    if settings.credentials_source == "env":
        st.info(
            "Les clés actuelles viennent des variables d'environnement (`.env`), prioritaires : "
            "le coffre n'est utilisé que si elles sont vides."
        )
    if settings.credentials_error:
        st.error(f"Coffre : {settings.credentials_error}")
    if key_status.defined:
        st.success(
            f"Clés enregistrées dans le coffre : clé API se terminant par `{key_status.hint.lstrip('…')}`, "
            f"mises à jour le {key_status.updated_at[:16].replace('T', ' ')} UTC."
        )
    else:
        st.caption("Aucune clé dans le coffre.")
    with st.expander("Avant de coller une clé : bonnes pratiques Binance", expanded=not key_status.defined):
        st.markdown(
            """
- Crée une clé **dédiée à ce bot**, jamais celle d'un autre outil.
- **Retraits désactivés**, toujours : le bot n'en fait aucun.
- **Restriction par adresse IP** : limite la clé à l'IP du serveur qui fait tourner le bot.
- Seul le trading Spot est utile : pas de marge, de contrats à terme ni de transferts.
- Cette version ne fonctionne que sur **Binance Demo** : une clé du compte réel serait refusée.
- En cas de doute (fuite, ancien serveur), **supprime la clé chez Binance** : c'est la seule
  révocation immédiate. Puis enregistre une nouvelle clé ici.
"""
        )
    with st.form("bsm_api_keys", clear_on_submit=True):
        new_api_key = st.text_input("Clé API", type="password", autocomplete="off", key="bsm_new_api_key")
        new_api_secret = st.text_input("Secret API", type="password", autocomplete="off", key="bsm_new_api_secret")
        save_keys = st.form_submit_button("Chiffrer et enregistrer mes clés", type="primary")
    if save_keys:
        # Le secret ne reste pas dans l'état de session du serveur, quel que soit le résultat.
        st.session_state.pop("bsm_new_api_key", None)
        st.session_state.pop("bsm_new_api_secret", None)
        if open_positions:
            st.error(
                f"{open_positions} position(s) ouverte(s) : changer de clé maintenant couperait leur suivi "
                "(stops, objectifs). Clôturer d'abord, ou révoquer la clé chez Binance en cas d'urgence."
            )
        else:
            try:
                vault.save_binance_keys(new_api_key, new_api_secret)
            except (ValueError, VaultError) as exc:
                st.error(str(exc))
            else:
                st.session_state.pop("settings_obj", None)
                reload_settings()
                st.cache_resource.clear()
                st.session_state["bsm_keys_message"] = (
                    "Clés chiffrées et enregistrées. Redémarre le worker pour qu'il les utilise "
                    "(Docker : `make worker-restart`)."
                )
                del new_api_key, new_api_secret
                st.rerun()
    if key_status.defined:
        confirm_delete = st.checkbox("Je veux supprimer mes clés de ce serveur", key="confirm_delete_keys")
        if st.button("Supprimer mes clés", disabled=not confirm_delete):
            if open_positions:
                st.error(
                    f"{open_positions} position(s) ouverte(s) : sans clé, le worker ne pourrait plus les "
                    "protéger. Clôturer d'abord, ou révoquer la clé chez Binance en cas d'urgence."
                )
            else:
                vault.delete_binance_keys()
                st.session_state.pop("settings_obj", None)
                st.session_state.pop("confirm_delete_keys", None)
                reload_settings()
                st.cache_resource.clear()
                st.session_state["bsm_keys_message"] = (
                    "Clés supprimées du coffre. Pense aussi à supprimer la clé chez Binance."
                )
                st.rerun()

    st.subheader("Licence")
    licence_status = licence.current_status()
    if not licence.licence_required():
        st.caption("Licence non exigée sur cette installation (`BSM_LICENCE_REQUIRED`).")
    if licence_status.valid:
        st.success(
            f"Licence {licence_status.offre} — {licence_status.client} — valable jusqu'au "
            f"{licence_status.fin} inclus ({licence_status.days_left} jour(s) restants)."
        )
    elif licence.licence_required():
        st.error(
            f"{licence_status.reason}. Aucune nouvelle entrée n'est acceptée ; les positions ouvertes "
            "restent suivies (stops, objectifs, clôtures)."
        )
    else:
        st.caption(licence_status.reason)
    licence_upload = st.file_uploader("Installer un fichier de licence (.json)", type=["json"], key="licence_upload")
    if licence_upload is not None and st.button("Vérifier et installer la licence"):
        installed = licence.install_licence(licence_upload.getvalue())
        if installed.valid:
            st.success(f"Licence installée : valable jusqu'au {installed.fin}.")
        else:
            st.error(f"Licence refusée : {installed.reason}")

    st.subheader("Périmètre d'exécution")

    st.markdown(
        f"""
**Environnement** : `{settings.environment.value}`

**Mode opérationnel** : `{settings.run_mode.value}`

**URL de base** : `{settings.base_url}`

**URL autorisée** : {"✅ oui" if settings.base_url in ALLOWED_DEMO_BASE_URLS else "❌ NON"}

**Clés API** : {"configurées" if settings.has_credentials else "absentes"}{" (coffre chiffré)" if settings.credentials_source == "vault" else " (environnement)" if settings.credentials_source == "env" else ""}
"""
    )

    st.info(
        "Les URLs autorisées pour toute écriture sont uniquement : "
        + " et ".join(f"`{url}`" for url in sorted(ALLOWED_DEMO_BASE_URLS))
        + ". Cette liste est appliquée dans le code, avant chaque envoi d'ordre."
    )

    st.subheader("Mode Live")
    st.error(
        "🚫 Le mode **LIVE n'est pas implémenté dans cette version**. "
        "Le code refuse activement toute écriture hors Binance Demo, même si "
        "`.env` est modifié : `BSM_RUN_MODE=LIVE` est ramené à DRY_RUN au chargement, "
        "et une URL non whitelistée lève `SECURITE : operation interdite hors Binance Demo`."
    )

    st.markdown("**Préparé pour plus tard (non actif)**")
    st.markdown(
        """
- Configuration séparée `DEMO` / `LIVE` dans `.env`
- Clés API distinctes, jamais réutilisées
- URL distincte (`BSM_LIVE_BASE_URL`)
- Activation nécessitant une confirmation renforcée
- Journalisation séparée par environnement
- Interdiction absolue de bascule automatique
"""
    )

    st.subheader("Modifier le mode opérationnel")
    st.caption(
        "Le mode se change dans `.env` (`BSM_RUN_MODE`), pas depuis cette page : "
        "c'est une décision volontaire pour éviter un basculement accidentel."
    )
    if st.button("Recharger la configuration (.env)"):
        reload_settings()
        st.cache_resource.clear()
        st.success("Configuration rechargée.")
        st.rerun()

# ==========================================================================
# Worker & risque
# ==========================================================================

with tabs[1]:
    st.subheader("Worker")
    saved = service.user_settings()

    interval = st.number_input(
        "Intervalle de boucle (s)",
        min_value=1,
        max_value=300,
        value=int(saved.get("worker_interval", settings.worker_interval)),
    )
    heartbeat_warn = st.number_input(
        "Alerte si heartbeat plus vieux que (s)",
        min_value=5,
        max_value=600,
        value=int(saved.get("heartbeat_stale_after", settings.heartbeat_stale_after)),
    )
    exit_on_crossed_stop = st.toggle(
        "Vendre au marché si le stop est déjà franchi",
        value=bool(saved.get("exit_on_crossed_stop", True)),
        help=(
            "Quand Binance refuse le SL parce que le prix est déjà sous le stop, le worker "
            "relit le prix puis vend la quantité de cette position au marché. Désactivé : "
            "la position est mise en pause, sans protection, avec une alerte."
        ),
    )

    st.subheader("Risque portefeuille")
    max_risk = st.number_input(
        "Risque max par position (%)",
        min_value=0.1,
        max_value=100.0,
        value=float(saved.get("max_risk_per_position_percent", settings.max_risk_per_position_percent)),
        step=0.1,
    )
    max_total_risk = st.number_input(
        "Risque total max (%)",
        min_value=0.1,
        max_value=100.0,
        value=float(saved.get("max_total_risk_percent", settings.max_total_risk_percent)),
        step=0.1,
    )
    max_positions = st.number_input(
        "Nombre max de positions ouvertes",
        min_value=1,
        max_value=50,
        value=int(saved.get("max_open_positions", settings.max_open_positions)),
    )
    max_exposure = st.number_input(
        "Exposition max par paire (%)",
        min_value=1.0,
        max_value=100.0,
        value=float(saved.get("max_exposure_per_symbol_percent", settings.max_exposure_per_symbol_percent)),
        step=1.0,
    )
    reserve = st.number_input(
        "Réserve de capital par défaut (%)",
        min_value=0,
        max_value=90,
        value=int(saved.get("capital_reserve_percent", settings.capital_reserve_percent)),
    )

    st.subheader("BNB disponible pour les frais")
    st.caption("Ton solde BNB libre, valorisé en USDT au cours Binance. Les frais sont payés avec ce BNB.")
    bnb_monitor = st.toggle(
        "M'alerter quand la réserve de frais est faible",
        value=bool(saved.get("bnb_fee_monitor_enabled", True)),
        help="Ce bouton règle le bot uniquement ; il ne modifie pas l'option correspondante sur Binance.",
    )
    bnb_threshold = st.number_input(
        "M'alerter si mon BNB disponible vaut moins de (USDT)",
        min_value=0.0,
        max_value=1_000_000.0,
        value=FeeTokenPolicy.from_mapping(saved).alert_threshold_usdt,
        step=1.0,
        format="%.2f",
        disabled=not bnb_monitor,
        help="Exemple : 5 signifie une alerte quand ton BNB disponible vaut moins de 5 USDT.",
    )
    @st.fragment(run_every="60s")
    def fee_reserve_panel():
        st.button("Actualiser la réserve", key="refresh_fee_reserve")
        try:
            check = assess_bnb_fees(
                service.client.get_balances(), {"BNBUSDT": service.client.get_price("BNBUSDT")},
                FeeTokenPolicy(alert_threshold_usdt=float(bnb_threshold)),
            )
            if not check.conversion_available:
                st.warning(check.reason)
                return
            st.metric("Valeur du BNB disponible pour les frais", f"{check.free_usdt:,.2f} USDT")
            st.caption(f"Valeur du BNB libre uniquement · {check.locked_usdt:,.2f} USDT de BNB bloqué exclus · actualisé chaque minute.")
            if bnb_monitor and check.low:
                st.warning(f"Réserve sous ton seuil de {bnb_threshold:.2f} USDT.")
        except Exception:
            st.warning("Réserve indisponible : impossible de lire le solde ou le cours Binance pour le moment.")

    fee_reserve_panel()
    bnb_block_buys = st.toggle(
        "Bloquer les achats si la réserve est insuffisante",
        value=bool(saved.get("bnb_fee_block_new_buys", True)),
        disabled=not bnb_monitor,
        help="Vérifie la réserve contre le seuil et les frais estimés de l'achat. Les ventes TP/SL restent autorisées.",
    )
    st.caption(
        "Les taux sont récupérés automatiquement depuis Binance Demo avant chaque achat, "
        "avec la réduction BNB active sur le compte et la paire."
    )
    fee_symbol = st.text_input("Paire pour consulter les frais", "BTCUSDT").strip().upper()
    if st.button("Lire les frais depuis Binance Demo", disabled=not fee_symbol):
        try:
            commission = AccountCommission.from_response(
                service.client.get_commission_rates(fee_symbol), fee_symbol,
            )
            st.session_state["settings_fee_snapshot"] = (
                fee_symbol, account_scope(settings), commission,
            )
        except Exception as exc:
            st.session_state.pop("settings_fee_snapshot", None)
            st.error(f"Lecture des commissions impossible : {exc}")
    fee_snapshot = st.session_state.get("settings_fee_snapshot")
    if fee_snapshot and fee_snapshot[0] == fee_symbol and fee_snapshot[1] == account_scope(settings):
        commission = fee_snapshot[2]
        st.table([
            {
                "Sens": "Achat" if side == "BUY" else "Vente",
                "Exécution": kind.capitalize(),
                "Sans BNB": f"{commission.percent(side, kind, pay_in_bnb=False):.5f} %",
                "Avec BNB disponible": f"{commission.percent(side, kind, pay_in_bnb=True):.5f} %",
            }
            for side in ("BUY", "SELL") for kind in ("maker", "taker")
        ])
        (st.success if commission.bnb_enabled else st.info)(
            "Paiement des frais en BNB activé pour ce compte et cette paire."
            if commission.bnb_enabled else "Réduction BNB inactive pour ce compte ou cette paire."
        )
    with st.expander("Marge de sécurité avancée"):
        bnb_safety = st.number_input(
            "Marge supplémentaire pour prévoir les frais (%)",
            min_value=0.0,
            max_value=900.0,
            value=round((FeeTokenPolicy.from_mapping(saved).safety_multiplier - 1) * 100, 2),
            step=5.0,
            disabled=not bnb_monitor,
            help="25 % signifie prévoir 1,25 USDT de réserve pour des frais estimés à 1 USDT.",
        )

    if st.button("Enregistrer les réglages", type="primary"):
        service.save_user_settings(
            {
                "worker_interval": int(interval),
                "heartbeat_stale_after": int(heartbeat_warn),
                "exit_on_crossed_stop": bool(exit_on_crossed_stop),
                "max_risk_per_position_percent": float(max_risk),
                "max_total_risk_percent": float(max_total_risk),
                "max_open_positions": int(max_positions),
                "max_exposure_per_symbol_percent": float(max_exposure),
                "capital_reserve_percent": int(reserve),
                "bnb_fee_monitor_enabled": bool(bnb_monitor),
                "bnb_fee_alert_threshold_usdt": float(bnb_threshold),
                "bnb_fee_block_new_buys": bool(bnb_block_buys),
                "bnb_fee_safety_multiplier": 1 + float(bnb_safety) / 100,
            }
        )
        st.success(
            "Réglages enregistrés dans data/settings.json. "
            "L'intervalle de boucle s'applique au worker au prochain cycle. "
            "Les limites de risque s'appliquent aux prochaines simulations et validations "
            "de nouveaux trades ; elles ne modifient pas les positions déjà ouvertes. "
            "La surveillance BNB est relue automatiquement par le worker."
        )

    st.divider()
    st.subheader("Automatisation TP/SL")
    st.markdown(
        """
- **TP** : surveillés par le worker, jamais par un OCO (évite `MAX_NUM_ALGO_ORDERS`).
- **SL** : un seul ordre `STOP_LOSS_LIMIT` côté Binance, pour la quantité restante.
- **Déclenchement** : un TP n'est validé qu'après confirmation du fill par Binance.
- **Déplacement du SL** : uniquement si l'écart dépasse 0,05 %, pour éviter les allers-retours.
"""
    )

with tabs[1]:
    st.divider()
    st.subheader("Protection en cas de chute du marché")
    st.caption("Si BTC baisse fortement, les nouvelles entrées automatiques sont suspendues : les signaux restent "
               "dans la boîte et ne partent ensuite que s'ils sont encore assez récents. Rien n'est vendu.")
    guard_saved = get_settings_store().load()
    guard_saved = guard_saved if isinstance(guard_saved, dict) else {}
    with st.form("market_guard_preferences"):
        guard_enabled = st.toggle("Activer la protection", value=bool(guard_saved.get("market_guard_enabled", True)),
                                  key="market_guard_enabled_toggle")
        guard_cols = st.columns(3)
        guard_drop = guard_cols[0].number_input("Baisse de BTC (%)", min_value=0.5, max_value=50.0, step=0.5,
                                                value=float(guard_saved.get("market_guard_drop_percent", 3.0)),
                                                key="market_guard_drop_input")
        guard_window = guard_cols[1].number_input("En combien d'heures", min_value=1.0, max_value=48.0, step=1.0,
                                                  value=float(guard_saved.get("market_guard_window_hours", 4.0)),
                                                  key="market_guard_window_input")
        guard_pause = guard_cols[2].number_input("Pause des entrées (heures)", min_value=0.5, max_value=72.0,
                                                 step=0.5, value=float(guard_saved.get("market_guard_pause_hours", 6.0)),
                                                 key="market_guard_pause_input")
        guard_tighten = st.toggle(
            "Au déclenchement, remonter au seuil de rentabilité les stops des positions en gain",
            value=bool(guard_saved.get("market_guard_tighten_stops", False)),
            key="market_guard_tighten_toggle",
            help="Seulement les positions dont le prix est au-dessus du seuil de rentabilité (frais compris).",
        )
        if st.form_submit_button("Enregistrer la protection"):
            get_settings_store().update({
                "market_guard_enabled": bool(guard_enabled), "market_guard_drop_percent": float(guard_drop),
                "market_guard_window_hours": float(guard_window), "market_guard_pause_hours": float(guard_pause),
                "market_guard_tighten_stops": bool(guard_tighten),
            })
            st.success("Protection enregistrée (prise en compte par le worker au prochain contrôle, ≤ 5 min).")

    st.divider()
    st.subheader("Perte maximale du jour")
    st.caption("Résultat du jour = gains et pertes des positions terminées depuis 00:00 UTC (frais compris) + latent "
               "des positions ouvertes. Au seuil, plus aucune nouvelle entrée (manuelle ou automatique) jusqu'à "
               "00:00 UTC ; les positions ouvertes restent suivies. Rien n'est vendu.")
    loss_saved = get_settings_store().load()
    loss_saved = loss_saved if isinstance(loss_saved, dict) else {}
    with st.form("daily_loss_preferences"):
        loss_enabled = st.toggle("Activer la perte maximale du jour", key="daily_loss_enabled_toggle",
                                 value=bool(loss_saved.get("daily_loss_enabled", True)))
        loss_percent = st.number_input("Seuil (% du capital)", min_value=0.5, max_value=50.0, step=0.5,
                                       value=float(loss_saved.get("daily_loss_percent", 3.0)),
                                       key="daily_loss_percent_input")
        if st.form_submit_button("Enregistrer la perte maximale"):
            get_settings_store().update({"daily_loss_enabled": bool(loss_enabled),
                                         "daily_loss_percent": float(loss_percent)})
            st.success("Perte maximale du jour enregistrée (prise en compte par le worker en moins d'une minute).")


# ==========================================================================
# Presets
# ==========================================================================

with tabs[2]:
    st.subheader("Presets enregistrés")
    presets = service.presets()

    if not presets:
        st.caption(
            "Aucun preset. Les presets sont enregistrés depuis New Trade "
            "(champ « Nom du preset »)."
        )
    else:
        for name, payload in presets.items():
            with st.expander(name):
                st.json(payload)
                if st.button("Supprimer", key=f"del_preset_{name}"):
                    service.delete_preset(name)
                    st.success(f"Preset « {name} » supprimé.")
                    st.rerun()

    st.divider()
    st.subheader("Favoris")
    st.caption(
        "Les favoris sont mémorisés dans data/settings.json et proposés en tête "
        "de liste dans New Trade."
    )
    favorites = st.text_input(
        "Paire à ajouter aux favoris", "",
        help="Ex. BTCUSDT, ETHUSDT, SOLUSDT",
    ).strip().upper()
    current_favorites = saved.get("favorites", []) if isinstance(saved, dict) else []

    if favorites and st.button("Ajouter le favori"):
        updated = sorted(set(list(current_favorites) + [favorites]))
        service.save_user_settings({"favorites": updated})
        st.success(f"{favorites} ajouté aux favoris.")
        st.rerun()

    if current_favorites:
        st.markdown("**Favoris enregistrés** : " + ", ".join(current_favorites))

# ==========================================================================
# Notifications
# ==========================================================================

with tabs[3]:
    st.subheader("Notifications Windows / navigateur")
    st.caption(
        "Elles peuvent s'afficher lorsque tu consultes un autre onglet ou une autre "
        "application. L'onglet du bot doit rester ouvert."
    )
    browser_saved = service.user_settings()
    browser_prefs = browser_alert_preferences(browser_saved)
    browser_sound = st.checkbox(
        "Bip pour les nouveaux événements",
        value=browser_prefs.sound,
        key="settings_browser_alert_sound",
    )
    browser_mode = st.radio(
        "Fermeture demandée au navigateur",
        ["Après délai", "Manuellement"],
        index=1 if browser_prefs.manual else 0,
        key="settings_browser_alert_mode",
    )
    browser_duration = st.number_input(
        "Durée (secondes)", min_value=1, max_value=120,
        value=browser_prefs.duration_seconds, step=1,
        disabled=browser_mode == "Manuellement",
        key="settings_browser_alert_duration",
    )
    if st.button("Enregistrer les notifications du navigateur"):
        service.save_user_settings({
            "browser_alert_sound": bool(browser_sound),
            "browser_alert_mode": "manual" if browser_mode == "Manuellement" else "auto",
            "browser_alert_duration": int(browser_duration),
        })
        st.success("Préférences enregistrées dans data/settings.json.")

    st.markdown("**Autorisation du navigateur**")
    st.html(PERMISSION_HTML, unsafe_allow_javascript=True)
    st.caption(
        "Clique sur « Activer les notifications du navigateur », puis accepte "
        "la demande. Windows peut décider de la durée réelle d'affichage."
    )
    if st.button("Tester la notification Windows et le bip"):
        st.html(
            notification_html(
                {"ts": uuid.uuid4().hex, "event": "TEST", "symbol": "Bot",
                 "message": "Notification de test · aucun ordre envoyé"},
                duration_seconds=int(browser_duration),
                manual=browser_mode == "Manuellement",
            ),
            unsafe_allow_javascript=True,
        )
        st.audio(alert_tone(), autoplay=True)
        st.info("Si aucune notification système n'apparaît, vérifie l'autorisation du navigateur.")

    st.divider()
    st.subheader("Canaux")
    st.markdown(f"**{channel_summary(service.notifications)}**")

    st.markdown(
        """
| Canal | État | Configuration |
|---|---|---|
| Telegram | implémenté | `BSM_TELEGRAM_BOT_TOKEN`, `BSM_TELEGRAM_CHAT_ID` |
| Email | implémenté | `BSM_SMTP_HOST`, `BSM_SMTP_USER`, `BSM_SMTP_PASSWORD`, `BSM_SMTP_FROM`, `BSM_SMTP_TO` |
| SMS | réservé | interface présente, envoi désactivé |
| WhatsApp | réservé | interface présente, envoi désactivé |
"""
    )

    st.subheader("Test d'envoi")
    if not service.notifications.active_channels:
        st.warning("Aucun canal configuré : renseigner `.env` puis recharger la configuration.")
    else:
        if st.button("Envoyer une notification de test"):
            from binance_spot_manager.notification_engine import Notification

            results = service.notifications.notify(
                Notification(
                    event="TEST",
                    title="Notification de test",
                    body="BinanceSpotManager — test de canal de notification.",
                )
            )
            for channel, ok in results.items():
                (st.success if ok else st.error)(
                    f"{channel} : {'envoyé' if ok else 'échec'}"
                )

    st.subheader("Événements notifiables")
    st.markdown(
        """
Signal à confirmer (désactivé par défaut, Settings → Signaux) · Entry créée · Entry remplie · Entry partielle ·
TP atteint · TP exécuté · SL déplacé · SL exécuté · Position terminée ·
Erreur Binance · Worker offline · Capital insuffisant · Désynchronisation
"""
    )
    st.caption(
        "Les préférences par position (quels événements notifier) se règlent "
        "dans la page Positions."
    )

with tabs[3]:
    st.divider()
    st.subheader("Rapport quotidien")
    report_saved = get_settings_store().load()
    report_saved = report_saved if isinstance(report_saved, dict) else {}
    with st.form("daily_report_preferences"):
        report_enabled = st.toggle("Envoyer un rapport chaque jour", key="daily_report_enabled_toggle",
                                   value=bool(report_saved.get("daily_report_enabled", True)))
        report_hour = st.number_input("Heure d'envoi (UTC)", min_value=0, max_value=23, step=1,
                                      value=int(report_saved.get("daily_report_hour_utc", 20)),
                                      key="daily_report_hour_input")
        st.caption("Résultat net du jour et des 7 derniers jours (frais compris), positions ouvertes, risque si "
                   "tous les stops sont touchés, meilleur et pire canal.")
        if st.form_submit_button("Enregistrer le rapport"):
            get_settings_store().update({"daily_report_enabled": bool(report_enabled),
                                         "daily_report_hour_utc": int(report_hour)})
            st.success("Rapport quotidien enregistré.")


# ==========================================================================
# Diagnostic
# ==========================================================================

with tabs[4]:
    @st.fragment(run_every="1s")
    def market_diagnostics_panel():
        st.subheader("Flux des prix — Binance Demo")
        st.caption("Lecture seule, actualisée chaque seconde. Un prix WebSocket de plus de 5 s n'est plus utilisé ; REST prend le relais.")
        runtime = service.runtime()
        snapshots = [
            ("Interface", service.market_prices.snapshot()),
            ("Worker", runtime.price_diagnostics),
        ]
        labels = {
            "IDLE": "En veille", "DISABLED": "Désactivé",
            "CONNECTING": "Connexion", "CONNECTED": "Connecté",
            "RECONNECTING": "Reconnexion",
        }
        for title, diagnostic in snapshots:
            st.markdown(f"**{title}**")
            if not diagnostic:
                st.info("Diagnostic non disponible : redémarrer le worker pour l'activer.")
                continue
            elapsed = max(0.0, time.time() - diagnostic["captured_at"])
            cols = st.columns(3)
            cols[0].metric("Flux", labels.get(diagnostic["state"], diagnostic["state"]))
            cols[1].metric("Reconnexions", diagnostic["reconnects"])
            cols[2].metric("Âge du diagnostic", f"{elapsed:.0f} s")
            if title == "Worker" and not runtime.is_alive():
                st.warning("Worker arrêté ou sans heartbeat récent : dernier diagnostic enregistré, pas un état en direct.")
            if diagnostic.get("disabled_reason"):
                st.info(diagnostic["disabled_reason"])
            if diagnostic.get("last_error"):
                st.caption(f"Dernière erreur du flux : {diagnostic['last_error']}")
            rows = []
            for item in diagnostic["symbols"]:
                age = item["age_seconds"]
                age = age + elapsed if age is not None else None
                row = {
                    "Paire": item["symbol"],
                    "Âge du prix WS": f"{age:.1f} s" if age is not None else "Non reçu",
                    "Prix WS frais": age is not None and age <= 5,
                }
                if title == "Worker":
                    row["Source à la dernière consultation"] = diagnostic.get("sources", {}).get(item["symbol"], "—")
                rows.append(row)
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
            else:
                st.caption("Aucune paire suivie pour l'instant.")

    market_diagnostics_panel()
    st.divider()
    st.subheader("Contrôle des TP/SL chez Binance Demo")
    st.caption("Contrôle ponctuel en lecture seule des positions ouvertes. Aucun ordre n'est créé, corrigé ou annulé. Les sorties locales sans ordre Binance sont indiquées séparément.")
    if st.button("Comparer les sorties avec Binance Demo"):
        with st.spinner("Lecture des ordres de sortie..."):
            try:
                exit_report = service.inspect_exit_orders()
            except Exception as exc:
                st.error(str(exc))
            else:
                st.caption(f"Instantané du {exit_report['checked_at']} — le worker peut faire évoluer les ordres pendant le contrôle.")
                if exit_report["rows"]:
                    st.dataframe(exit_report["rows"], hide_index=True, width="stretch")
                    st.info("OK confirme uniquement les champs contrôlés sur un ordre ouvert, pas l'exécution future ni la protection globale du portefeuille.")
                else:
                    st.info("Aucune sortie à contrôler sur les positions ouvertes.")
    st.divider()
    st.subheader("Diagnostic du worker")
    worker_status = service.worker_status()
    worker_cols = st.columns(4)
    worker_cols[0].metric("État", worker_status.label)
    worker_cols[1].metric("PID", worker_status.pid or "—")
    worker_cols[2].metric("Heartbeat", f"{worker_status.heartbeat_age:.0f} s" if worker_status.heartbeat_age is not None else "—")
    worker_cols[3].metric("Boucles", worker_status.loop_count)

    st.subheader("Connexion Binance")

    if st.button("Tester la connexion maintenant"):
        with st.spinner("Interrogation de Binance Demo..."):
            report = BinanceSpotClient(settings).connectivity_report()

        rows = {
            "URL": report["base_url"],
            "URL whitelistée": report["url_whitelisted"],
            "Ping": report["ping_ok"],
            "Offset serveur (ms)": report["server_time_offset_ms"],
            "Clés présentes": report["credentials_set"],
            "Compte lisible": report["account_ok"],
            "canTrade": report["can_trade"],
            f"{settings.quote_asset} libre": report["quote_free"],
        }
        st.table([{"Contrôle": k, "Valeur": str(v)} for k, v in rows.items()])

        for error in report["errors"]:
            st.error(error)
        if not report["errors"]:
            st.success("Connexion Binance Demo opérationnelle.")

    st.divider()
    st.subheader("Fichiers et dossiers")
    st.markdown(
        """
| Chemin | Contenu |
|---|---|
| `data/positions/` | une position par fichier JSON |
| `data/bot_runtime.json` | état, PID, heartbeat du worker |
| `data/bot_stop.flag` | présent = arrêt demandé |
| `data/bot_worker.lock` | verrou anti double-worker |
| `data/settings.json` | réglages de cette page |
| `data/presets.json` | presets enregistrés |
| `logs/events.jsonl` | journal d'événements |
| `logs/bot.log` | journal d'exécution |
| `logs/errors.log` | erreurs applicatives |
"""
    )

    st.subheader("Maintenance")
    col1, col2 = st.columns(2)

    if col1.button("Nettoyer l'état du worker"):
        st.info(service.process_manager.clear_orphan_state())

    if col2.button("Rafraîchir le cache de l'interface"):
        st.cache_resource.clear()
        st.success("Cache vidé.")
        st.rerun()

    summary = service.summary()
    st.subheader("Résumé des données")
    st.json(summary)
