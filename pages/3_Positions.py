"""Positions — détail, ordres réels, réconciliation et édition du plan de sortie."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.config import get_settings  # noqa: E402
from binance_spot_manager.automation_engine import planned_tp_quantity  # noqa: E402
from binance_spot_manager.execution_engine import ExecutionEngine  # noqa: E402
from binance_spot_manager.models import (  # noqa: E402
    CloseReason,
    EntryStatus,
    EventType,
    SLRuleAfterTP,
    SLStatus,
    SLTrigger,
    TPExecutionPolicy,
    TPStatus,
    utcnow,
)
from binance_spot_manager.position_engine import (  # noqa: E402
    PositionEngine,
    finish_position,
    percent_change,
    recompute_position,
)
from binance_spot_manager.reconciliation_engine import audit_open_orders  # noqa: E402
from binance_spot_manager.reconciliation_engine import ReconciliationEngine
from ui_common import (  # noqa: E402
    banner,
    fmt_percent,
    fmt_price,
    fmt_qty,
    fmt_quote,
    get_service,
    page_header,
    sidebar_status,
    submit_to_worker, new_command_confirmation,
    colored_pnl, pnl_metric, pnl_dataframe, fresh_position_view,
)

from ui_common import require_login  # noqa: E402

# Connexion exigee avant tout affichage (comptes : scripts/creer_compte.py).
require_login()

settings = get_settings()
service = get_service()

st.set_page_config(page_title="Positions — BinanceSpotManager", page_icon="📌", layout="wide")
page_header("Positions", "Détail, ordres réels et plan de sortie")
banner(settings)
sidebar_status(settings)

positions = service.positions.list_all()

if not positions:
    st.info("Aucune position enregistrée. Crée-en une depuis **New Trade**.")
    st.stop()

status_groups = {
    "Pending": {"DRAFT", "PENDING_ENTRIES"},
    "Active": {"ACTIVE", "CLOSING"},
    "Closed": {"CLOSED", "CANCELED"},
}
status_filter = st.segmented_control(
    "Filtrer les positions", ["Toutes", "Pending", "Active", "Closed"],
    default="Toutes", key="position_status_filter",
    help="Pending : en attente d'achat (ou brouillon). Active : active ou en cours de clôture. "
         "Closed : clôturée ou annulée. Toutes inclut aussi les positions en erreur.",
)
if status_filter in status_groups:
    positions = [p for p in positions if p.status.value in status_groups[status_filter]]
st.caption(f"{len(positions)} position(s) correspondant au filtre")
if not positions:
    st.info("Aucune position pour ce statut. Sélectionne un autre filtre.")
    st.stop()

labels = {
    p.position_id: p
    for p in positions
}
choice = st.selectbox("Position", list(labels.keys()), format_func=lambda key:
    f"{labels[key].symbol} · {key} · {labels[key].status.value} · {labels[key].created_at.strftime('%d/%m %H:%M')}")
position = labels[choice]
new_command_confirmation(f"move_sl_{position.position_id}")

rules = service.rules_cache.get(position.symbol)
price = service.current_price(position.symbol)
if price:
    position.metrics.current_price = price
    recompute_position(position)

execution = ExecutionEngine(settings=settings, events=service.events)


# ==========================================================================
# Aides d'edition
# ==========================================================================


def _save(message: str) -> None:
    recompute_position(position)
    service.positions.save(position)
    st.success(message)
    st.cache_resource.clear()
    st.rerun()


def _reorder_tps() -> None:
    """Renumeroie les TP dans l'ordre croissant des prix cibles.

    Apres une suppression ou une modification, les sequences doivent rester
    coherentes : TP1 est toujours le plus proche, et `next_tp` doit renvoyer
    le bon niveau.
    """
    for index, tp in enumerate(
        sorted(position.take_profits, key=lambda t: t.target_price or 0.0),
        start=1,
    ):
        tp.sequence_number = index


# ==========================================================================
# En-tête
# ==========================================================================

@st.fragment(run_every="1s", key="position_summary")
def live_position_summary(position_id):
    position, price, returns = fresh_position_view(service, position_id)
    if position is None:
        st.info("Position introuvable dans le stockage local.")
        return
    st.caption(f"Actualisation automatique chaque seconde · lecture seule · dernière lecture {utcnow().strftime('%H:%M:%S')} UTC")
    st.subheader(f"{position.symbol}")
    head = st.columns(5)
    head[0].metric("État", position.status.value)
    head[1].metric("Sync", position.sync_status.value)
    head[2].metric("Prix actuel", fmt_price(price))
    head[3].metric("Prix moyen", fmt_price(position.metrics.average_price))
    head[4].metric("Quantité nette", fmt_qty(position.metrics.net_qty))

    head2 = st.columns(5)
    head2[0].metric("Capital engagé", fmt_price(position.metrics.capital_committed))
    head2[1].metric("Capital en attente", fmt_price(position.metrics.capital_pending))
    pnl_metric(head2[2], "PnL latent (USDT)", returns["unrealized_usdt"])
    pnl_metric(head2[3], "PnL réalisé (USDT)", returns["realized_usdt"])
    pnl_metric(head2[4], "PnL total (USDT / %)", returns["total_usdt"],
               f"{fmt_price(returns['total_usdt'])} USDT ({fmt_percent(returns['percent'])})")
    if not returns["complete"]:
        missing = ", ".join(returns["unpriced_fee_assets"]) or "inconnus"
        st.warning(f"PnL incomplet : frais non valorisables ({missing}) ou conversion USDT indisponible.")
    elif returns["estimated_fee_assets"]:
        assets = ", ".join(returns["estimated_fee_assets"])
        st.caption(f"Frais {assets} déduits avec le cours Binance actuel : PnL estimatif, conversion actualisée chaque seconde.")

    st.caption(
        f"Position {position.position_id} · source "
        f"{position.source_groups[0].source.value if position.source_groups else '—'} · "
        f"créée le {position.created_at.strftime('%d/%m/%Y %H:%M')} UTC · "
        f"environnement {position.environment}"
    )

    # ==========================================================================
    # Progression
    # ==========================================================================

    progress_cols = st.columns(4)
    next_tp = position.next_tp
    progress_cols[0].metric(
        "Prochain TP",
        f"TP{next_tp.sequence_number} @ {fmt_price(next_tp.target_price)}" if next_tp else "—",
    )
    progress_cols[1].metric("TP atteints", f"{len(position.executed_tps)}/{len(position.take_profits)}")
    progress_cols[2].metric(
        "SL", f"{fmt_price(position.stop_loss.resolved_price)} ("
              + (f"clôture {position.stop_loss.candle_interval}"
                 if position.stop_loss.trigger is SLTrigger.CANDLE_CLOSE else position.stop_loss.status.value)
              + ")"
    )
    progress_cols[3].metric(
        "Référence break-even",
        fmt_price(position.metrics.break_even_with_fees),
        help="Prix moyen majoré des commissions d'achat déjà payées (estimation).",
    )

    if position.take_profits:
        st.progress(len(position.executed_tps) / len(position.take_profits))


live_position_summary(position.position_id)

next_tp = position.next_tp

with st.expander("Tester le prochain TP sans attendre le prix cible"):
    st.caption(
        "Aperçu au prix cible simulé : aucun ordre Binance n'est envoyé et "
        "la position reste inchangée."
    )
    if next_tp is None:
        st.info("Aucun TP à tester sur cette position.")
    elif st.button("Simuler la vente du prochain TP", key=f"preview_tp_{position.position_id}"):
        quantity = planned_tp_quantity(position, next_tp, rules)
        target_price = float(next_tp.target_price or 0.0)
        market = next_tp.execution_policy is TPExecutionPolicy.MARKET_ON_TRIGGER
        errors = rules.check_qty(quantity, market=market)
        if target_price <= 0:
            errors.append("prix cible invalide")
        else:
            errors.extend(rules.check_notional(target_price, quantity))

        st.write(
            f"TP {next_tp.sequence_number} · cible simulée : "
            f"{fmt_price(target_price)} {position.quote_asset}"
        )
        st.write(
            f"Quantité nette locale : "
            f"{fmt_qty(position.metrics.net_qty)} {position.base_asset}"
        )
        st.write(f"Quantité arrondie à vendre : {fmt_qty(quantity)} {position.base_asset}")
        st.write(f"Notionnel estimé : {fmt_quote(quantity * target_price, position.quote_asset)}")
        if position.stop_loss.status is SLStatus.ACTIVE:
            st.info(
                "Le worker annulerait le SL pour libérer le solde, "
                "puis le restaurerait si nécessaire."
            )
        if errors:
            for error in errors:
                st.error(error)
        else:
            st.success("Quantité et filtres Binance cohérents pour ce scénario.")
        st.caption(
            "L'acceptation et le prix d'exécution réels restent à vérifier "
            "lorsque le TP sera déclenché sur Binance Demo."
        )

# ==========================================================================
# Entries
# ==========================================================================

@st.fragment(run_every="1s")
def live_position_entries(position_id):
    position = service.positions.load(position_id)
    if position is None:
        return
    st.subheader("Entries")
    pnl_dataframe(
        pd.DataFrame(
            [
                {
                    "N°": e.sequence_number,
                    "Type": e.order_type.value,
                    "Prix résolu": fmt_price(e.resolved_price),
                    "Statut": e.status.value,
                    "Qté demandée": fmt_qty(e.requested_qty),
                    "Qté Binance": fmt_qty(e.binance_qty),
                    "Exécutée": fmt_qty(e.executed_qty),
                    "Prix remplissage": fmt_price(e.average_fill_price),
                    "Dépensé": fmt_quote(e.quote_spent, position.quote_asset),
                    "Capital %": f"{e.capital_percent:.1f}",
                    "orderId": e.order_id or "—",
                    "clientOrderId": e.client_order_id or "—",
                }
                for e in position.sorted_entries
            ]
        ),
        width="stretch",
        hide_index=True,
    )


live_position_entries(position.position_id)

# ==========================================================================
# Plan de sortie — Take Profits
# ==========================================================================

st.subheader("Plan de sortie — Take Profits")


@st.fragment(run_every="1s")
def live_take_profit_status(position_id):
    latest = service.positions.load(position_id)
    if latest is None or not latest.take_profits:
        return
    pnl_dataframe([{"TP": tp.sequence_number, "État": tp.status.value,
                    "Cible": fmt_price(tp.target_price), "Vente %": tp.sell_percent,
                    "Quantité exécutée": tp.executed_qty,
                    "Gain réalisé": fmt_quote(tp.gain_realized, latest.quote_asset),
                    "Ordre": tp.order_id}
                   for tp in latest.sorted_tps], hide_index=True, width="stretch",
                  key=f"live_tp_status_{position_id}")


live_take_profit_status(position.position_id)
st.caption("Les états ci-dessus s'actualisent chaque seconde. Les champs d'édition restent inchangés pendant la saisie.")

pending_tps = [tp for tp in position.sorted_tps if tp.status is TPStatus.PENDING]
executed_tps = [tp for tp in position.sorted_tps if tp.status is not TPStatus.PENDING]

if not position.take_profits:
    st.info("Aucun TP défini sur cette position.")

mode = st.radio(
    "Mode d'édition",
    ["Lot de TP", "TP par TP"],
    horizontal=True,
    help="Modifier un ou plusieurs TP en une fois, ou modifier un TP isolé.",
)

# ---------------------------------------------------------------- lot complet
if mode == "Lot de TP" and pending_tps:
    st.caption(
        "Modifie les prix et les pourcentages de vente. Le total des ventes est "
        "affiché pour vérifier qu'il atteint 100 % — sinon une partie de la "
        "position resterait sans plan de sortie."
    )

    with st.form("tp_bulk_form"):
        edited: list[dict] = []
        for tp in pending_tps:
            cols = st.columns([1, 2, 2, 1])
            cols[0].markdown(f"**TP {tp.sequence_number}**")
            new_price = cols[1].number_input(
                "Prix",
                min_value=0.0,
                value=float(tp.target_price or 0.0),
                step=float(rules.tick_size),
                format="%.2f",
                key=f"bulk_price_{tp.tp_id}",
            )
            new_sell = cols[2].number_input(
                "Vente %",
                min_value=0.0,
                max_value=100.0,
                value=float(tp.sell_percent),
                step=1.0,
                key=f"bulk_sell_{tp.tp_id}",
            )
            cols[3].caption(f"actuel\n{fmt_price(tp.target_price)}")
            edited.append({"tp": tp, "price": new_price, "sell": new_sell})

        total_sell = sum(row["sell"] for row in edited)
        st.markdown(f"**Total des ventes : {total_sell:.2f} %**")

        submitted = st.form_submit_button("Enregistrer tous les TP")

    if submitted:
        errors: list[str] = []

        average = position.metrics.average_price
        prices = []

        for row in edited:
            tp = row["tp"]
            new_price = float(rules.round_price(row["price"], mode="down"))

            if new_price <= 0:
                errors.append(f"TP {tp.sequence_number} : prix nul")
                continue
            if average and new_price <= average:
                errors.append(
                    f"TP {tp.sequence_number} : prix ({fmt_price(new_price)}) "
                    f"sous le prix moyen ({fmt_price(average)})"
                )
                continue

            tp.target_price = new_price
            tp.target_percent = percent_change(new_price, average) if average else tp.target_percent
            tp.sell_percent = row["sell"]
            prices.append((tp.sequence_number, new_price))

        # Verifie que les TP restent ordonnes.
        ordered = sorted(prices, key=lambda item: item[1])
        if [seq for seq, _ in ordered] != sorted(seq for seq, _ in prices):
            errors.append("Les TP ne sont pas dans l'ordre croissant des prix")

        if errors:
            for error in errors:
                st.error(error)
        else:
            _reorder_tps()
            for tp in pending_tps:
                tp.estimated_qty = position.metrics.net_qty * tp.sell_percent / 100.0

            position.log(
                EventType.POSITION_UPDATED,
                f"Plan de sortie modifié ({len(edited)} TP) depuis Positions",
                total_sell=total_sell,
            )
            if total_sell < 99.99:
                position.log(
                    EventType.POSITION_UPDATED,
                    f"Attention : les TP ne vendent que {total_sell:.2f} % "
                    "de la position",
                )
            _save(f"Plan de sortie enregistré ({len(edited)} TP)")

# ------------------------------------------------------------- TP par TP
if mode == "TP par TP":
    if not position.take_profits:
        st.caption("Aucun TP à modifier.")

    for tp in position.sorted_tps:
        is_pending = tp.status is TPStatus.PENDING
        title = (
            f"TP {tp.sequence_number} · {fmt_percent(tp.target_percent)} · "
            f"vente {tp.sell_percent:.0f} % · {tp.status.value}"
        )

        with st.expander(title, expanded=is_pending and len(pending_tps) <= 2):
            if not is_pending:
                st.info(
                    f"Ce TP est en statut {tp.status.value} — il n'est plus "
                    "modifiable. Une vente déclenchée ne se réécrit pas."
                )
                st.caption(
                    f"Prix cible {fmt_price(tp.target_price)} · "
                    f"vendue {fmt_qty(tp.executed_qty)} · "
                    f"gain réalisé {colored_pnl(tp.gain_realized)}"
                )
                continue

            cols = st.columns(3)
            new_price = cols[0].number_input(
                "Prix cible",
                min_value=0.0,
                value=float(tp.target_price or 0.0),
                step=float(rules.tick_size),
                format="%.2f",
                key=f"single_price_{tp.tp_id}",
            )
            new_sell = cols[1].number_input(
                "Vente (%)",
                min_value=0.0,
                max_value=100.0,
                value=float(tp.sell_percent),
                step=1.0,
                key=f"single_sell_{tp.tp_id}",
            )
            pnl_metric(cols[2], "Gain estimé", tp.gain_estimated)

            confirm_save = st.checkbox(
                "Confirmer la modification", key=f"confirm_save_{tp.tp_id}"
            )
            buttons = st.columns(2)

            if buttons[0].button(
                "💾 Enregistrer",
                key=f"save_tp_{tp.tp_id}",
                disabled=not confirm_save,
                width="stretch",
            ):
                target = float(rules.round_price(new_price, mode="down"))
                average = position.metrics.average_price

                if target <= 0:
                    st.error("Prix cible invalide.")
                elif average and target <= average:
                    st.error(
                        f"Le prix cible doit rester au-dessus du prix moyen "
                        f"({fmt_price(average)})."
                    )
                else:
                    tp.target_price = target
                    tp.target_percent = percent_change(target, average) if average else None
                    tp.sell_percent = new_sell
                    tp.estimated_qty = position.metrics.net_qty * new_sell / 100.0
                    position.log(
                        EventType.POSITION_UPDATED,
                        f"TP {tp.sequence_number} modifié : {fmt_price(target)} · "
                        f"vente {new_sell:.0f} %",
                        tp_id=tp.tp_id,
                    )
                    _reorder_tps()
                    _save(f"TP {tp.sequence_number} enregistré")

            if buttons[1].button(
                "🗑️ Supprimer",
                key=f"del_tp_{tp.tp_id}",
                disabled=not confirm_save,
                width="stretch",
            ):
                tp.status = TPStatus.CANCELED
                tp.last_error = "Supprimé manuellement depuis le Dashboard"
                position.log(
                    EventType.POSITION_UPDATED,
                    f"TP {tp.sequence_number} supprimé manuellement",
                    tp_id=tp.tp_id,
                )

                total_after = sum(
                    t.sell_percent
                    for t in position.take_profits
                    if t.status is TPStatus.PENDING
                )
                position.log(
                    EventType.POSITION_UPDATED,
                    f"Attention : les TP restants ne vendent que {total_after:.2f} % "
                    "de la position",
                )
                _reorder_tps()
                _save(f"TP {tp.sequence_number} supprimé")

# ==========================================================================
# Stop Loss
# ==========================================================================

st.subheader("Stop Loss")

sl = position.stop_loss
@st.fragment(run_every="1s")
def live_stop_loss(position_id):
    position = service.positions.load(position_id)
    if position is None:
        return
    sl = position.stop_loss
    if sl.trigger is SLTrigger.CANDLE_CLOSE:
        st.info(
            f"SL à la clôture de bougie {sl.candle_interval} : aucun ordre stop sur Binance. "
            f"Le worker vend au marché si une bougie {sl.candle_interval} clôture à "
            f"{fmt_price(sl.resolved_price)} ou dessous ; il doit rester actif pour protéger la position."
        )
    sl_cols = st.columns(5)
    sl_cols[0].metric("Mode", f"Clôture {sl.candle_interval}" if sl.trigger is SLTrigger.CANDLE_CLOSE else sl.mode.value)
    sl_cols[1].metric("Valeur", sl.value)
    sl_cols[2].metric("Prix résolu", fmt_price(sl.resolved_price))
    sl_cols[3].metric("Quantité protégée", fmt_qty(sl.quantity))
    sl_cols[4].metric("Statut", sl.status.value)

    st.caption(
        f"Remplacements : {sl.replace_count} · orderId {sl.order_id or '—'} · "
        f"clientOrderId {sl.client_order_id or '—'} · décalage limite "
        f"{sl.limit_offset_percent} %"
    )
    if sl.last_error:
        st.error(sl.last_error)


live_stop_loss(position.position_id)

if position.is_open and position.metrics.net_qty > 0 and sl.status is not SLStatus.NONE:
    with st.expander("Modifier / supprimer le SL", expanded=False):
        st.warning(
            "Déplacer le SL annule l'ordre existant puis en crée un nouveau. "
            "Supprimer le SL laisse la position **sans aucune protection** — "
            "le worker le recréera au cycle suivant, mais il y aura une fenêtre "
            "sans stop."
        )

        edit_cols = st.columns(2)

        new_sl_price = edit_cols[0].number_input(
            "Nouveau prix de SL",
            min_value=0.0,
            value=float(sl.resolved_price or 0.0),
            step=float(rules.tick_size),
            format="%.2f",
            key="sl_new_price",
        )
        rule_options = [rule.value for rule in SLRuleAfterTP]
        manual_rule = edit_cols[1].selectbox(
            "Ou appliquer une règle", ["—"] + rule_options, key="sl_new_rule"
        )

        confirm_sl = st.checkbox(
            "Je confirme l'action sur le SL unique", key="sl_confirm"
        )

        sl_buttons = st.columns(2)

        if sl_buttons[0].button(
            "💾 Déplacer le SL",
            key="sl_move",
            disabled=not confirm_sl,
            width="stretch",
        ):
            engine = PositionEngine(rules)
            target = float(new_sl_price)
            if manual_rule != "—":
                resolved = engine.compute_sl_rule_price(position, manual_rule)
                if resolved:
                    target = float(rules.round_price(resolved, mode="down"))

            submit_to_worker(
                "MOVE_SL",
                {"position_id": position.position_id, "target_price": target,
                 "expected_order_id": sl.order_id},
                confirmation_key=f"move_sl_{position.position_id}",
            )

        if sl_buttons[1].button(
            "🗑️ Supprimer le SL",
            key="sl_delete",
            disabled=not confirm_sl,
            width="stretch",
        ):
            if sl.order_id:
                submit_to_worker(
                    "CANCEL_ORDER", {"symbol": position.symbol, "order_id": sl.order_id},
                    confirmation_key=f"delete_sl_{position.position_id}_{sl.order_id}",
                )
            else:
                st.warning("Aucun ordre identifie : verifier les intentions dans Operations.")
else:
    st.caption("Aucun SL configuré, ou position sans quantité ouverte.")

# ==========================================================================
# Ordres réels + réconciliation
# ==========================================================================

st.subheader("Ordres ouverts réels (Binance)")

orders, error = service.open_orders(position.symbol)
if error:
    st.info(error)

if orders:
    pnl_dataframe(
        pd.DataFrame(
            [
                {
                    "orderId": o.order_id,
                    "clientOrderId": o.client_order_id,
                    "Sens": o.side,
                    "Type": o.order_type,
                    "Prix": fmt_price(o.price),
                    "Stop": fmt_price(o.stop_price),
                    "Quantité": fmt_qty(o.orig_qty),
                    "Exécuté": fmt_qty(o.executed_qty),
                    "Statut": o.status,
                    "Origine": o.owner or "hors bot",
                }
                for o in orders
            ]
        ),
        width="stretch",
        hide_index=True,
    )
else:
    st.caption("Aucun ordre ouvert côté Binance pour cette paire.")

raw_orders = [
    {
        "orderId": o.order_id,
        "clientOrderId": o.client_order_id,
        "side": o.side,
        "type": o.order_type,
        "origQty": o.orig_qty,
        "price": o.price,
    }
    for o in orders
]
for finding in audit_open_orders(position, raw_orders, tracked_positions=service.positions.list_open()):
    st.warning(f"⚠️ {finding.message} — {finding.suggested_action}")

st.markdown("**Réconcilier avec Binance**")
if st.button("Lancer une réconciliation"):
    with st.spinner("Comparaison de l'état local et de l'état Binance..."):
        report = ReconciliationEngine(execution, events=service.events).reconcile(position, apply=False)

    if not report.has_desync:
        st.success("✅ État local et Binance synchronisés.")
    else:
        st.warning(f"{len(report.findings)} écart(s) détecté(s) :")
        for finding in report.findings:
            level = {"CRITICAL": st.error, "WARNING": st.warning}.get(
                finding.severity, st.info
            )
            suffix = " (appliqué automatiquement)" if finding.auto_applied else ""
            level(f"[{finding.kind}] {finding.message}{suffix}")
            if finding.suggested_action:
                st.caption(f"Action suggérée : {finding.suggested_action}")

# ==========================================================================
# Automatisation
# ==========================================================================

st.subheader("Automatisation")
auto_cols = st.columns(2)
paused = auto_cols[0].toggle("Mettre en pause", value=position.automation.paused)
cancel_on_tp1 = auto_cols[1].toggle(
    "Annuler les Entries restantes au TP1",
    value=position.automation.cancel_remaining_entries_on_first_tp,
)

if (
    paused != position.automation.paused
    or cancel_on_tp1 != position.automation.cancel_remaining_entries_on_first_tp
):
    position.automation.paused = paused
    position.automation.cancel_remaining_entries_on_first_tp = cancel_on_tp1
    position.log(
        EventType.POSITION_UPDATED,
        f"Automatisation {'mise en pause' if paused else 'réactivée'}",
    )
    service.positions.save(position)
    st.success("Préférence enregistrée")

# ==========================================================================
# Historique
# ==========================================================================

@st.fragment(run_every="1s")
def live_position_history(position_id):
    position = service.positions.load(position_id)
    if position is None:
        return
    st.subheader("Historique")
    if position.history:
        pnl_dataframe(
            pd.DataFrame(
                [
                    {
                        "Date": h.timestamp.strftime("%d/%m/%Y %H:%M:%S"),
                        "Événement": h.event_type.value,
                        "Message": h.message,
                    }
                    for h in reversed(position.history)
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("Aucun événement enregistré pour cette position.")


live_position_history(position.position_id)

# ==========================================================================
# Fermeture manuelle
# ==========================================================================

@st.fragment(run_every="1s")
def live_market_close(position_id):
    position, price, returns = fresh_position_view(service, position_id)
    if position is None or not position.is_open:
        st.info("Position clôturée ; aucun nouvel ordre ne sera envoyé.")
        return
    with st.expander("Clôturer la position au marché", expanded=False):
        st.warning("Annule uniquement les achats en attente, TP et SL de cette position, puis vend son solde net au marché. Le prix final dépend de l'exécution Binance Demo.")
        st.markdown("Résultat estimé avant vente : " + colored_pnl(returns["total_usdt"],
                    f"{fmt_price(returns['total_usdt'])} USDT ({fmt_percent(returns['percent'])})"))
        st.caption("Les frais de la vente et le glissement de prix peuvent modifier ce résultat. Les poussières non vendables restent dans le portefeuille.")
        new_command_confirmation(f"market_close_{position.position_id}")
        confirm_market = st.checkbox("Je confirme l'annulation des ordres et la vente au marché", key=f"confirm_market_{position.position_id}")
        if st.button("Annuler les TP/SL et vendre au marché", type="primary",
                     disabled=not confirm_market or any(s.status not in {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"} for s in position.manual_exits)):
            submit_to_worker("CLOSE_MARKET", {"position_id": position.position_id},
                             confirmation_key=f"market_close_{position.position_id}")
        if position.status.value == "CLOSING":
            st.info("Clôture en cours ou suspendue : le worker vérifie la vente sans la renvoyer. Consulte Operations en cas d'erreur.")


if position.is_open:
    st.divider()
    live_market_close(position.position_id)
    with st.expander("Fermer la position (manuel)", expanded=False):
        st.warning(
            "Cette action clôture la position localement et annule les ordres "
            "suivis. Elle n'envoie **aucune vente** : les sorties déjà exécutées "
            "côté Binance doivent être réconciliées d'abord."
        )
        confirm_close = st.checkbox("Je confirme la fermeture locale")
        if st.button("Fermer la position", disabled=not confirm_close):
            submit_to_worker(
                "CLOSE_LOCAL", {"position_id": position.position_id},
                confirmation_key=f"close_{position.position_id}",
            )

@st.fragment(run_every="1s")
def live_close_result(position_id):
    position = service.positions.load(position_id)
    if position is None:
        return
    if position.manual_exits:
        st.subheader("Ventes de clôture")
        pnl_dataframe([{"Ordre": sale.order_id, "État": sale.status,
                        "Quantité vendue": sale.executed_qty, "Prix exécuté": sale.average_fill_price,
                        f"Reçu ({position.quote_asset})": sale.quote_received}
                       for sale in position.manual_exits], hide_index=True, width="stretch")

    if position.status.value == "CLOSED":
        st.divider()
        st.info(
            f"Position terminée le "
            f"{position.closed_at.strftime('%d/%m/%Y %H:%M') if position.closed_at else '—'} — "
            f"raison {position.close_reason.value if position.close_reason else '—'} — "
            f"PnL réalisé {fmt_quote(position.pnl.realized, position.quote_asset)}"
        )


live_close_result(position.position_id)
