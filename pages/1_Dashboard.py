"""Dashboard — vue globale, worker, portefeuille, positions, ordres réels."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.config import get_settings  # noqa: E402
from binance_spot_manager.models import EntryStatus, EventType, SLStatus, SyncStatus, TPStatus  # noqa: E402
from binance_spot_manager.oco_preview import preview_oco_sell  # noqa: E402
from binance_spot_manager.position_engine import recompute_position  # noqa: E402
from binance_spot_manager.protection_status import protection_alerts  # noqa: E402
from ui_common import (  # noqa: E402
    banner,
    fmt_percent,
    fmt_price,
    fmt_qty,
    fmt_quote,
    get_service,
    page_header,
    sidebar_status,
    submit_to_worker,
)

from ui_common import colored_pnl, pnl_metric, pnl_dataframe

from ui_common import require_login  # noqa: E402

# Connexion exigee avant tout affichage (comptes : scripts/creer_compte.py).
require_login()

settings = get_settings()
service = get_service()

st.set_page_config(page_title="Dashboard — BinanceSpotManager", page_icon="📊", layout="wide")
page_header("Dashboard", "Worker, portefeuille et positions en temps réel")
banner(settings)
sidebar_status(settings)


# ==========================================================================
# Aides d'annulation
# ==========================================================================


def _cancel_order(order, confirmed: bool) -> None:
    if not confirmed:
        st.warning("Confirme l'annulation avant envoi.")
        return
    submit_to_worker(
        "CANCEL_ORDER", {"symbol": order.symbol, "order_id": order.order_id},
        confirmation_key=f"cancel_{order.order_id}",
    )


# ==========================================================================
# Worker
# ==========================================================================

st.subheader("Worker")

@st.fragment(run_every="1s")
def worker_panel() -> None:
    """Actualise l'etat du worker sans exiger un second clic sur Arreter."""
    status = service.worker_status()
    docker = service.process_manager.docker
    pending = bool(st.session_state.get("worker_stop_pending"))
    if pending and not status.running:
        st.session_state.worker_stop_pending = False
        st.session_state.worker_flash = (
            ("success", "Worker en veille : aucun suivi ni ordre." if docker else "Worker arrêté proprement.")
            if status.state in ("STOPPED", "PAUSED")
            else ("error", "Worker terminé sans confirmation d'arrêt propre : vérifie le journal.")
        )
        st.rerun(scope="app")  # actualise aussi l'etat dans la barre laterale

    cols = st.columns(3)
    cols[0].metric("État", status.label)
    cols[1].metric("PID", "Docker" if docker else status.pid or "—")
    cols[2].metric("Positions suivies", status.positions_monitored)

    if status.last_message:
        st.caption(f"Dernier message : {status.last_message}")
    if status.last_error:
        st.error(f"Dernière erreur : {status.last_error}")
    if docker and not status.pid_alive:
        st.warning("Aucun heartbeat du conteneur worker : lancer `make worker-start`, puis `make logs`.")
    elif status.is_stale:
        st.warning(
            "Le worker ne répond plus. Il est peut-être bloqué : "
            + ("lancer `make worker-restart`." if docker else "utiliser l'arrêt forcé ci-dessous, puis relancer.")
        )
    flash = st.session_state.get("worker_flash")
    if flash:
        (st.success if flash[0] == "success" else st.error)(flash[1])
    if pending:
        elapsed = time.monotonic() - st.session_state.get("worker_stop_requested_at", time.monotonic())
        if elapsed >= 30:
            st.warning("Arrêt demandé depuis plus de 30 s. Vérifie le worker avant un arrêt forcé.")
        else:
            st.info("Arrêt en cours : le worker termine sa boucle. La page se met à jour automatiquement.")

    actions = st.columns(5)
    if actions[0].button("▶️ Démarrer le worker", width="stretch", disabled=status.running or pending):
        ok, message = service.process_manager.start()
        st.session_state.worker_flash = ("success" if ok else "error", message)
        st.rerun(scope="app")

    if actions[1].button("⏹️ Arrêter proprement", width="stretch", disabled=not status.running or pending):
        ok, message = service.process_manager.request_stop()
        if ok:
            st.session_state.worker_stop_pending = True
            st.session_state.worker_stop_requested_at = time.monotonic()
            st.session_state.pop("worker_flash", None)
            st.info(message)
        else:
            st.session_state.worker_flash = ("error", message)

    if actions[2].button("🛑 Arrêt forcé", width="stretch", disabled=docker or not status.running):
        ok, message = service.process_manager.stop(force=True, timeout_seconds=5)
        st.session_state.worker_stop_pending = False
        st.session_state.worker_flash = ("success" if ok else "error", message)
        st.rerun(scope="app")

    if actions[3].button("🧹 Nettoyer l'état", width="stretch", disabled=docker):
        st.session_state.worker_flash = ("success", service.process_manager.clear_orphan_state())
        st.rerun(scope="app")

    if actions[4].button("🔄 Rafraîchir", width="stretch"):
        st.cache_resource.clear()
        st.rerun(scope="app")
    if docker:
        st.caption(
            "Worker géré par Docker : « Arrêter proprement » le met en veille, « Démarrer » le relance. "
            "Conteneur : `make worker-restart`, `make logs`."
        )


