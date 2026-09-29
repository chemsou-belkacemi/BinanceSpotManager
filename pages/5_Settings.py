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

settings = st.session_state.setdefault("settings_obj", None) or reload_settings()
service = get_service()

st.set_page_config(page_title="Settings — BinanceSpotManager", page_icon="⚙️", layout="wide")
page_header("Settings", "Configuration, presets et diagnostic")
banner(settings)
sidebar_status(settings)

tabs = st.tabs(["Sécurité", "Worker & risque", "Presets", "Notifications", "Diagnostic", "Signaux"])

with tabs[5]:
    from binance_spot_manager.position_store import get_settings_store
    from binance_spot_manager.signal_sizing import SignalSizingPolicy
    from binance_spot_manager.telegram_signals import chat_allowlist

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
        "Le worker relit `data/signal_drop/incoming/` à chaque cycle. Chaque fichier JSON "
        "passe par le même parseur strict que Telegram ; la réception seule ne crée aucun ordre."
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
        "Ce réglage propose le budget lors de la simulation. Il ne supprime jamais "
        "la vérification du signal ni la confirmation manuelle avant l'ordre Demo."
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
    st.subheader("Exécution automatique")
    auto_was_enabled = bool(signal_preferences.get("signal_auto_execute_enabled", False))
    auto_execute = st.toggle(
        "Envoyer automatiquement les signaux Telegram valides au worker",
        value=auto_was_enabled,
        key="signal_auto_execute_toggle",
        help=(
            "Chaque nouveau signal autorisé peut créer immédiatement ses ordres LIMIT "
            "sur Binance Demo avec le budget défini ci-dessus."
        ),
    )
    auto_touch_stop = st.toggle(
        "Interpréter les SL (1h/15min) comme des stops au toucher",
        value=bool(signal_preferences.get("signal_auto_touch_stop", False)),
        disabled=not auto_execute,
        key="signal_auto_touch_stop_toggle",
        help="Active cette option seulement si cette interprétation correspond à ta stratégie.",
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
            get_settings_store().update({
                "signal_auto_execute_enabled": bool(auto_execute),
                "signal_auto_execute_enabled_since": enabled_since,
                "signal_auto_touch_stop": bool(auto_touch_stop),
                "signal_auto_max_age_minutes": int(auto_max_age),
                "signal_drop_auto_enabled": drop_auto,
                "signal_drop_auto_enabled_since": drop_since,
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
            f"refusés : {auto_diagnostics.get('rejected_total', 0)}"
        )
        if auto_diagnostics.get("last_detail"):
            st.caption(f"Dernier résultat : {auto_diagnostics['last_detail']}")

# ==========================================================================
# Sécurité
# ==========================================================================

with tabs[0]:
    st.subheader("Périmètre d'exécution")

    st.markdown(
        f"""
**Environnement** : `{settings.environment.value}`

**Mode opérationnel** : `{settings.run_mode.value}`

**URL de base** : `{settings.base_url}`

**URL autorisée** : {"✅ oui" if settings.base_url in ALLOWED_DEMO_BASE_URLS else "❌ NON"}

**Clés API** : {"configurées" if settings.has_credentials else "absentes"}
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
Signal reçu · Signal rejeté · Entry créée · Entry remplie · Entry partielle ·
TP atteint · TP exécuté · SL déplacé · SL exécuté · Position terminée ·
Erreur Binance · Worker offline · Capital insuffisant · Désynchronisation
"""
    )
    st.caption(
        "Les préférences par position (quels événements notifier) se règlent "
        "dans la page Positions."
    )

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
