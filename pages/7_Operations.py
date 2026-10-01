"""Suivi des demandes, reprises en lecture seule, alertes et sauvegardes."""

import hashlib
from pathlib import Path
import sys
import uuid

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.command_store import account_scope
from binance_spot_manager.config import DATA_DIR
from binance_spot_manager.order_journal import OrderJournal
from binance_spot_manager.backup_manager import build_backup, verify_backup
from binance_spot_manager.accounting import accounting_snapshot
from binance_spot_manager.protection_status import protection_overview
from ui_common import get_service, banner, sidebar_status, pnl_dataframe

from ui_common import require_login  # noqa: E402

# Connexion exigee avant tout affichage (comptes : scripts/creer_compte.py).
require_login()

st.set_page_config(page_title="Operations — BinanceSpotManager", page_icon=":material/fact_check:", layout="wide")
st.title("Operations")
st.caption("Demandes au worker, protection et reprise — Binance Demo uniquement")
service = get_service()
banner(service.settings)
sidebar_status(service.settings)
if not service.settings.is_demo:
    st.error("Operations reservees a Binance Demo")
    st.stop()
scope = account_scope(service.settings)
if st.button("Tester le circuit de commande (sans ordre)"):
    try:
        command = service.submit_command("CHECK_CONNECTION", {}, request_key=uuid.uuid4().hex)
        st.info(f"Test {command['id'][:12]} ajoute a la file. Son resultat apparait ci-dessous.")
    except Exception as exc:
        st.error(str(exc))


@st.fragment(run_every="1s")
def command_panel():
    st.subheader("Demandes au worker")
    commands = service.commands.list_recent(scope)
    if not commands:
        st.caption("Aucune demande enregistree pour ce compte et ce mode.")
        return
    labels = {"PENDING": "En attente", "RUNNING": "En cours", "SUCCEEDED": "Traitee",
              "FAILED": "Refusee", "UNCERTAIN": "A verifier", "EXPIRED": "Expiree", "CANCELED": "Retiree"}
    pnl_dataframe([{"Demande": c["id"][:12], "Action": c["action"], "Etat": labels[c["state"]],
                   "Resultat": c["result"].get("message", "")} for c in commands], hide_index=True)
    selected = st.selectbox("Detail d'une demande", [c["id"] for c in commands],
                            format_func=lambda cid: cid[:12], key="operations_command")
    command = next(c for c in commands if c["id"] == selected)
    st.json(command["result"])
    if command["state"] == "UNCERTAIN":
        st.error("Le resultat n'est pas confirme. Aucun rejeu automatique. Verifie les intentions et les ordres Binance ci-dessous.")
    if command["state"] == "PENDING" and st.button("Retirer cette demande avant execution"):
        if service.commands.cancel_pending(scope, selected):
            st.success("Demande retiree ; aucun ordre envoye par cette demande.")
        else:
            st.warning("Deja prise en charge : consulte son resultat, ne suppose pas une annulation.")


command_panel()
st.caption("Traitee signifie que le worker a traite la demande, pas que tout ordre accepte est deja rempli. Les achats expirent apres deux minutes en attente.")

@st.fragment(run_every="1s")
def protection_panel():
    st.subheader("Protection des positions")
    if st.button("Verifier maintenant sur Binance Demo"):
        try:
            st.session_state.operations_protection = service.inspect_exit_orders()
        except Exception as exc:
            st.error(str(exc))
    positions_now = service.positions.list_all()
    report = st.session_state.get("operations_protection")
    pnl_dataframe(protection_overview(positions_now, report), hide_index=True)
    for error in service.positions.read_errors:
        st.error(error)
    st.caption("Un controle a plus de 30 secondes est considere perime. Un stop-limit peut se declencher sans s'executer.")


protection_panel()
positions = service.positions.list_all()

st.subheader("Intentions d'ordres : controle sans renvoi")
namespace = service.settings.base_url + ":" + hashlib.sha256(service.settings.api_key.encode()).hexdigest()
intents = OrderJournal(DATA_DIR / "order_intents.sqlite3").recent(namespace)
if intents:
    chosen = st.selectbox("Intention a consulter", range(len(intents)),
                          format_func=lambda n: f"{intents[n]['symbol']} · {intents[n]['client_id']}")
    intent = intents[chosen]
    if st.button("Lire le statut de cette intention sur Binance"):
        try:
            if intent["payload"].get("listClientOrderId"):
                result = service.client.get_order_list(list_client_order_id=intent["client_id"])
            else:
                result = service.client.find_order(intent["symbol"], client_order_id=intent["client_id"])
            if result is None:
                st.warning("Ordre non retrouve. Cela n'autorise pas un nouvel envoi automatique.")
            else:
                st.json(result)
        except Exception as exc:
            st.error(f"Verification impossible : {exc}")
else:
    st.caption("Aucune intention dans le nouveau journal. Les ordres anterieurs restent visibles au Dashboard.")

