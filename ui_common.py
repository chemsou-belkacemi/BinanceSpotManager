"""Helpers d'interface partages par toutes les pages Streamlit."""

from __future__ import annotations

import io
import math
import struct
import wave
import uuid
from typing import Any, Optional

import streamlit as st

from binance_spot_manager.config import Settings, get_settings
from binance_spot_manager.dashboard_service import DashboardService
from binance_spot_manager.symbol_rules import SymbolRules, SymbolRulesError, SymbolRulesCache
from binance_spot_manager.binance_client import BinanceSpotClient
from binance_spot_manager.browser_notifications import (
    browser_alert_preferences, notification_html,
)
from binance_spot_manager.pnl_display import format_price
from binance_spot_manager.ui_alerts import unseen_alerts
from binance_spot_manager import auth

MODE_COLORS = {
    "DRY_RUN": "🟦",
    "DEMO_MANUAL": "🟨",
    "DEMO_AUTO": "🟩",
    "LIVE": "🟥",
}


@st.cache_resource(show_spinner=False)
def get_service() -> DashboardService:
    """Service partage dans le processus : application locale monocompte."""
    return DashboardService(get_settings())


@st.cache_resource(show_spinner=False)
def get_rules_cache() -> SymbolRulesCache:
    return SymbolRulesCache(BinanceSpotClient(get_settings()))


def _login_required(store: "auth.AccountStore") -> bool:
    try:
        return auth.auth_required(store)
    except Exception:  # noqa: BLE001 - echec sur : en cas de doute, connexion exigee
        return True


def require_login() -> Optional[str]:
    """Garde appelee par CHAQUE page AVANT tout affichage.

    Renvoie l'identifiant connecte, ou None si la connexion n'est pas exigee (aucun compte cree
    et BSM_AUTH_REQUIRED non defini). Sinon affiche la page de connexion et arrete la page.
    """
    store = auth.AccountStore()
    if not _login_required(store):
        return None
    try:
        user = auth.current_user(st.session_state, store)
    except Exception:  # noqa: BLE001
        user = None
    if user:
        return user
    _login_page(store)
    st.stop()
    return None  # jamais atteint : st.stop() interrompt la page


def _login_page(store: "auth.AccountStore") -> None:
    st.title("BinanceSpotManager")
    st.caption("Connexion requise")
    try:
        has_accounts = store.has_accounts() and bool(store.usernames())
    except RuntimeError as exc:
        st.error(str(exc))
        return
    if not has_accounts:
        st.error(
            "Connexion exigée (BSM_AUTH_REQUIRED) mais aucun compte n'existe. Créer le premier compte "
            "en ligne de commande : `python scripts/creer_compte.py <identifiant>` "
            "(Docker : `make compte NAME=<identifiant>`)."
        )
        return
    with st.form("bsm_login", clear_on_submit=True):
        username = st.text_input("Identifiant", autocomplete="username")
        password = st.text_input("Mot de passe", type="password", autocomplete="current-password")
        code = st.text_input("Code à 6 chiffres (application d'authentification)", max_chars=6,
                             autocomplete="one-time-code")
        submitted = st.form_submit_button("Se connecter", type="primary")
    if submitted:
        try:
            result = store.authenticate(username, password, code)
        except Exception as exc:  # noqa: BLE001 - coffre ou fichier illisible : rester ferme
            st.error(f"Connexion impossible : {exc}")
            return
        if result.ok:
            auth.open_session(st.session_state, result)
            st.rerun()
        st.error(result.message)
    st.caption(
        f"Session fermée après {auth.idle_timeout_seconds() // 60} min d'inactivité. "
        f"{auth.MAX_FAILURES} échecs bloquent le compte {auth.LOCK_SECONDS // 60} minutes."
    )


