#!/usr/bin/env bash
# shellcheck disable=SC1112 # apostrophes typographiques (’) voulues dans les messages
# Sauvegarde CHIFFRÉE de data/ et logs/, sur le VPS (make backup-chiffre).
#
#   make backup-chiffre              → backups/bsm-<AAAAMMJJ-HHMMSS UTC>.tar.gz.age, 14 gardées
#   make backup-chiffre GARDER=30    → en garder 30
#
# Même copie que « make backup » : le worker est arrêté le temps de la copie puis relancé
# (seulement s'il tournait : un worker arrêté exprès le reste), et la clé maîtresse du coffre
# n'est jamais copiée (volume bsm-keys, hors de /app). Différence : l'archive est chiffrée À LA
# VOLÉE par age avec la CLÉ PUBLIQUE du propriétaire (deploy/sauvegarde.age.pub, hors Git) :
#
#     tar (dans un conteneur) ─tube─▶ age -R clé publique ─▶ backups/…tar.gz.age
#
# Aucune archive en clair n'est écrite sur le disque, et le VPS ne peut pas relire ses propres
# sauvegardes : la clé privée reste sur le PC du propriétaire. Récupération, déchiffrement et
# restauration : docs/SECURITE_VPS.md.
#
# Les fonctions peuvent être chargées sans rien lancer (source) : c'est ainsi que
# tests/test_ops_scripts.py teste la rotation et l'enchaînement, sans Docker.
set -uo pipefail
umask 077
export LC_ALL=C

