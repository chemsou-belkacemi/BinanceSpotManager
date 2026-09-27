"""Dashboard — vue globale, worker, portefeuille, positions, ordres réels."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.config import get_settings  # noqa: E402
from binance_spot_manager.execution_engine import ExecutionEngine  # noqa: E402
from binance_spot_manager.models import EntryStatus, EventType, SLStatus  # noqa: E402
from binance_spot_manager.position_engine import recompute_position  # noqa: E402
from ui_common import (  # noqa: E402
    banner,
    fmt_percent,
    fmt_price,
    fmt_qty,
    fmt_quote,
    get_service,
    page_header,
    sidebar_status,
)

settings = get_settings()
service = get_service()

st.set_page_config(page_title="Dashboard — BinanceSpotManager", page_icon="📊", layout="wide")
page_header("Dashboard", "Worker, portefeuille et positions en temps réel")
banner(settings)
sidebar_status(settings)


# ==========================================================================
# Aides d'annulation
# ==========================================================================


def _apply_cancel_locally(order_id: int) -> bool:
    """Reporte une annulation sur l'etat local. Retourne True si une
    position a bien ete mise a jour.

    Sans cela, la position continuerait de croire que l'ordre vit, et le
    worker chercherait un ordre disparu a chaque cycle.
    """
    for position in service.positions.list_open():
        touched = False

        for entry in position.entries:
            if entry.order_id == order_id:
                entry.status = EntryStatus.CANCELED
                position.log(
                    EventType.ENTRY_CANCELED,
                    f"Entry {entry.sequence_number} annulée depuis le Dashboard",
                    entry_id=entry.entry_id,
                )
                touched = True

        if position.stop_loss.order_id == order_id:
            position.stop_loss.status = SLStatus.CANCELED
            position.log(
                EventType.MANUAL_CHANGE,
                "SL annulé depuis le Dashboard — position non protégée",
                order_id=order_id,
            )
            touched = True

        if touched:
            recompute_position(position)
            service.positions.save(position)
            return True

    return False


def _cancel_order(order, confirmed: bool) -> None:
    """Annule un ordre apres confirmation, puis met a jour l'etat local."""
    if not confirmed:
        st.warning("Coche la confirmation pour annuler cet ordre.")
        return

    execution = ExecutionEngine(settings=settings, events=service.events)
    result = execution.cancel_order(
        order.symbol,
        order_id=order.order_id,
        client_order_id=order.client_order_id or None,
    )

    if not result.success:
        st.error(f"Annulation refusée : {result.error}")
        return

    updated = _apply_cancel_locally(order.order_id)

    service.events.append(
        EventType.MANUAL_CHANGE,
        f"Ordre #{order.order_id} annulé depuis le Dashboard ({order.symbol})",
        symbol=order.symbol,
        level="WARNING",
        owner=order.owner or "hors bot",
        local_state_updated=updated,
    )

    st.success(f"Ordre #{order.order_id} annulé.")
    if updated:
        st.caption("État local mis à jour.")
    else:
        st.warning(
            "Ordre annulé côté Binance, mais aucune position locale ne le "
            "référençait — vérifier la réconciliation."
        )

    st.cache_resource.clear()
    st.rerun()


# ==========================================================================
# Worker
# ==========================================================================

st.subheader("Worker")

status = service.worker_status()
cols = st.columns(5)
cols[0].metric("État", status.label)
cols[1].metric("PID", status.pid or "—")
cols[2].metric(
    "Heartbeat",
    f"{status.heartbeat_age:.0f} s" if status.heartbeat_age is not None else "—",
)
cols[3].metric("Boucles", status.loop_count)
cols[4].metric("Positions suivies", status.positions_monitored)

if status.last_message:
    st.caption(f"Dernier message : {status.last_message}")
if status.last_error:
    st.error(f"Dernière erreur : {status.last_error}")
if status.is_stale:
    st.warning(
        "Le heartbeat n'est plus rafraîchi. Le worker est peut-être bloqué : "
        "utiliser l'arrêt forcé ci-dessous, puis relancer."
    )

actions = st.columns(5)
if actions[0].button("▶️ Démarrer le worker", width="stretch"):
    ok, message = service.process_manager.start()
    (st.success if ok else st.error)(message)
    st.rerun()

if actions[1].button("⏹️ Arrêter proprement", width="stretch"):
    ok, message = service.process_manager.request_stop()
    (st.success if ok else st.warning)(message)
    st.rerun()

if actions[2].button("🛑 Arrêt forcé", width="stretch"):
    ok, message = service.process_manager.stop(force=True, timeout_seconds=5)
    (st.success if ok else st.error)(message)
    st.rerun()

if actions[3].button("🧹 Nettoyer l'état", width="stretch"):
    st.info(service.process_manager.clear_orphan_state())
    st.rerun()

if actions[4].button("🔄 Rafraîchir", width="stretch"):
    st.cache_resource.clear()
    st.rerun()

st.divider()

# ==========================================================================
# Portefeuille
# ==========================================================================

st.subheader("Portefeuille")

view = service.portfolio()
if view.errors:
    for error in view.errors:
        st.warning(error)
st.caption(f"Source des soldes : {view.source}")