def session_still_valid() -> bool:
    """Controle sans prolonger la session (rafraichissements automatiques des fragments)."""
    store = auth.AccountStore()
    if not _login_required(store):
        return True
    try:
        return auth.current_user(st.session_state, store, touch=False) is not None
    except Exception:  # noqa: BLE001
        return False


def page_header(title: str, subtitle: str = "") -> None:
    st.title(title)
    if subtitle:
        st.caption(subtitle)


def submit_to_worker(action: str, payload: dict, *, confirmation_key: str) -> bool:
    """Une confirmation reste stable entre clics et reruns ; aucune API d'ordre ici."""
    key = "command_confirmation_" + confirmation_key
    request_key = st.session_state.setdefault(key, uuid.uuid4().hex)
    try:
        command = get_service().submit_command(action, payload, request_key=request_key)
    except Exception as exc:
        st.error(f"Demande non ajoutee : {exc}")
        return False
    st.info(f"Demande {command['id'][:12]} — {command['state']}. Suivi dans la page Operations.")
    return True


def new_command_confirmation(key: str) -> None:
    if "command_confirmation_" + key in st.session_state:
        st.caption("Une confirmation a deja ete preparee. Verifie son resultat dans Operations avant une nouvelle demande.")
        if st.button("Preparer une nouvelle demande", key="reset_confirmation_" + key):
            del st.session_state["command_confirmation_" + key]
            st.rerun()


def banner(settings: Optional[Settings] = None) -> None:
    """Bandeau de securite toujours visible."""
    settings = settings or get_settings()
    icon = MODE_COLORS.get(settings.run_mode.value, "⬜")
    if settings.environment.value == "DEMO":
        st.success(
            f"{icon} **{settings.mode_label}** — écritures limitées à `{settings.base_url}`",
            icon="🔒",
        )
    else:
        st.error(
            f"🚫 **{settings.mode_label}** — le mode Live est désactivé dans cette version",
            icon="🚫",
        )


def sidebar_status(settings: Optional[Settings] = None) -> None:
    settings = settings or get_settings()
    service = get_service()

    with st.sidebar:
        st.markdown("### État")
        status = service.worker_status()

        dot = "🟢" if (status.running and status.pid_alive) else "⚪"
        st.markdown(f"{dot} **{status.label}**")
        if status.pid:
            st.caption(f"PID {status.pid}")
        if status.is_stale:
            st.warning("Le worker ne répond plus : il est peut-être bloqué")
        if status.last_error:
            st.error(status.last_error)

        st.divider()
        st.markdown(f"**Mode** : {settings.environment.value}")
        st.markdown(f"**Run** : {settings.run_mode.value}")
        if not settings.has_credentials:
            st.warning("Clés API non configurées : Settings → Sécurité")
        elif settings.credentials_source == "vault":
            st.caption("Clés API : coffre chiffré de l'instance")
        if settings.credentials_error:
            st.error(f"Coffre des clés : {settings.credentials_error}")
        licence_sidebar()
        session = st.session_state.get(auth.SESSION_KEY)
        if isinstance(session, dict) and session.get("username"):
            st.divider()
            st.caption(f"Connecté : {session['username']}")
            if st.button("Se déconnecter", key="bsm_logout"):
                auth.close_session(st.session_state)
                st.rerun()

        st.divider()
        summary = service.summary()
        st.metric("Positions ouvertes", summary["open"])
        st.caption(f"{summary['total']} positions au total")
        pending = signals_to_confirm(settings)
        if pending:
            st.warning(f"{pending} signal(aux) à confirmer · page Signaux")

    global_alerts()


def signals_to_confirm(settings: Settings) -> int:
    """Nombre de signaux « À confirmer » (lecture seule ; aucun fichier créé)."""
    try:
        from binance_spot_manager.command_store import account_scope
        from binance_spot_manager.signal_inbox import SignalInbox

        inbox = SignalInbox()
        if not inbox.path.exists():
            return 0
        return inbox.count_review(account_scope(settings))
    except Exception:  # noqa: BLE001 - badge indicatif seulement
        return 0