@st.fragment(run_every="5s")
def alerts_panel():
    st.subheader("Alertes persistantes")
    inbox = service.events.alert_inbox()

    def read_all(versions):
        count = inbox.acknowledge_all(expected_versions=versions)
        st.session_state.operations_alert_message = f"{count} alerte(s) marquée(s) comme lue(s)."

    def delete_all(versions):
        count = inbox.delete_all(expected_versions=versions)
        st.session_state.operations_alert_message = f"{count} alerte(s) supprimée(s). Le journal des événements est conservé."

    def read_one(alert_id, version):
        if inbox.acknowledge(alert_id, expected_last_seen=version):
            st.session_state.operations_alert_message = "Alerte marquée comme lue."
        else:
            st.session_state.operations_alert_notice = "Cette alerte a évolué ou a été supprimée. La liste est actualisée ; aucune nouvelle occurrence n'a été marquée comme lue."

    def delete_one(alert_id, version):
        if inbox.delete(alert_id, expected_last_seen=version):
            st.session_state.operations_alert_message = "Alerte supprimée. Le journal des événements est conservé."
        else:
            st.session_state.operations_alert_notice = "Cette alerte a évolué ou a été supprimée. La liste est actualisée ; aucune nouvelle occurrence n'a été supprimée."

    if message := st.session_state.pop("operations_alert_message", None):
        st.success(message)
    if notice := st.session_state.pop("operations_alert_notice", None):
        st.info(notice)
    counts = inbox.counts()
    st.caption(f"{counts['unread']} non lue(s) · {counts['read']} lue(s) · {counts['total']} au total · actualisé toutes les 5 secondes.")
    selected_filter = st.selectbox("Afficher les alertes", ["Toutes", "Non lues", "Lues"], key="operations_alert_filter")
    actions = st.columns(2)
    versions = inbox.versions()
    actions[0].button("Tout marquer comme lu", key="alerts_read_all", on_click=read_all, args=(versions,), disabled=counts["unread"] == 0)
    actions[1].button("Tout supprimer", key="alerts_delete_all", on_click=delete_all, args=(versions,), disabled=counts["total"] == 0)
    st.caption("Les actions « Tout » concernent toutes les alertes déjà présentes, quel que soit le filtre. Les nouvelles alertes ou occurrences arrivées depuis l'affichage sont conservées.")
    alerts = inbox.recent(unread=selected_filter == "Non lues", read=selected_filter == "Lues")
    if not alerts:
        st.caption("Aucune alerte pour ce filtre." if counts["total"] else "Aucune alerte persistante.")
        return
    matching_count = counts[{"Toutes": "total", "Non lues": "unread", "Lues": "read"}[selected_filter]]
    st.caption(f"{len(alerts)} alerte(s) affichée(s) sur {matching_count}, les plus récentes en premier.")
    pnl_dataframe([{"Alerte": a["record"].get("message", ""), "Niveau": a["record"].get("level"),
                   "Derniere occurrence": a["last_seen"], "Occurrences": a["occurrences"],
                   "Lue": a["acknowledged_at"] is not None} for a in alerts], hide_index=True)
    by_id = {a["id"]: a for a in alerts}
    chosen_alert = st.selectbox("Alerte à gérer", list(by_id), key="operations_alert_selection",
                               format_func=lambda aid: f"{by_id[aid]['last_seen']} · {by_id[aid]['record'].get('message', aid)}")
    single_actions = st.columns(2)
    single_actions[0].button("Marquer cette alerte comme lue", disabled=by_id[chosen_alert]["acknowledged_at"] is not None,
                             on_click=read_one, args=(chosen_alert, by_id[chosen_alert]["last_seen"]))
    single_actions[1].button("Supprimer cette alerte", key="alerts_delete_one", on_click=delete_one,
                             args=(chosen_alert, by_id[chosen_alert]["last_seen"]))


alerts_panel()

st.subheader("Comptabilite detaillee")
accounting = [accounting_snapshot(p, fee_rates=service.fee_rates(p)) for p in positions]
if accounting:
    pnl_dataframe(accounting, hide_index=True)
    st.caption("Vue au coût moyen net : frais en base et cotation inclus. Les frais BNB/autres valorisables utilisent le cours Binance actuel et restent signalés comme estimatifs ; les actifs sans taux restent explicites. Ne pas additionner des devises différentes.")

st.subheader("Sauvegardes verifiables")
st.caption("Export des positions, preferences, presets et bases de commandes/intention. Ni .env, ni .venv, ni cles API de configuration. Archive non chiffree : conserver dans un emplacement prive.")
if st.button("Preparer une sauvegarde (worker arrete)"):
    try:
        raw = build_backup(DATA_DIR, worker_running=service.worker_status().running)
        verify_backup(raw)
        st.session_state.operations_backup = raw
    except Exception as exc:
        st.error(str(exc))
if st.session_state.get("operations_backup"):
    st.download_button("Telecharger la sauvegarde verifiee", st.session_state.operations_backup,
                       file_name="binance-demo-backup.zip", mime="application/zip")
uploaded = st.file_uploader("Verifier une archive avant restauration", type=["zip"])
if uploaded is not None and st.button("Controler l'integrite de l'archive"):
    try:
        manifest = verify_backup(uploaded.getvalue())
        st.success(f"Archive integre : {len(manifest['files'])} fichiers, creee le {manifest['created_at']}.")
        st.warning("Aucune restauration automatique : conserver les intentions recentes et rapprocher les ordres Binance avant toute remise en service.")
    except Exception as exc:
        st.error(f"Archive refusee : {exc}")
