"""Avis CSI : évaluer un signal, bilan des groupes Telegram, état de CryptoSignalIntelligence."""
from pathlib import Path
import sys

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager import csi_client  # noqa: E402
from binance_spot_manager.config import get_settings  # noqa: E402
from binance_spot_manager.csi_client import CsiUnavailable  # noqa: E402
from ui_common import banner, page_header, sidebar_status  # noqa: E402

st.set_page_config(page_title="Avis CSI — Binance Demo", page_icon=":material/psychology:", layout="wide")
page_header("Avis CSI", "CryptoSignalIntelligence : évaluer un signal, bilan des groupes, état de la surveillance")
settings = get_settings()
banner(settings)
sidebar_status(settings)
st.info(
    "CSI ne place aucun ordre et n'a aucune clé : il donne un avis. L'exécution se décide sur la page "
    "Signaux, sur Binance Demo. Un taux de base historique n'est jamais la probabilité qu'un signal réussisse."
)

client = csi_client.CsiClient.from_env()
health, failure = client.probe()
if health is None:
    st.warning(f"CSI injoignable : {failure}")
    st.caption(
        "Démarrer CSI depuis son dossier (CryptoSignalIntelligence) : `docker compose up -d`. "
        f"Adresse attendue : `{client.base_url}` (variable `BSM_CSI_API_URL`). Les signaux automatiques "
        "restent retenus tant que CSI ne répond pas, selon le réglage Settings → Signaux."
    )
    st.stop()
(st.success if health.get("ready") else st.warning)(
    ("Surveillance CSI prête" if health.get("ready") else "Surveillance CSI pas encore prête")
    + f" — {health.get('detail', '')}"
)

# ---------------------------------------------------------------------------------------------
st.subheader("Évaluer un signal")
with st.form("csi_evaluate"):
    source = st.text_input(
        "Groupe (source)", value=st.session_state.get("csi_source", ""),
        help="Nom du groupe Telegram d'où vient le signal : CSI tient un bilan par groupe.",
    )
    text = st.text_area("Coller le signal", height=200, max_chars=csi_client.MAX_TEXT_CHARS)
    record = st.checkbox(
        "Enregistrer l'évaluation (CSI suivra l'issue réelle du signal et construira le bilan du groupe)",
        value=True,
    )
    submitted = st.form_submit_button("Demander l'avis de CSI")
if submitted:
    if not text.strip() or not source.strip():
        st.warning("Indiquer le groupe et coller le signal.")
    else:
        try:
            opinion = client.evaluate(text, source=source.strip(), record=record)
            st.session_state["csi_source"] = source.strip()
            st.session_state["csi_last_opinion"] = opinion
        except CsiUnavailable as exc:
            st.error(f"Évaluation impossible : {exc}")
        except ValueError as exc:
            st.warning(str(exc))
opinion = st.session_state.get("csi_last_opinion")
if opinion is not None:
    boxes = {"REFUSE": st.error, "DEFAVORABLE": st.error, "INDETERMINE": st.warning, "FAVORABLE": st.success}
    boxes.get(opinion.verdict, st.info)(f"{opinion.icon} **{opinion.label}** — {opinion.summary}")
    with st.expander("Détail de l'évaluation"):
        checks = opinion.raw.get("checks") or []
        if checks:
            st.dataframe(
                [{"Contrôle": c.get("label"), "OK": bool(c.get("ok")), "Détail": c.get("detail")} for c in checks],
                hide_index=True, width="stretch",
            )
        rate = opinion.raw.get("base_rate")
        if rate:
            st.write({
                "Ordres semblables (remplis, résolus)": rate.get("samples"),
                "TP1 avant stop": f"{100 * float(rate.get('tp_first') or 0):.0f} %",
                "Espérance par ordre rempli (R)": rate.get("expectancy_r"),
                "IC95 (R)": rate.get("expectancy_r_ci95"),
                "Régime": rate.get("regime"),
                "Méthode": rate.get("method"),
            })
        for warning in opinion.raw.get("warnings") or []:
            st.warning(warning)
        if opinion.record_id:
            st.caption(f"Enregistré chez CSI sous {opinion.record_id} ; l'issue réelle sera mesurée automatiquement.")
    st.caption("Pour exécuter ce signal sur Binance Demo : page Signaux (analyse, simulation, confirmation).")

# ---------------------------------------------------------------------------------------------
st.subheader("Bilan des groupes Telegram")
try:
    report = client.sources()
    records = report.get("sources") or []
    st.caption(f"Règle de CSI : {report.get('rule', '')}.")
    if records:
        st.dataframe(
            [{
                "Groupe": r.get("source"), "Évalués": r.get("evaluated"), "Résolus": r.get("resolved"),
                "TP1 réel": r.get("tp1_real"), "TP1 de base": r.get("tp1_base"),
                "Écart moyen (R)": r.get("edge_r"), "IC95 de l'écart": r.get("edge_ci95"),
                "Conclusion": r.get("conclusion"),
            } for r in records],
            hide_index=True, width="stretch",
        )
    else:
        st.caption("Aucun signal évalué pour l'instant.")
except CsiUnavailable as exc:
    st.warning(f"Bilan indisponible : {exc}")

# ---------------------------------------------------------------------------------------------
st.subheader("Stratégies de CSI (dernier walk-forward)")
try:
    strategies = client.strategies()
    if strategies:
        st.dataframe(
            [{
                "Stratégie": s.get("strategy"), "Verdict": s.get("verdict"),
                "E[R] hors échantillon": s.get("expectancy_r"), "IC95": s.get("expectancy_r_ci95"),
                "Trades clos": s.get("trades_closed"), "Run": s.get("run_id"),
            } for s in strategies],
            hide_index=True, width="stretch",
        )
        st.caption("Seule une stratégie VALIDATED_OOS puis promue par le propriétaire peut produire des signaux exécutables.")
    else:
        st.caption("Aucun walk-forward enregistré.")
except CsiUnavailable as exc:
    st.warning(f"Stratégies indisponibles : {exc}")

# ---------------------------------------------------------------------------------------------
st.subheader("Dernières évaluations")
try:
    recent = client.recent(20)
    if recent:
        st.dataframe(
            [{
                "Reçu": r.get("received_at"), "Groupe": r.get("source"), "Paire": r.get("symbol"),
                "Entrée": r.get("entry"), "Stop": r.get("stop"), "TP1": r.get("tp1"),
                "Avis": csi_client.verdict_label(r.get("verdict")), "Issue": r.get("outcome"),
                "R réel": r.get("outcome_r"),
            } for r in recent],
            hide_index=True, width="stretch",
        )
    else:
        st.caption("Aucune évaluation enregistrée.")
except CsiUnavailable as exc:
    st.warning(f"Historique indisponible : {exc}")