def licence_sidebar() -> None:
    """Etat de la licence de location, quand elle est exigee."""
    from binance_spot_manager import licence

    if not licence.licence_required():
        return
    status = licence.current_status()
    if not status.valid:
        st.error(f"Licence : {status.reason}. Aucune nouvelle entrée ; positions ouvertes suivies.")
    elif status.expiring_soon:
        st.warning(f"Licence : fin le {status.fin} ({status.days_left} j)")
    else:
        st.caption(f"Licence {status.offre} valable jusqu'au {status.fin}")


def alert_tone() -> bytes:
    """Petit bip WAV genere localement, sans fichier ni service externe."""
    sample_rate = 16000
    frames = b"".join(
        struct.pack("<h", int(6500 * math.sin(2 * math.pi * 880 * i / sample_rate)))
        for i in range(int(sample_rate * 0.22))
    )
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return output.getvalue()


@st.fragment(run_every="1s")
def global_alerts() -> None:
    """Surveille les evenements depuis chaque page, sans rejouer l'historique."""
    if not session_still_valid():
        # Session expiree pendant que la page se rafraichit seule : retour a la connexion.
        st.rerun()
    service = get_service()
    preferences = browser_alert_preferences(service.user_settings())
    records = service.events.tail(limit=100)
    if "alert_last_seen" not in st.session_state:
        st.session_state.alert_last_seen = max(
            (str(record.get("ts") or "") for record in records), default=""
        )

    # Reserve un emplacement dans le fragment lui-meme : ecrire directement
    # dans st.sidebar lors d'un rafraichissement du fragment leve une erreur.
    audio_slot = st.empty()
    alerts, last_seen = unseen_alerts(records, st.session_state.alert_last_seen)
    st.session_state.alert_last_seen = last_seen
    if hasattr(service.events, "alert_inbox"):
        inbox = service.events.alert_inbox()
        alerts = [record for record in alerts if inbox.claim_delivery(record)]
        critical = [a for a in inbox.recent(unread=True) if a["record"].get("level") == "CRITICAL"]
        if critical:
            st.warning(f"{len(critical)} alerte(s) critique(s) non acquittee(s). Consulter Operations.")
    if alerts:
        newest = alerts[-1]
        st.session_state.alert_latest = newest
        if preferences.sound:
            audio_slot.audio(alert_tone(), autoplay=True)
        st.html(
            notification_html(
                newest, duration_seconds=preferences.duration_seconds,
                manual=preferences.manual,
            ),
            unsafe_allow_javascript=True,
        )


def fmt_price(value: Optional[float], decimals: Optional[int] = None) -> str:
    """Prix : décimales selon la grandeur (0.37120, 84 000.00), sauf nombre de décimales imposé."""
    if value is None:
        return "—"
    if decimals is None:
        return format_price(value)
    return f"{value:,.{decimals}f}".replace(",", " ")


def fmt_amount(value: Optional[float], decimals: int = 2) -> str:
    """Montant en devise de cotation (solde, capital, PnL, frais, risque) : 2 décimales."""
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}".replace(",", " ")


def fmt_qty(value: Optional[float], decimals: int = 8) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")


def fmt_quote(value: Optional[float], asset: str = "USDT") -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} {asset}".replace(",", " ")


def fmt_percent(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:+.{decimals}f} %"


def pnl_color(value: float) -> str:
    if value > 0:
        return "normal"
    if value < 0:
        return "inverse"
    return "off"


def load_rules(symbol: str) -> tuple[Optional[SymbolRules], str]:
    """Retourne (regles, message). Message vide si OK."""
    if not symbol or not symbol.strip():
        return None, ""
    try:
        return get_rules_cache().get(symbol.strip().upper()), ""
    except SymbolRulesError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 — reseau indisponible, message clair
        return None, f"Paire non vérifiable : {exc}"


