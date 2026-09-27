"""Settings — réglages, presets, sécurité, notifications, diagnostic."""

from __future__ import annotations

import sys
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

tabs = st.tabs(["Sécurité", "Worker & risque", "Presets", "Notifications", "Diagnostic"])

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

    if st.button("Enregistrer les réglages", type="primary"):
        service.save_user_settings(
            {
                "worker_interval": int(interval),
                "heartbeat_stale_after": int(heartbeat_warn),
                "max_risk_per_position_percent": float(max_risk),
                "max_total_risk_percent": float(max_total_risk),
                "max_open_positions": int(max_positions),
                "max_exposure_per_symbol_percent": float(max_exposure),
                "capital_reserve_percent": int(reserve),
            }
        )
        st.success(
            "Réglages enregistrés dans data/settings.json. "
            "L'intervalle de boucle s'applique au worker au prochain cycle. "
            "Les autres réglages de risque ne sont pas appliqués au worker par ce bouton."
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