worker_panel()

@st.fragment(run_every="1s")
def protection_alert_panel() -> None:
    st.subheader("Points de vigilance — protections")
    st.caption("État local actualisé chaque seconde, sans appel Binance. Pour vérifier les ordres réels : Settings → Diagnostic → Comparer les sorties.")
    positions = service.positions.list_open()
    storage_errors = service.positions.read_errors
    for message in storage_errors:
        st.error(f"Position locale non lisible — {message}. Son suivi ne peut pas être confirmé ; vérifier les ordres chez Binance Demo.")
    if settings.dry_run:
        st.info("DRY_RUN : aucune protection réelle n'est créée chez Binance.")
        return
    alerts = protection_alerts(positions)
    for alert in alerts:
        display = st.error if alert["severity"] == "CRITICAL" else st.warning
        display(f"{alert['symbol']} · {alert['position_id']} — {alert['message']}")
    if not alerts and not storage_errors:
        st.caption("Aucune anomalie relevée par ces contrôles locaux. Cela ne garantit pas l'exécution future des TP/SL.")


protection_alert_panel()

st.divider()

# ==========================================================================
# Portefeuille
# ==========================================================================

st.subheader("Portefeuille")

@st.fragment(run_every="1s")
def live_portfolio_summary():
    view = service.portfolio()
    if view.unpriced_fee_assets:
        st.warning("Frais non convertis : " + ", ".join(sorted(view.unpriced_fee_assets))
                   + ". Voir la comptabilité détaillée dans Operations.")
    elif view.estimated_fee_assets:
        st.caption("Frais " + ", ".join(sorted(view.estimated_fee_assets))
                   + " déduits au cours Binance actuel.")
    if view.errors:
        for error in view.errors:
            st.warning(error)
    st.caption(f"Source des soldes : {view.source}")

    cols = st.columns(6)
    cols[0].metric(f"{settings.quote_asset} libre", fmt_price(view.quote_free))
    cols[1].metric("Capital engagé", fmt_price(view.capital_committed))
    cols[2].metric("Capital en attente", fmt_price(view.capital_pending))
    pnl_metric(cols[3], "PnL latent", view.unrealized_pnl)
    pnl_metric(cols[4], "PnL réalisé", view.realized_pnl)
    pnl_metric(cols[5], "PnL total", view.total_pnl)

    cols = st.columns(4)
    cols[0].metric("Risque total", fmt_price(view.total_risk_quote), f"{view.total_risk_percent:.2f} %")
    cols[1].metric("Exposition", f"{view.exposure_percent:.1f} %")
    cols[2].metric("Réserve estimée", fmt_price(view.capital_reserved))
    cols[3].metric("Positions ouvertes", view.open_positions)


live_portfolio_summary()

