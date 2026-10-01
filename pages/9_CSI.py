"""CSI : vérifier un signal, voir les signaux trouvés par CSI, les tester à la main en Demo."""
from pathlib import Path
import sys

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager import csi_client  # noqa: E402
from binance_spot_manager.command_store import account_scope  # noqa: E402
from binance_spot_manager.config import get_settings  # noqa: E402
from binance_spot_manager.csi_client import CsiUnavailable  # noqa: E402
from binance_spot_manager.signal_inbox import SignalInbox  # noqa: E402
from ui_common import banner, page_header, sidebar_status  # noqa: E402

st.set_page_config(page_title="CSI — Binance Demo", page_icon=":material/psychology:", layout="wide")
page_header("CSI", "Le cerveau : il vérifie tes signaux et en cherche lui-même. Il ne passe jamais d'ordre.")
settings = get_settings()
banner(settings)
sidebar_status(settings)

client = csi_client.CsiClient.from_env()
health, failure = client.probe()

# ---------------------------------------------------------------------------------------------
# État, en un voyant
if health is None:
    st.error(f"🔴 CSI ne répond pas — {failure}")
    st.caption("Démarrer CSI : dans son dossier (CryptoSignalIntelligence), `docker compose up -d`.")
    st.stop()
if health.get("ready"):
    st.success("🟢 CSI fonctionne : il analyse les marchés toutes les 15 minutes.")
else:
    st.warning(f"🟡 CSI démarre ou s'est arrêté un moment (PC en veille ?) — {health.get('detail', '')}")

BOXES = {"REFUSE": st.error, "DEFAVORABLE": st.error, "INDETERMINE": st.warning, "FAVORABLE": st.success,
         "EN_ATTENTE": st.info}
IN_SHORT = {
    "REFUSE": "Ne pas prendre : le signal est invalide ou déjà dépassé.",
    "DEFAVORABLE": "Déconseillé : ce type de trade a perdu de l'argent en moyenne dans le passé.",
    "INDETERMINE": "Pas d'avis clair : le passé ne permet pas de dire si c'est mieux que le hasard.",
    "FAVORABLE": "Plutôt bon : ce type de trade a gagné en moyenne dans le passé (sans garantie).",
    "EN_ATTENTE": "Paire nouvelle : CSI télécharge son historique. Redemande dans quelques minutes.",
}

# ---------------------------------------------------------------------------------------------
# 1. Vérifier un signal
st.subheader("1. Vérifier un signal")
with st.form("csi_evaluate"):
    text = st.text_area("Colle ici un signal (Telegram ou autre)", height=160, max_chars=csi_client.MAX_TEXT_CHARS,
                        placeholder="PAIR: ETH/USDT\nENTRY 1: 2690\nT1: 2745\nSL: 2650")
    source = st.text_input("De quel groupe vient-il ?", value=st.session_state.get("csi_source", ""),
                           placeholder="ex. Suhaib")
    submitted = st.form_submit_button("Vérifier", type="primary", icon=":material/fact_check:")
if submitted:
    if not text.strip():
        st.warning("Colle d'abord un signal.")
    else:
        try:
            # Collé ici = validé par toi : une paire inconnue de CSI est ajoutée à sa liste.
            opinion = client.evaluate(text, source=source.strip() or "manuel", user_validated=True)
            st.session_state["csi_source"] = source.strip()
            st.session_state["csi_last_opinion"] = opinion
        except CsiUnavailable as exc:
            st.error(f"Vérification impossible : {exc}")
        except ValueError as exc:
            st.warning(str(exc))
opinion = st.session_state.get("csi_last_opinion")
if opinion is not None:
    BOXES.get(opinion.verdict, st.info)(f"{opinion.icon} **{opinion.label}** — {IN_SHORT.get(opinion.verdict, '')}")
    with st.expander("Pourquoi ?"):
        st.write(opinion.summary)
        checks = opinion.raw.get("checks") or []
        if checks:
            st.dataframe([{"Contrôle": c.get("label"), "OK": bool(c.get("ok")), "Détail": c.get("detail")}
                          for c in checks], hide_index=True, width="stretch")
    st.caption("Pour l'exécuter sur Binance Demo : page Signaux (colle-le, simule, confirme).")

