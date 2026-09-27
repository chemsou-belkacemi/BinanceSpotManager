"""Helpers d'interface partages par toutes les pages Streamlit."""

from __future__ import annotations

import io
import math
import struct
import wave
from typing import Any, Optional

import streamlit as st

from binance_spot_manager.config import Settings, get_settings
from binance_spot_manager.dashboard_service import DashboardService
from binance_spot_manager.symbol_rules import SymbolRules, SymbolRulesError, SymbolRulesCache
from binance_spot_manager.binance_client import BinanceSpotClient
from binance_spot_manager.ui_alerts import unseen_alerts

MODE_COLORS = {
    "DRY_RUN": "🟦",
    "DEMO_MANUAL": "🟨",
    "DEMO_AUTO": "🟩",
    "LIVE": "🟥",
}


@st.cache_resource(show_spinner=False)
def get_service() -> DashboardService:
    """Service unique par session Streamlit (client HTTP reutilise)."""
    return DashboardService(get_settings())


@st.cache_resource(show_spinner=False)
def get_rules_cache() -> SymbolRulesCache:
    return SymbolRulesCache(BinanceSpotClient(get_settings()))


def page_header(title: str, subtitle: str = "") -> None:
    st.title(title)
    if subtitle:
        st.caption(subtitle)


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
        if status.heartbeat_age is not None:
            st.caption(f"Heartbeat : il y a {status.heartbeat_age:.0f} s")
        if status.is_stale:
            st.warning("Heartbeat ancien : le worker est peut-être bloqué")
        if status.last_error:
            st.error(status.last_error)

        st.divider()
        st.markdown(f"**Mode** : {settings.environment.value}")
        st.markdown(f"**Run** : {settings.run_mode.value}")
        if not settings.has_credentials:
            st.warning("Clés API non configurées (.env)")

        st.divider()
        summary = service.summary()
        st.metric("Positions ouvertes", summary["open"])
        st.caption(f"{summary['total']} positions au total")

    global_alerts()


def _alert_tone() -> bytes:
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


def _remember_alert_sound() -> None:
    st.session_state["alert_sound_preference"] = bool(
        st.session_state["alert_sound_enabled"]
    )


@st.fragment(run_every="3s")
def global_alerts() -> None:
    """Surveille les evenements depuis chaque page, sans rejouer l'historique."""
    records = get_service().events.tail(limit=100)
    if "alert_last_seen" not in st.session_state:
        st.session_state.alert_last_seen = max(
            (str(record.get("ts") or "") for record in records), default=""
        )

    with st.sidebar:
        st.divider()
        st.markdown("### Alertes")
        sound_enabled = st.checkbox(
            "Bip pour les nouveaux événements",
            value=bool(st.session_state.get("alert_sound_preference", False)),
            key="alert_sound_enabled",
            on_change=_remember_alert_sound,
        )
        if st.button("Tester le bip et la notification", key="alert_sound_test"):
            st.toast("Notification de test", icon="🔔")
            st.audio(_alert_tone(), autoplay=True)

        alerts, last_seen = unseen_alerts(records, st.session_state.alert_last_seen)
        st.session_state.alert_last_seen = last_seen
        if alerts:
            newest = alerts[-1]
            st.session_state.alert_latest = newest
            st.toast(
                f"{newest.get('symbol') or 'Bot'} · "
                f"{newest.get('message') or newest.get('event')}",
                icon="🚨" if newest.get("level") in {"ERROR", "CRITICAL"} else "🔔",
            )
            if sound_enabled:
                st.audio(_alert_tone(), autoplay=True)

        latest = st.session_state.get("alert_latest")
        if latest:
            message = (
                f"{latest.get('ts', '')} · {latest.get('symbol') or 'Bot'} · "
                f"{latest.get('message') or latest.get('event')}"
            )
            if latest.get("level") in {"ERROR", "CRITICAL"}:
                st.error(message)
            else:
                st.info(message)
        else:
            st.caption("Alertes locales actives : TP, SL, erreurs et fin de position.")


def fmt_price(value: Optional[float], decimals: int = 2) -> str:
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


@st.fragment(run_every="2s")
def live_price_metric(symbol: str, quote_asset: str) -> None:
    price = get_service().current_price(symbol)
    st.metric("Prix actuel", fmt_price(price), quote_asset)
    st.caption("Actualisation automatique toutes les 2 s")


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
        fmt_price(balances.get(rules.quote_asset, 0.0)),
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