@st.fragment(run_every="1s")
def live_wallet():
    st.markdown("**Tous les actifs Spot Demo**")
    try:
        wallet = service.wallet_valuation()
    except Exception as exc:  # lecture uniquement ; ne masque pas le reste du Dashboard
        st.warning(f"Valorisation du portefeuille indisponible : {exc}")
    else:
        st.caption(
            f"Soldes libres + bloqués · prix indicatifs Binance Demo · "
            f"{wallet.valued_at.strftime('%d/%m/%Y %H:%M:%S')} UTC"
        )
        totals = st.columns(2)
        totals[0].metric(
            "Total connu en USDT" if wallet.unpriced_usdt else "Total en USDT",
            f"{wallet.total_usdt:,.2f}",
        )
        totals[1].metric(
            "Total connu en EUR" if wallet.unpriced_eur else "Total en EUR",
            f"{wallet.total_eur:,.2f} €",
        )
        if wallet.unpriced_usdt or wallet.unpriced_eur:
            st.warning(
                "Actifs sans taux Demo : "
                + ", ".join(sorted(set(wallet.unpriced_usdt + wallet.unpriced_eur)))
                + ". Ils sont affichés, mais exclus des totaux correspondants."
            )
        if wallet.assets:
            pnl_dataframe(
                pd.DataFrame([
                    {
                        "Actif": row.asset,
                        "Libre": fmt_qty(row.free),
                        "Bloqué": fmt_qty(row.locked),
                        "Total": fmt_qty(row.total),
                        "Cours USDT": fmt_price(row.price_usdt) if row.price_usdt is not None else "—",
                        "Cours EUR": fmt_price(row.price_eur) if row.price_eur is not None else "—",
                        "Valeur USDT": f"{row.value_usdt:,.2f}" if row.value_usdt is not None else "—",
                        "Valeur EUR": f"{row.value_eur:,.2f}" if row.value_eur is not None else "—",
                        "Part": (
                            f"{row.value_usdt / wallet.total_usdt * 100:.1f} %"
                            if row.value_usdt is not None and wallet.total_usdt > 0 else "—"
                        ),
                    }
                    for row in wallet.assets
                ]),
                width="stretch", hide_index=True,
            )
        else:
            st.info("Aucun actif Spot non nul dans ce compte Demo.")


live_wallet()

st.divider()

# ==========================================================================
# Positions
# ==========================================================================

st.subheader("Positions ouvertes")

@st.fragment(run_every="1s")
def live_open_positions():
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
                    "Position": r.position_id,
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
        pnl_dataframe(table, width="stretch", hide_index=True)

        for position in service.positions.list_open():
            if position.oco_exit is not None:
                oco = position.oco_exit
                st.caption(
                    f"OCO Demo {position.symbol} · {oco.status} · "
                    f"liste #{oco.order_list_id} · TP #{oco.tp_order_id} · SL #{oco.sl_order_id}"
                )

        desync = [r for r in rows if r.has_desync]
        if desync:
            st.warning(
                "⚠️ Désynchronisation détectée sur : "
                + ", ".join(f"{r.symbol} ({r.sync_status})" for r in desync)
                + " — voir la page Positions pour le détail."
            )


live_open_positions()

if service.positions.list_open():
    with st.expander("Prototype OCO Demo — aperçu uniquement"):
        st.caption(
            "Aucun ordre n'est envoyé. Le worker garde sa stratégie TP/SL actuelle. "
            "Une position avec SL indépendant actif ne peut pas être convertie ici."
        )
        selected = st.selectbox(
            "Position à examiner",
            options=service.positions.list_open(),
            format_func=lambda p: f"{p.symbol} · {p.position_id}",
            key="oco_preview_position",
        )
        if selected and st.button("Calculer l'aperçu OCO", key="oco_preview_button"):
            try:
                rules = service.rules_cache.get(selected.symbol)
                price = selected.metrics.current_price
                if price <= 0:
                    st.warning("Prix actuel indisponible : aperçu impossible.")
                else:
                    preview = preview_oco_sell(selected, rules, price)
                    st.write(
                        f"Vente {preview.quantity} {selected.base_asset} · "
                        f"TP limite {preview.take_profit_price} · "
                        f"SL {preview.stop_price} / limite {preview.stop_limit_price}"
                    )
                    if preview.blockers:
                        for blocker in preview.blockers:
                            st.warning(blocker)
                    else:
                        st.success("Paramètres cohérents pour une expérimentation Demo séparée.")
                    st.info("Aucune vérification du solde libre ni création d'ordre à cette étape.")
            except Exception as exc:  # noqa: BLE001 — aperçu non critique
                st.error(f"Aperçu OCO indisponible : {exc}")

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
        pnl_dataframe(
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
