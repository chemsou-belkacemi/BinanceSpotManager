#!/usr/bin/env bash
# Sur le PC du propriétaire : rapatrie les sauvegardes CHIFFRÉES du VPS, sans rien y supprimer.
#
#   bash scripts/recuperer_sauvegardes.sh <hote-ssh> [dossier]
#   bash scripts/recuperer_sauvegardes.sh csi@203.0.113.10                → ~/sauvegardes-bsm
#   bash scripts/recuperer_sauvegardes.sh csi@203.0.113.10 /media/cle/bsm
#
# Copie par rsync (scp si rsync manque) les fichiers backups/bsm-*.tar.gz.age du VPS. Rien n'est
# supprimé, ni sur le VPS ni sur le PC : le PC garde donc plus d'historique que le VPS (qui n'en
# garde que 14). Dossier distant par défaut : BinanceSpotManager/BinanceSpotManager/backups,
# depuis le dossier personnel sur le VPS (disposition de docs/VPS.md de CSI) ; sinon :
#   DOSSIER_DISTANT=/home/moi/bsm/backups bash scripts/recuperer_sauvegardes.sh <hote-ssh>
#
# Si age est installé et que la clé privée est là (~/.config/bsm-sauvegarde.key, ou
# CLE_SAUVEGARDE=<chemin>), la sauvegarde la plus récente est vérifiée : déchiffrée dans un tube
# et relue par tar, sans rien écrire en clair sur le disque. Déchiffrer et restaurer :
# docs/SECURITE_VPS.md.
set -uo pipefail
umask 077
export LC_ALL=C

usage() {
    cat <<'FIN'
Usage : bash scripts/recuperer_sauvegardes.sh <hote-ssh> [dossier]

  <hote-ssh>  ce que tu tapes après « ssh » pour joindre le VPS (ex. csi@203.0.113.10)
  [dossier]   où ranger les copies sur ce PC (défaut : ~/sauvegardes-bsm)

Rien n'est supprimé sur le VPS. Variables facultatives :
  DOSSIER_DISTANT  dossier des sauvegardes sur le VPS
                   (défaut : BinanceSpotManager/BinanceSpotManager/backups)
  CLE_SAUVEGARDE   clé privée age pour vérifier la dernière copie
                   (défaut : ~/.config/bsm-sauvegarde.key)
FIN
}

HOTE=${1:-}
case $HOTE in
    -h | --help)
        usage
        exit 0
        ;;
    '')
        usage >&2
        exit 2
        ;;
    -*)
        echo "Refusé : hôte invalide « $HOTE »" >&2
        exit 2
        ;;
esac
DESTINATION=${2:-$HOME/sauvegardes-bsm}
DISTANT=${DOSSIER_DISTANT:-BinanceSpotManager/BinanceSpotManager/backups}
DISTANT=${DISTANT%/}
CLE=${CLE_SAUVEGARDE:-$HOME/.config/bsm-sauvegarde.key}
MOTIF='bsm-*.tar.gz.age'

# Le chemin distant est interprété par le shell du VPS : caractères simples seulement.
case $DISTANT in
    '' | *[!A-Za-z0-9._/-]*)
        echo "Refusé : DOSSIER_DISTANT ne doit contenir que lettres, chiffres, . _ - / (reçu « $DISTANT »)" >&2
        exit 2
        ;;
esac

lister() { # sauvegardes chiffrées présentes dans la destination (une par ligne, triées)
    local f
    for f in "$DESTINATION"/bsm-*.tar.gz.age; do
        if [ -f "$f" ]; then printf '%s\n' "${f##*/}"; fi
    done
}

mkdir -p -- "$DESTINATION" || {
    echo "Refusé : impossible de créer $DESTINATION" >&2
    exit 1
}
AVANT=$(lister)

copier_scp() {
    echo "Copie par scp depuis $HOTE:$DISTANT/ …"
    scp -p "$HOTE:$DISTANT/$MOTIF" "$DESTINATION/"
}

if command -v rsync >/dev/null 2>&1; then
    echo "Copie par rsync depuis $HOTE:$DISTANT/ …"
    # Sans --delete ni --remove-source-files : rien n'est jamais supprimé, d'un côté ou de l'autre.
    rsync -a --ignore-existing --itemize-changes --include="$MOTIF" --exclude='*' \
        "$HOTE:$DISTANT/" "$DESTINATION/"
    CODE=$?
    if [ "$CODE" -eq 12 ] || [ "$CODE" -eq 127 ]; then
        echo "rsync indisponible sur le VPS (code $CODE) : essai avec scp."
        copier_scp
        CODE=$?
    fi
elif command -v scp >/dev/null 2>&1; then
    copier_scp
    CODE=$?
else
    echo "Refusé : ni rsync ni scp sur ce PC. Installer : sudo apt install rsync openssh-client" >&2
    exit 1
fi
if [ "$CODE" -ne 0 ]; then
    echo "ÉCHEC de la copie (code $CODE) : vérifier « ssh $HOTE » et le dossier $DISTANT sur le VPS." >&2
    exit 1
fi

APRES=$(lister)
TOTAL=0
NOUVELLES=0
DERNIERE=""
while IFS= read -r nom; do
    [ -n "$nom" ] || continue
    TOTAL=$((TOTAL + 1))
    DERNIERE=$nom
    if ! printf '%s\n' "$AVANT" | grep -Fqx -- "$nom"; then NOUVELLES=$((NOUVELLES + 1)); fi
done < <(printf '%s\n' "$APRES")
echo "$NOUVELLES nouvelle(s) sauvegarde(s) ; $TOTAL en tout dans $DESTINATION"
if [ -z "$DERNIERE" ]; then
    echo "Aucune sauvegarde chiffrée récupérée : lancer « make backup-chiffre » sur le VPS." >&2
    exit 1
fi
echo "La plus récente : $DERNIERE"

# Vérification : déchiffrement dans un tube, relu par tar (liste seulement, rien d'extrait).
if ! command -v age >/dev/null 2>&1; then
    echo "Vérification sautée : age n'est pas installé sur ce PC (sudo apt install age)."
elif [ ! -f "$CLE" ]; then
    echo "Vérification sautée : clé privée absente ($CLE)."
elif LISTE=$(age -d -i "$CLE" -- "$DESTINATION/$DERNIERE" | tar tzf -); then
    if printf '%s\n' "$LISTE" | grep -q '^data/'; then
        echo "✔ Vérifiée : $DERNIERE se déchiffre et l'archive est complète ($(printf '%s\n' "$LISTE" | grep -c .) entrées, dont data/)."
    else
        echo "✘ $DERNIERE se déchiffre mais ne contient pas data/ : sauvegarde inutilisable." >&2
        exit 1
    fi
else
    echo "✘ $DERNIERE ne se déchiffre pas avec $CLE, ou l'archive est abîmée." >&2
    exit 1
fi