# ---------------------------------------------------------------------------------------------
# 2. Signaux trouvés par CSI
st.subheader("2. Signaux trouvés par CSI")
st.warning(
    "⚠️ **Stratégies non validées.** Testées sur 5 ans, elles perdent en moyenne après frais. Ces signaux servent "
    "à observer ou à tester **à la main sur Demo** : rien ne part tout seul."
)
try:
    generated = client.generated(15)
except CsiUnavailable as exc:
    generated = []
    st.error(f"Liste indisponible : {exc}")
if not generated:
    st.caption("Aucun signal trouvé pour l'instant. CSI en cherche toutes les 15 minutes.")
scope = account_scope(settings)
for item in generated:
    with st.container(border=True):
        cols = st.columns([3, 2, 2])
        targets = ", ".join(item.get("targets") or [])
        cols[0].markdown(f"**{item['symbol']}** · achat à **{item['entry']}** · objectif {targets} · "
                         f"stop {item['stop_loss']}")
        cols[0].caption(f"{item['strategy']} · trouvé le {item['created_at'][:16].replace('T', ' ')} UTC · "
                        f"tendance {item.get('trend_regime')}, volatilité {item.get('volatility_regime')}")
        if item.get("expired"):
            cols[1].caption("⌛ Expiré : l'entrée n'est plus valable.")
        else:
            cols[1].caption(f"Valable jusqu'à {item['entry_expires_at'][11:16]} UTC")
        verdict = item.get("strategy_verdict")
        expectancy = item.get("strategy_expectancy_r")
        cols[1].caption(f"Stratégie : {verdict or 'non testée'}"
                        + (f" ({float(expectancy):+.2f} R par trade en moyenne)" if expectancy is not None else ""))
        if cols[2].button("Tester en Demo", key=f"test_{item['signal_id']}", disabled=bool(item.get("expired")),
                          icon=":material/science:",
                          help="L'ajoute à la page Signaux, où tu choisis le budget et confirmes."):
            row = SignalInbox().receive(scope, item["bsm_text"], template="auto")
            st.session_state["selected_signal"] = row["id"]
            st.switch_page("pages/8_Signaux.py")

# ---------------------------------------------------------------------------------------------
with st.expander("Détails (bilan des groupes, stratégies, historique)"):
    try:
        report = client.sources()
        records = report.get("sources") or []
        st.markdown("**Bilan des groupes Telegram**")
        st.caption(f"Règle : {report.get('rule', '')}.")
        if records:
            needed = int(report.get("min_resolved") or 20)
            st.dataframe([{"Groupe": r.get("source"),
                           "Vers une conclusion": min(1.0, (r.get("resolved") or 0) / needed),
                           "Résolus": f"{r.get('resolved') or 0}/{needed}", "Évalués": r.get("evaluated"),
                           "Écart moyen (R)": r.get("edge_r"), "Conclusion": r.get("conclusion")} for r in records],
                         hide_index=True, width="stretch",
                         column_config={"Vers une conclusion": st.column_config.ProgressColumn(
                             "Vers une conclusion", min_value=0.0, max_value=1.0, format="percent",
                             help=f"CSI conclut sur un groupe après {needed} signaux résolus "
                                  f"(sur au moins {report.get('min_days') or 10} jours)")})
        strategies = client.strategies()
        if strategies:
            st.markdown("**Stratégies de CSI (dernier test)**")
            st.dataframe([{"Stratégie": s.get("strategy"), "Verdict": s.get("verdict"),
                           "Résultat moyen (R)": s.get("expectancy_r"), "Trades testés": s.get("trades_closed")}
                          for s in strategies], hide_index=True, width="stretch")
        recent = client.recent(20)
        if recent:
            st.markdown("**Dernières vérifications**")
            st.dataframe([{"Reçu": r.get("received_at", "")[:16], "Groupe": r.get("source"), "Paire": r.get("symbol"),
                           "Avis": csi_client.verdict_label(r.get("verdict")), "Issue": r.get("outcome")}
                          for r in recent], hide_index=True, width="stretch")
    except CsiUnavailable as exc:
        st.warning(f"Détails indisponibles : {exc}")