_SCRIPT=${BASH_SOURCE[0]}
_DOSSIER_SCRIPT=${_SCRIPT%/*}
[ "$_DOSSIER_SCRIPT" = "$_SCRIPT" ] && _DOSSIER_SCRIPT=.
PROJET=$(cd "$_DOSSIER_SCRIPT/.." && pwd)
CLE_PUBLIQUE=$PROJET/deploy/sauvegarde.age.pub
DOSSIER=$PROJET/backups
GARDER_PAR_DEFAUT=14
# Seuls les fichiers à ce nom exact sont concernés par la rotation (jamais ceux de make backup).
MOTIF_NOM='^bsm-[0-9]{8}-[0-9]{6}\.tar\.gz\.age$'
read -r -a COMPOSE_CMD <<< "${COMPOSE:-docker compose}"

WORKER_A_RELANCER=0
RELANCE_ECHOUEE=0
PARTIEL=""

usage() {
    cat <<'FIN'
Sauvegarde chiffrée de data/ et logs/ (worker arrêté le temps de la copie).

  make backup-chiffre              14 sauvegardes gardées
  make backup-chiffre GARDER=30    en garder 30

Ou directement : bash scripts/sauvegarde_chiffree.sh [--garder N]
Prérequis : age installé (sudo apt install age) et la clé PUBLIQUE dans
deploy/sauvegarde.age.pub. Guide : docs/SECURITE_VPS.md
FIN
}

echouer() {   # $1 = motif du refus, $2 = commande ou piste pour corriger (facultatif)
    printf 'Refusé : %s\n' "$1" >&2
    if [ -n "${2:-}" ]; then printf '  → %s\n' "$2" >&2; fi
    exit 1
}

# --- Docker : remplacées par des doublures dans les tests -------------------------------------
worker_en_marche() { [ -n "$("${COMPOSE_CMD[@]}" ps -q --status running worker 2>/dev/null)" ]; }
arreter_worker()   { "${COMPOSE_CMD[@]}" stop worker; }
relancer_worker()  { "${COMPOSE_CMD[@]}" start worker; }
# Archive tar.gz de data/ et logs/ sur la SORTIE STANDARD (jamais sur le disque) : même commande
# que make backup, dans un conteneur ponctuel du worker (mêmes volumes ; la clé maîtresse est
# montée hors de /app, elle n'est donc pas copiée).
produire_archive() { "${COMPOSE_CMD[@]}" run --rm --no-deps -T worker tar czf - -C /app data logs; }

# --- Contrôles avant d'arrêter quoi que ce soit ----------------------------------------------
verifier_prerequis() {
    command -v age >/dev/null 2>&1 \
        || echouer "age n'est pas installé sur ce serveur." "sudo apt install age"
    [ -f "$CLE_PUBLIQUE" ] || echouer "clé publique absente : deploy/sauvegarde.age.pub" \
        "sur le PC : age-keygen -y ~/.config/bsm-sauvegarde.key | ssh <vps> 'cat > ~/BinanceSpotManager/BinanceSpotManager/deploy/sauvegarde.age.pub' (docs/SECURITE_VPS.md)"
    if grep -q 'AGE-SECRET-KEY-' "$CLE_PUBLIQUE"; then
        echouer "deploy/sauvegarde.age.pub contient une CLÉ PRIVÉE. Elle ne doit jamais être sur le VPS." \
            "supprimer ce fichier du VPS, créer une nouvelle paire de clés sur le PC et n'envoyer que la ligne publique age1… (docs/SECURITE_VPS.md)"
    fi
    tr -d '\r' < "$CLE_PUBLIQUE" | grep -Eq '^age1[0-9a-z]{58}$' \
        || echouer "aucune clé publique age valide (ligne age1…) dans deploy/sauvegarde.age.pub" \
            "sur le PC : age-keygen -y ~/.config/bsm-sauvegarde.key affiche la bonne ligne (docs/SECURITE_VPS.md)"
}

valider_garder() {   # nombre entier >= 1, écrit en décimal
    case ${1:-} in
        '' | *[!0-9]*) echouer "nombre de sauvegardes à garder invalide : « ${1:-} » (entier attendu)" ;;
    esac
    [ $((10#$1)) -ge 1 ] || echouer "il faut garder au moins une sauvegarde (GARDER=0 refusé)"
}

# --- Rotation : garde les N plus récentes, ne touche à rien d'autre ---------------------------
tourner_sauvegardes() {   # $1 = dossier, $2 = nombre à garder
    local dossier=$1 garder chemin nom exces i
    valider_garder "${2:-}"
    garder=$((10#$2))
    local -a fichiers=()
    # Développement trié par nom (LC_ALL=C) : la date du nom donne l'ordre chronologique.
    for chemin in "$dossier"/bsm-*.tar.gz.age; do
        nom=${chemin##*/}
        if [ -f "$chemin" ] && [[ $nom =~ $MOTIF_NOM ]]; then fichiers+=("$chemin"); fi
    done
    exces=$((${#fichiers[@]} - garder))
    for ((i = 0; i < exces; i++)); do
        rm -f -- "${fichiers[i]}" && echo "Ancienne sauvegarde supprimée : ${fichiers[i]##*/}"
    done
    if [ "$exces" -lt 0 ]; then exces=0; fi
    echo "Sauvegardes chiffrées conservées : $((${#fichiers[@]} - exces)) (au plus $garder)"
}

# --- Relance du worker : toujours tentée, même après une erreur ou une interruption ----------
relancer_si_besoin() {
    if [ "$WORKER_A_RELANCER" = 1 ]; then
        WORKER_A_RELANCER=0
        echo "Relance du worker…"
        if ! relancer_worker; then
            RELANCE_ECHOUEE=1
            printf 'ATTENTION : le worker n’est PAS reparti. Le relancer tout de suite : make worker-start\n' >&2
        fi
    fi
}

nettoyer() {
    local code=$?
    relancer_si_besoin
    if [ -n "$PARTIEL" ]; then rm -f -- "$PARTIEL"; fi
    if [ "$RELANCE_ECHOUEE" = 1 ] && [ "$code" -eq 0 ]; then code=1; fi
    exit "$code"
}

sauvegarder() {   # $1 = nombre de sauvegardes à garder
    local garder=$1 horodatage nom entete
    local -a etats
    verifier_prerequis
    mkdir -p -- "$DOSSIER" || echouer "impossible de créer $DOSSIER"

    # Une seule sauvegarde à la fois (planification + lancement à la main).
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$DOSSIER/.sauvegarde.lock"
        flock -n 9 || echouer "une autre sauvegarde est déjà en cours."
    fi

    horodatage=$(date -u +%Y%m%d-%H%M%S)
    nom="bsm-$horodatage.tar.gz.age"
    [ ! -e "$DOSSIER/$nom" ] || echouer "backups/$nom existe déjà : relancer dans une seconde."
    # Nom provisoire caché, hors du motif *.age : jamais pris pour une sauvegarde complète.
    PARTIEL="$DOSSIER/.$nom.partiel"

    if worker_en_marche; then
        WORKER_A_RELANCER=1
        echo "Arrêt du worker le temps de la copie (ses ordres déjà posés chez Binance ne bougent pas)…"
        arreter_worker || echouer "arrêt du worker impossible : sauvegarde annulée"
    else
        echo "Worker déjà arrêté : il le restera après la sauvegarde."
    fi

    # Le chiffrement passe par un tube : l'archive en clair n'existe qu'en mémoire.
    produire_archive | age -R "$CLE_PUBLIQUE" -o "$PARTIEL"
    etats=("${PIPESTATUS[@]}")
    relancer_si_besoin

    # age d'abord : s'il échoue, la copie s'arrête aussi (tube fermé) sans en être la cause.
    [ "${etats[1]}" -eq 0 ] || echouer "chiffrement par age échoué (code ${etats[1]}) : aucune sauvegarde écrite" \
        "vérifier l'espace disque (df -h) et deploy/sauvegarde.age.pub"
    [ "${etats[0]}" -eq 0 ] || echouer "copie des données échouée (code ${etats[0]}) : aucune sauvegarde écrite" \
        "make ps, puis make logs SERVICE=worker"
    entete=""
    IFS= read -r entete < "$PARTIEL" || true
    [ "$entete" = "age-encryption.org/v1" ] \
        || echouer "le fichier produit n'est pas chiffré par age : supprimé, aucune sauvegarde écrite"

    mv -f -- "$PARTIEL" "$DOSSIER/$nom" || echouer "impossible de nommer backups/$nom"
    PARTIEL=""
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Sauvegarde chiffrée : backups/$nom ($(du -h -- "$DOSSIER/$nom" | cut -f1))"
    tourner_sauvegardes "$DOSSIER" "$garder"
    if [ "$RELANCE_ECHOUEE" = 1 ]; then
        echouer "sauvegarde écrite, mais le worker n'a pas redémarré" "make worker-start"
    fi
}

principal() {
    local garder=$GARDER_PAR_DEFAUT
    while [ $# -gt 0 ]; do
        case $1 in
            --garder) [ $# -ge 2 ] || echouer "--garder attend un nombre"; garder=$2; shift 2 ;;
            --garder=*) garder=${1#*=}; shift ;;
            -h | --help) usage; return 0 ;;
            *) usage >&2; echouer "option inconnue : $1" ;;
        esac
    done
    valider_garder "$garder"
    cd "$PROJET" || echouer "dossier du projet introuvable : $PROJET"
    trap nettoyer EXIT
    trap 'exit 130' INT TERM HUP
    sauvegarder "$garder"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    principal "$@"
fi
