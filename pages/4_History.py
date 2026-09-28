"""History — positions terminées, filtres, analyses simples."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.config import get_settings  # noqa: E402
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

from ui_common import colored_pnl, pnl_metric, pnl_dataframe

settings = get_settings()
service = get_service()

st.set_page_config(page_title="History — BinanceSpotManager", page_icon="🗂️", layout="wide")
page_header("History", "Positions terminées et statistiques")
banner(settings)
sidebar_status(settings)

all_positions = service.positions.list_all()
closed = [p for p in all_positions if not p.is_open]

if not closed:
    st.info("Aucune position terminée pour le moment.")
    st.stop()

# ==========================================================================
# Filtres (section 86)
# ==========================================================================

st.subheader("Filtres")

col1, col2, col3, col4 = st.columns(4)

symbols = sorted({p.symbol for p in closed})
symbol_filter = col1.multiselect("Symbole", symbols)

sources = sorted(
    {g.source.value for p in closed for g in p.source_groups} or {"manual"}
)
source_filter = col2.multiselect("Source", sources)

results = {
    "Gagnantes": lambda p: p.pnl.realized > 0,
    "Perdantes": lambda p: p.pnl.realized <= 0,
}
result_filter = col3.multiselect("Résultat", list(results.keys()))

days = col4.slider("Fenêtre (jours)", 1, 365, 90)

filtered = []
for position in closed:
    age_days = (
        service.runtime().heartbeat_at - position.updated_at
    ).days if service.runtime().heartbeat_at else 0
    if (position.closed_at or position.updated_at).date() < (
        service.runtime().heartbeat_at.date() if service.runtime().heartbeat_at else position.updated_at.date()
    ) and days < 365:
        pass
    if symbol_filter and position.symbol not in symbol_filter:
        continue
    if source_filter and not any(g.source.value in source_filter for g in position.source_groups):
        continue
    if result_filter:
        if not any(results[r](position) for r in result_filter):
            continue
    filtered.append(position)

if not filtered:
    st.warning("Aucune position ne correspond aux filtres.")
    st.stop()

# ==========================================================================
# Tableau
# ==========================================================================

st.subheader(f"{len(filtered)} position(s)")

rows = []
for position in filtered:
    recompute_position(position)
    rows.append(
        {
            "Symbole": position.symbol,
            "Ouverte le": position.created_at.strftime("%d/%m/%Y"),
            "Fermée le": position.closed_at.strftime("%d/%m/%Y") if position.closed_at else "—",
            "Raison": position.close_reason.value if position.close_reason else "—",
            "Source": position.source_groups[0].source.value if position.source_groups else "—",
            "Prix moyen": fmt_price(position.metrics.average_price),
            "Quantité totale": fmt_qty(position.metrics.total_bought_qty),
            "Capital": fmt_price(position.metrics.capital_committed),
            "PnL réalisé": fmt_price(position.pnl.realized),
            "Frais": fmt_price(position.pnl.fees_paid),
            "TP atteints": f"{len(position.executed_tps)}/{len(position.take_profits)}",
            "Entries": len(position.entries),
            "SL final": fmt_price(position.stop_loss.resolved_price),
        }
    )

pnl_dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ==========================================================================
# Statistiques (preparation Analytics — section 87)
# ==========================================================================

st.subheader("Statistiques")

gains = [p.pnl.realized for p in filtered if p.pnl.realized > 0]
pertes = [p.pnl.realized for p in filtered if p.pnl.realized <= 0]
total = sum(p.pnl.realized for p in filtered)
fees = sum(p.pnl.fees_paid for p in filtered)

cols = st.columns(6)
cols[0].metric("Positions", len(filtered))
cols[1].metric(
    "Win rate",
    f"{len(gains) / len(filtered) * 100:.1f} %" if filtered else "—",
)
pnl_metric(cols[2], "PnL réalisé total", total)
pnl_metric(cols[3], "Gain moyen", sum(gains) / len(gains) if gains else None)
pnl_metric(cols[4], "Perte moyenne", sum(pertes) / len(pertes) if pertes else None)
cols[5].metric("Frais payés", fmt_price(fees))

if gains or pertes:
    profit_factor = (sum(gains) / abs(sum(pertes))) if pertes else float("inf")
    st.caption(
        f"Profit factor : {profit_factor:.2f}" if profit_factor != float("inf")
        else "Profit factor : ∞ (aucune perte enregistrée)"
    )

# ==========================================================================
# Performance par symbole / source
# ==========================================================================

col_a, col_b = st.columns(2)

with col_a:
    st.markdown("**Par symbole**")
    by_symbol: dict[str, list[float]] = {}
    for position in filtered:
        by_symbol.setdefault(position.symbol, []).append(position.pnl.realized)
    pnl_dataframe(
        pd.DataFrame(
            [
                {
                    "Symbole": symbol,
                    "Positions": len(values),
                    "PnL total": fmt_price(sum(values)),
                    "PnL moyen": fmt_price(sum(values) / len(values)),
                }
                for symbol, values in sorted(by_symbol.items())
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

with col_b:
    st.markdown("**Par source**")
    by_source: dict[str, list[float]] = {}
    for position in filtered:
        source = position.source_groups[0].source.value if position.source_groups else "manual"
        by_source.setdefault(source, []).append(position.pnl.realized)
    pnl_dataframe(
        pd.DataFrame(
            [
                {
                    "Source": source,
                    "Positions": len(values),
                    "PnL total": fmt_price(sum(values)),
                }
                for source, values in sorted(by_source.items())
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

# ==========================================================================
# Détail + duplication (section 50)
# ==========================================================================

st.subheader("Détail d'une position")

selected_label = st.selectbox(
    "Position",
    [
        f"{p.symbol} · {p.created_at.strftime('%d/%m/%Y')} · "
        f"{fmt_price(p.pnl.realized)}"
        for p in filtered
    ],
)
index = [
    f"{p.symbol} · {p.created_at.strftime('%d/%m/%Y')} · {fmt_price(p.pnl.realized)}"
    for p in filtered
].index(selected_label)
position = filtered[index]

with st.expander("Détail complet", expanded=True):
    st.markdown(
        f"""