@st.cache_data(ttl=1, show_spinner=False)
def live_price(symbol: str) -> Optional[float]:
    """Prix courtement partage entre les blocs de la page New Trade."""
    return get_service().current_price(symbol)


@st.fragment(run_every="1s")
def live_price_metric(symbol: str, quote_asset: str) -> None:
    price = live_price(symbol)
    st.metric("Prix actuel", fmt_price(price), quote_asset)
    st.caption("Actualisation automatique toutes les 1 s")


@st.fragment(run_every="1s")
def live_market_price(symbol: str) -> None:
    st.info(f"Marché ≈ {fmt_price(live_price(symbol))}")


def symbol_status_box(symbol: str, rules: Optional[SymbolRules], balances: dict) -> None:
    """Bloc d'etat d'une paire, tel que decrit section 21."""
    if not symbol:
        return
    if rules is None:
        st.error(f"{symbol} ❌ — paire inexistante ou non vérifiable")
        return

    st.success(f"{rules.symbol} ✅ — paire valide ({rules.status})")
    cols = st.columns(4)
    with cols[0]:
        live_price_metric(rules.symbol, rules.quote_asset)
    cols[1].metric("Base", rules.base_asset)
    cols[2].metric("Quote", rules.quote_asset)
    cols[3].metric(
        f"{rules.quote_asset} disponibles",
        fmt_amount(balances.get(rules.quote_asset, 0.0)),
    )
    st.caption(
        f"tickSize {rules.tick_size} · stepSize {rules.step_size} · "
        f"minQty {rules.min_qty} · minNotional {rules.min_notional}"
    )


def error_list(errors: list[str], title: str = "Corrections nécessaires") -> None:
    if not errors:
        st.success("✅ Stratégie prête")
        return
    st.error(f"❌ {title}")
    for error in errors:
        st.markdown(f"- {error}")


def warning_list(warnings: list[str]) -> None:
    for warning in warnings:
        st.warning(f"⚠️ {warning}")


def render_kv_table(rows: list[tuple[str, Any]]) -> None:
    for label, value in rows:
        col1, col2 = st.columns([2, 3])
        col1.markdown(f"**{label}**")
        col2.markdown(str(value))


def colored_pnl(value, text=None):
    if value is None:
        return "—"
    text = text or fmt_amount(value)
    color = "green" if value > 0 else "red" if value < 0 else "gray"
    return f":{color}[{text}]"


def fresh_position_view(service, position_id):
    """Read a new snapshot on every tick, without saving or mutating editor data."""
    from binance_spot_manager.position_engine import recompute_position
    from binance_spot_manager.pnl_display import position_return
    saved = service.positions.load(position_id)
    if saved is None:
        return None, None, None
    position = saved.model_copy(deep=True)
    price = service.current_price(position.symbol)
    if price is not None and price > 0:
        position.metrics.current_price = price
    fee_rates = service.fee_rates(position) if hasattr(service, "fee_rates") else {}
    recompute_position(position, fee_rates=fee_rates)
    rate = 1.0 if position.quote_asset == "USDT" else service.current_price(f"{position.quote_asset}USDT")
    return position, price, position_return(position, rate, fee_rates=fee_rates)


def pnl_metric(container, label, value, text=None):
    container.caption(label)
    container.markdown(f"### {colored_pnl(value, text)}")


def pnl_dataframe(data, **kwargs):
    """Keep native table formatting, color only profit/loss columns."""
    import pandas as pd
    from binance_spot_manager.pnl_display import pnl_css
    frame = data if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
    columns = [name for name in frame.columns if any(
        word in str(name).lower() for word in ("pnl", "gain", "perte", "realise", "réalisé"))]
    if "Frais non convertis" in frame.columns:
        columns += [name for name in ("Total",) if name in frame.columns]
    st.dataframe(frame.style.map(pnl_css, subset=columns) if columns else frame, **kwargs)