cols = st.columns(6)
cols[0].metric(f"{settings.quote_asset} libre", fmt_price(view.quote_free))
cols[1].metric("Capital engagé", fmt_price(view.capital_committed))
cols[2].metric("Capital en attente", fmt_price(view.capital_pending))
cols[3].metric("PnL latent", fmt_price(view.unrealized_pnl))
cols[4].metric("PnL réalisé", fmt_price(view.realized_pnl))
cols[5].metric("PnL total", fmt_price(view.total_pnl))

cols = st.columns(4)
cols[0].metric("Risque total", fmt_price(view.total_risk_quote), f"{view.total_risk_percent:.2f} %")
cols[1].metric("Exposition", f"{view.exposure_percent:.1f} %")
cols[2].metric("Réserve estimée", fmt_price(view.capital_reserved))
cols[3].metric("Positions ouvertes", view.open_positions)

st.divider()

# ==========================================================================
# Positions
# ==========================================================================

st.subheader("Positions ouvertes")

rows = [
    r
    for r in service.position_rows()
    if r.status in {"DRAFT", "PENDING_ENTRIES", "ACTIVE", "CLOSING"}
]

if not rows:
    st.info("Aucune position ouverte.")
else:
    table = pd.DataFrame(
        [
            {
                "Symbole": r.symbol,
                "État": r.status,
                "Sync": r.sync_status,
                "Prix moyen": fmt_price(r.average_price),
                "Prix actuel": fmt_price(r.current_price),
                "Quantité nette": fmt_qty(r.net_qty),
                "PnL": fmt_price(r.pnl_total),
                "PnL %": fmt_percent(r.pnl_percent),
                "SL": fmt_price(r.sl_price),
                "Prochain TP": (
                    f"TP{r.next_tp_number} @ {fmt_price(r.next_tp_price)}"
                    if r.next_tp_price
                    else "—"
                ),
                "Entries": f"{r.entries_filled}/{r.entries_total}",
                "TP": f"{r.tps_executed}/{r.tps_total}",
            }
            for r in rows
        ]
    )
    st.dataframe(table, width="stretch", hide_index=True)

    desync = [r for r in rows if r.has_desync]
    if desync:
        st.warning(
            "⚠️ Désynchronisation détectée sur : "
            + ", ".join(f"{r.symbol} ({r.sync_status})" for r in desync)
            + " — voir la page Positions pour le détail."
        )

st.divider()

# ==========================================================================
# Ordres réels — un bloc par ordre, avec annulation individuelle
# ==========================================================================

st.subheader("Ordres ouverts (Binance)")

symbol_filter = st.text_input("Filtrer par paire (optionnel)", "").strip().upper() or None
orders, error = service.open_orders(symbol_filter)

if error:
    st.info(error)

if not orders:
    st.caption("Aucun ordre ouvert côté Binance.")
else:
    st.caption(
        f"{len(orders)} ordre(s) ouvert(s) — chaque ligne dispose de sa propre "
        "annulation, avec confirmation."
    )

    for order in orders:
        owner_label = order.owner or ("hors bot" if not order.owned_by_bot else "inconnu")
        header = (
            f"{order.order_type} · {order.side} · {fmt_qty(order.orig_qty)} "
            f"{order.symbol} — {owner_label}"
        )

        with st.expander(header, expanded=True):
            cols = st.columns([3, 2])

            with cols[0]:
                st.markdown(
                    f"""
**Prix** : {fmt_price(order.price)} · **Stop** : {fmt_price(order.stop_price)}

**Quantité** : {fmt_qty(order.orig_qty)} · **Exécuté** : {fmt_qty(order.executed_qty)} ·
**Reste** : {fmt_qty(order.remaining_qty)}

**Statut** : {order.status} · **Origine** : {owner_label} · ordre `#{order.order_id}`
"""
                )

            with cols[1]:
                if owner_label == "SL":
                    st.warning("Annuler ce SL laisse la position sans protection.")
                elif owner_label.startswith("Entry"):
                    st.warning("Annuler cette Entry l'empêchera de s'exécuter.")

                confirmed = st.checkbox(
                    "Je confirme l'annulation",
                    key=f"confirm_cancel_{order.order_id}",
                )
                if st.button(
                    "🗑️ Annuler cet ordre",
                    key=f"cancel_order_{order.order_id}",
                    disabled=not confirmed,
                    width="stretch",
                ):
                    _cancel_order(order, confirmed)

st.divider()

# ==========================================================================
# Journal
# ==========================================================================

with st.expander("Journal récent"):
    events = service.recent_events(limit=40)
    if not events:
        st.caption("Aucun événement enregistré.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Date": e.get("ts", ""),
                        "Niveau": e.get("level", ""),
                        "Événement": e.get("event", ""),
                        "Symbole": e.get("symbol", ""),
                        "Message": e.get("message", ""),
                    }
                    for e in reversed(events)
                ]
            ),
            width="stretch",
            hide_index=True,
        )

    errors = service.recent_errors(limit=10)
    if errors:
        st.markdown("**Dernières erreurs**")
        for error in reversed(errors):
            st.error(f"{error.get('ts', '')} — {error.get('message', '')}")