**{position.symbol}** — {position.position_id}

Prix moyen : {fmt_price(position.metrics.average_price)} ·
Quantité achetée : {fmt_qty(position.metrics.total_bought_qty)} ·
Quantité vendue : {fmt_qty(position.metrics.total_sold_qty)}

Capital engagé : {fmt_price(position.metrics.capital_committed)} ·
Commissions {position.quote_asset} : {fmt_price(position.metrics.commissions_quote)} ·
Commissions {position.base_asset} : {fmt_qty(position.metrics.commissions_base, 8)}

PnL réalisé : {colored_pnl(position.pnl.realized)} ·
Break-even avec frais : {fmt_price(position.metrics.break_even_with_fees)}
"""
    )

    st.markdown("**Entries**")
    pnl_dataframe(
        pd.DataFrame(
            [
                {
                    "N°": e.sequence_number,
                    "Prix": fmt_price(e.average_fill_price or e.resolved_price),
                    "Exécutée": fmt_qty(e.executed_qty),
                    "Statut": e.status.value,
                }
                for e in position.sorted_entries
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("**Take Profits**")
    pnl_dataframe(
        pd.DataFrame(
            [
                {
                    "N°": t.sequence_number,
                    "Prix": fmt_price(t.target_price),
                    "Vente %": f"{t.sell_percent:.0f}",
                    "Statut": t.status.value,
                    "Gain réalisé": fmt_price(t.gain_realized),
                }
                for t in position.sorted_tps
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("**Événements**")
    pnl_dataframe(
        pd.DataFrame(
            [
                {
                    "Date": h.timestamp.strftime("%d/%m/%Y %H:%M:%S"),
                    "Événement": h.event_type.value,
                    "Message": h.message,
                }
                for h in position.history
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("**Dupliquer la stratégie**")
    st.caption(
        "Reprend la structure (Entries, répartitions, TP, règles SL) mais "
        "recalcule tous les prix et quantités avec les conditions actuelles. "
        "Ouvre New Trade : les champs doivent y être ressaisis."
    )
    if st.button("Préparer la duplication"):
        st.session_state["duplicate_from"] = {
            "symbol": position.symbol,
            "entries": [
                {"type": e.order_type.value, "offset": e.requested_offset_percent,
                 "alloc": e.capital_percent}
                for e in position.sorted_entries
            ],
            "take_profits": [
                {"percent": t.target_percent, "sell": t.sell_percent,
                 "rule": t.sl_rule_after_hit.value}
                for t in position.sorted_tps
            ],
            "sl": {"mode": position.stop_loss.mode.value, "value": position.stop_loss.value},
        }
        st.success(
            "Structure mémorisée. Ouvre **New Trade** : elle est proposée comme point de départ."
        )
