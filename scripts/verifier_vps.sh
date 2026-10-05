#!/usr/bin/env bash
# shellcheck disable=SC1112 # apostrophes typographiques (’) voulues dans les messages
# Contrôle de sécurité du VPS, en LECTURE SEULE : ce script ne modifie rien.
#
#   cd ~/BinanceSpotManager/BinanceSpotManager
#   sudo bash scripts/verifier_vps.sh
#
# Une ligne par contrôle : ✔ bon, ⚠ à regarder, ✘ à corriger. À la fin : un résumé et, pour
# chaque ⚠ ou ✘, la commande à taper soi-même. Contrôles : configuration SSH effective
# (sshd -T), pare-feu ufw, fail2ban, mises à jour automatiques, ports publiés par Docker (Docker
# contourne ufw), droits des fichiers sensibles, sauvegardes chiffrées, ports en écoute (ss).
#
# Le fichier .env n'est JAMAIS lu : seuls ses droits et son propriétaire sont relevés (stat).
# Code de sortie : 0 si aucun ✘, 1 sinon. Guide pas à pas : docs/SECURITE_VPS.md.
#
# Règles d'écriture, vérifiées par tests/test_ops_scripts.py :
# - les commandes PROPOSÉES au propriétaire sont toujours entre apostrophes simples (ou dans un
#   format printf entre apostrophes) ; hors de ces chaînes, uniquement des commandes de lecture ;
# - les commandes système inspectées passent par « lancer » (sortie anglaise, durée bornée) ;
# - aucune redirection vers un fichier, aucun sudo ; les messages utilisent l'apostrophe ’.

if [ -z "${BASH_VERSION:-}" ]; then
    echo "Lancer avec bash : sudo bash scripts/verifier_vps.sh" >&2
    exit 2
fi
set -u

SCRIPT=${BASH_SOURCE[0]}
DOSSIER_SCRIPT=${SCRIPT%/*}
[ "$DOSSIER_SCRIPT" = "$SCRIPT" ] && DOSSIER_SCRIPT=.
PROJET=$(cd "$DOSSIER_SCRIPT/.." && pwd)
PROJET_COMPOSE=binance-spot-manager # « name: » de docker-compose.yml
FICHIER_ENV=$PROJET/.env            # jamais lu : stat seulement

# --- Affichage --------------------------------------------------------------------------------
if [ -t 1 ]; then
    VERT=$'\033[32m' JAUNE=$'\033[33m' ROUGE=$'\033[31m' GRAS=$'\033[1m' NORMAL=$'\033[0m'
else
    VERT='' JAUNE='' ROUGE='' GRAS='' NORMAL=''
fi
NB_BON=0
NB_ALERTE=0
NB_ERREUR=0
TITRES=()
COMMANDES=()

section() { printf '\n%s%s%s\n' "$GRAS" "$1" "$NORMAL"; }
detail() { printf '      %s\n' "$1"; }
info() { printf '  • %s\n' "$1"; }
retenir() { TITRES+=("$1"); COMMANDES+=("$2"); }
bon() {
    NB_BON=$((NB_BON + 1))
    printf '  %s✔%s %s\n' "$VERT" "$NORMAL" "$1"
}
alerte() {
    NB_ALERTE=$((NB_ALERTE + 1))
    printf '  %s⚠%s %s\n' "$JAUNE" "$NORMAL" "$1"
    retenir "⚠ $1" "${2:-}"
}
erreur() {
    NB_ERREUR=$((NB_ERREUR + 1))
    printf '  %s✘%s %s\n' "$ROUGE" "$NORMAL" "$1"
    retenir "✘ $1" "${2:-}"
}

existe() { command -v "$1" >/dev/null 2>&1; }
# Lecture seule, sortie anglaise stable (elle est analysée), 20 secondes au plus.
lancer() {
    if existe timeout; then LC_ALL=C timeout 20 "$@"; else LC_ALL=C "$@"; fi
}
dans_liste() {
    case " $2 " in *" $1 "*) return 0 ;; esac
    return 1
}
adresse_locale() {
    case $1 in 127.* | ::1 | ::ffff:127.* | localhost) return 0 ;; esac
    return 1
}

# --- En-tête ----------------------------------------------------------------------------------
RACINE=0
[ "$(id -u 2>/dev/null)" = 0 ] && RACINE=1
UTILISATEUR=${SUDO_USER:-}
[ -n "$UTILISATEUR" ] || UTILISATEUR=$(id -un 2>/dev/null)
PROPRIETAIRE=$(stat -c %U -- "$PROJET" 2>/dev/null)

printf '%sContrôle de sécurité du VPS%s — lecture seule : rien n’est modifié.\n' "$GRAS" "$NORMAL"
printf 'Projet : %s\n' "$PROJET"
if [ "$RACINE" -eq 0 ]; then
    alerte "Lancé sans sudo : SSH, pare-feu, fail2ban et ports ne sont pas tous lisibles" \
        'sudo bash scripts/verifier_vps.sh'
fi

# --- Collecte Docker (affichée plus bas) : l’accès public Caddy change les ports attendus ------
DOCKER_ETAT=absent
CONTENEURS=""
if existe docker; then
    if CONTENEURS=$(lancer docker ps -a --format '{{.Names}}|{{.State}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}|{{.Ports}}' 2>/dev/null); then
        DOCKER_ETAT=ok
    else
        DOCKER_ETAT=injoignable
    fi
fi
PUBLIC=0
if [ "$DOCKER_ETAT" = ok ] && printf '%s\n' "$CONTENEURS" |
    awk -F'|' -v p="$PROJET_COMPOSE" '$3 == p && $4 == "proxy" { vu = 1 } END { exit !vu }'; then
    PUBLIC=1
fi

# --- 1. SSH -----------------------------------------------------------------------------------
valeur_ssh() { # valeur d’une option dans la sortie de « sshd -T »
    printf '%s\n' "$CONFIG_SSH" | awk -v cle="$1" '$1 == cle { $1 = ""; sub(/^ +/, ""); print; exit }'
}

ou_est_reglee() { # fichiers de configuration qui règlent une option SSH (lecture seule)
    local lignes ligne
    lignes=$(grep -Hi "^[[:space:]]*$1[[:space:]]" /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null | head -n 3)
    if [ -z "$lignes" ]; then
        detail "(aucune ligne dans /etc/ssh/sshd_config ni sshd_config.d/ : valeur par défaut)"
        return
    fi
    while IFS= read -r ligne; do detail "réglé ici : $ligne"; done < <(printf '%s\n' "$lignes")
}

afficher_ssh() { # $1 = bon|erreur, $2 = texte, $3 = correction
    if [ "$1" = bon ]; then bon "$2"; else erreur "$2" "$3"; fi
}

controler_ssh() {
    local mdp kbd pam racine cle vide methodes cle_seule=0 lignes="" racine_conseil maison
    local etat_mdp texte_mdp option_mdp="" etat_racine texte_racine etat_cle texte_cle
    local correction="" etape2 corr
    mdp=$(valeur_ssh passwordauthentication)
    kbd=$(valeur_ssh kbdinteractiveauthentication)
    [ -n "$kbd" ] || kbd=$(valeur_ssh challengeresponseauthentication)
    pam=$(valeur_ssh usepam)
    racine=$(valeur_ssh permitrootlogin)
    cle=$(valeur_ssh pubkeyauthentication)
    vide=$(valeur_ssh permitemptypasswords)
    methodes=$(valeur_ssh authenticationmethods)
    PORTS_SSH=$(printf '%s\n' "$CONFIG_SSH" | awk '$1 == "port" { print $2 }' | sort -un | tr '\n' ' ')
    PORTS_SSH=${PORTS_SSH% }
    case $methodes in
        '' | any | *password* | *keyboard-interactive*) ;;
        *) cle_seule=1 ;;
    esac
    # Root : « no » quand on travaille avec un compte sudo, sinon « prohibit-password » (clé
    # seulement) pour ne pas se fermer la porte.
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        racine_conseil='PermitRootLogin no\n'
    else
        racine_conseil='PermitRootLogin prohibit-password\n'
    fi

    if [ "$cle_seule" -eq 1 ]; then
        etat_mdp=bon
        texte_mdp="Connexion par mot de passe impossible (AuthenticationMethods $methodes)"
    elif [ "$mdp" != no ]; then
        etat_mdp=erreur
        texte_mdp="Connexion SSH par mot de passe AUTORISÉE (PasswordAuthentication ${mdp:-?})"
        option_mdp=PasswordAuthentication
        lignes+='PasswordAuthentication no\nKbdInteractiveAuthentication no\n'
    elif [ "$kbd" = yes ] && [ "$pam" = yes ]; then
        etat_mdp=erreur
        texte_mdp="Mot de passe encore possible par PAM (KbdInteractiveAuthentication yes)"
        option_mdp=KbdInteractiveAuthentication
        lignes+='KbdInteractiveAuthentication no\n'
    else
        etat_mdp=bon
        texte_mdp="Connexion par mot de passe désactivée (PasswordAuthentication no)"
    fi

    case $racine in
        no)
            etat_racine=bon
            texte_racine="Connexion directe de root interdite (PermitRootLogin no)"
            ;;
        prohibit-password | without-password)
            etat_racine=bon
            texte_racine="root : connexion par clé seulement (PermitRootLogin $racine)"
            ;;
        forced-commands-only)
            etat_racine=bon
            texte_racine="root : commandes imposées seulement (PermitRootLogin forced-commands-only)"
            ;;
        *)
            etat_racine=erreur
            texte_racine="Connexion de root AUTORISÉE avec mot de passe (PermitRootLogin ${racine:-?})"
            lignes+=$racine_conseil
            ;;
    esac

    if [ "$cle" = yes ]; then
        etat_cle=bon
        texte_cle="Authentification par clé active (PubkeyAuthentication yes)"
    else
        etat_cle=erreur
        texte_cle="Authentification par clé DÉSACTIVÉE (PubkeyAuthentication ${cle:-?})"
        lignes+='PubkeyAuthentication yes\n'
    fi
    if [ "$vide" = yes ]; then lignes+='PermitEmptyPasswords no\n'; fi

    # Une seule correction pour tout SSH : un fichier lu en premier par sshd (00-…), complété
    # (tee -a) plutôt qu’écrasé, puis vérification de la syntaxe avant de redémarrer.
    if [ -n "$lignes" ]; then
        printf -v etape2 '2. printf "%s" | sudo tee -a /etc/ssh/sshd_config.d/00-securite.conf' "$lignes"
        correction='1. Garder cette session ouverte. Dans un 2e terminal, vérifier que la connexion par clé marche (sinon, depuis le PC : ssh-copy-id <utilisateur>@<adresse-du-vps>).'
        correction+=$'\n'"$etape2"
        correction+=$'\n''3. sudo sshd -t && sudo systemctl restart ssh'
        correction+=$'\n''4. Ouvrir une NOUVELLE connexion pour tester avant de fermer celle-ci (docs/SECURITE_VPS.md, partie SSH).'
    fi

    afficher_ssh "$etat_mdp" "$texte_mdp" "$correction"
    if [ -n "$option_mdp" ]; then ou_est_reglee "$option_mdp"; fi
    afficher_ssh "$etat_racine" "$texte_racine" "$correction"
    if [ "$etat_racine" = erreur ]; then ou_est_reglee PermitRootLogin; fi
    afficher_ssh "$etat_cle" "$texte_cle" "$correction"
    if [ "$vide" = yes ]; then
        erreur "Mots de passe vides acceptés (PermitEmptyPasswords yes)" "$correction"
    fi

    maison=""
    if [ -n "$UTILISATEUR" ] && existe getent; then
        maison=$(getent passwd "$UTILISATEUR" | cut -d: -f6)
    fi
    if [ -n "$maison" ]; then
        if [ -s "$maison/.ssh/authorized_keys" ]; then
            bon "Clé SSH installée pour $UTILISATEUR (~/.ssh/authorized_keys)"
        else
            printf -v corr 'Depuis le PC : ssh-copy-id %s@<adresse-du-vps>   puis tester : ssh %s@<adresse-du-vps>' "$UTILISATEUR" "$UTILISATEUR"
            alerte "Aucune clé SSH pour $UTILISATEUR (~/.ssh/authorized_keys vide ou absent) : l’installer AVANT de couper les mots de passe" "$corr"
        fi
    fi
    info "Port SSH : ${PORTS_SSH:-22}"
}

section "SSH"
PORTS_SSH=""
CONFIG_SSH=""
if ! existe sshd; then
    alerte "Commande sshd introuvable : configuration SSH non vérifiée" \
        'sudo bash scripts/verifier_vps.sh   (avec sudo, sshd est trouvé dans /usr/sbin)'
elif ! CONFIG_SSH=$(lancer sshd -T 2>&1); then
    alerte "Configuration SSH illisible (sshd -T) : lancer avec sudo" 'sudo bash scripts/verifier_vps.sh'
    detail "${CONFIG_SSH%%$'\n'*}"
else
    controler_ssh
fi

# --- 2. Pare-feu ------------------------------------------------------------------------------
ports_de_regle() { # colonne « To » de ufw → ports, « tout » ou « inconnu »
    local vers=${1% on *}
    case $vers in
        Anywhere*) echo tout ;;
        [0-9]*)
            vers=${vers%%/*}
            echo "${vers//,/ }"
            ;;
        OpenSSH) echo 22 ;;
        'Nginx Full' | 'WWW Full' | 'Apache Full') echo '80 443' ;;
        'Nginx HTTP' | WWW | Apache) echo 80 ;;
        'Nginx HTTPS' | 'WWW Secure' | 'Apache Secure') echo 443 ;;
        *) echo inconnu ;;
    esac
}

controler_regles_ufw() {
    local defaut regles vers depuis ports p supplement="" tout="" ssh_ouvert=0 web="" corr
    defaut=$(printf '%s\n' "$ETAT_UFW" | sed -n 's/^Default: \([a-z]*\) (incoming).*/\1/p')
    case $defaut in
        deny | reject) bon "Entrées refusées par défaut (Default: $defaut incoming)" ;;
        '') alerte "Politique par défaut du pare-feu illisible" 'sudo ufw status verbose' ;;
        *) erreur "Le pare-feu laisse TOUT entrer par défaut (Default: $defaut incoming)" 'sudo ufw default deny incoming' ;;
    esac

    regles=$(printf '%s\n' "$ETAT_UFW" | awk -F'  +' '
        /^--/ { tableau = 1; next }
        tableau && NF >= 2 && $2 ~ /^(ALLOW|LIMIT)/ {
            vers = $1; depuis = $3
            sub(/ \(v6\)$/, "", vers); sub(/ \(v6\)$/, "", depuis)
            if (depuis == "") depuis = "Anywhere"
            print vers "|" depuis
        }' | sort -u)
    if [ -z "$regles" ]; then detail "Aucune règle d’ouverture."; fi
    while IFS='|' read -r vers depuis; do
        [ -n "$vers" ] || continue
        detail "Règle : $vers ← $depuis"
        ports=$(ports_de_regle "$vers")
        case $ports in
            tout) tout+=" $depuis" ;;
            inconnu) supplement+=" « $vers »" ;;
            *)
                for p in $ports; do
                    if dans_liste "$p" "$PORTS_ATTENDUS"; then
                        if dans_liste "$p" "$PORTS_SSH_EFFECTIFS"; then ssh_ouvert=1; fi
                        case $p in 80 | 443) web+=" $p" ;; esac
                    elif [ "$depuis" = Anywhere ]; then
                        supplement+=" $p"
                    else
                        supplement+=" $p (depuis $depuis)"
                    fi
                done
                ;;
        esac
    done < <(printf '%s\n' "$regles")

    if [ -n "$tout" ]; then
        erreur "Une règle ouvre TOUS les ports (depuis :$tout)" \
            'sudo ufw status numbered   puis   sudo ufw delete <numéro>'
    fi
    if [ -n "$supplement" ]; then
        alerte "Ports ouverts en plus des ports attendus ($PORTS_ATTENDUS_TEXTE) :$supplement" \
            'sudo ufw status numbered   puis   sudo ufw delete <numéro>   (une règle à la fois ; relancer status numbered entre deux)'
    elif [ -z "$tout" ]; then
        bon "Seuls les ports attendus sont ouverts ($PORTS_ATTENDUS_TEXTE)"
    fi
    if [ "$ssh_ouvert" -eq 0 ] && [ -z "$tout" ] && [ "$defaut" != allow ]; then
        printf -v corr 'sudo ufw allow %s/tcp' "$PORT_SSH_PRINCIPAL"
        erreur "SSH (port $PORTS_SSH_EFFECTIFS) n’est pas autorisé par ufw : la prochaine connexion sera refusée" "$corr"
    fi
    if [ "$PUBLIC" -eq 1 ] && [ -z "$web" ]; then
        detail "80/443 absents des règles : l’accès public marche quand même (Docker publie ces ports sans passer par ufw)."
    fi
}

section "Pare-feu (ufw)"
PORTS_SSH_EFFECTIFS=${PORTS_SSH:-22}
PORT_SSH_PRINCIPAL=${PORTS_SSH_EFFECTIFS%% *}
PORTS_ATTENDUS=$PORTS_SSH_EFFECTIFS
PORTS_ATTENDUS_TEXTE="SSH $PORTS_SSH_EFFECTIFS"
if [ "$PUBLIC" -eq 1 ]; then
    PORTS_ATTENDUS+=" 80 443"
    PORTS_ATTENDUS_TEXTE+=", 80 et 443 pour l’accès public Caddy"
fi
printf -v CORRECTION_UFW 'sudo ufw allow %s/tcp      (SSH d’abord, sinon la connexion sera coupée)' "$PORT_SSH_PRINCIPAL"
if [ "$PUBLIC" -eq 1 ]; then CORRECTION_UFW+=$'\n''sudo ufw allow 80,443/tcp   (accès public Caddy)'; fi
CORRECTION_UFW+=$'\n''sudo ufw enable'
ETAT_UFW=""
if ! existe ufw; then
    erreur "ufw n’est pas installé : aucun pare-feu sur ce serveur" $'sudo apt install ufw\n'"$CORRECTION_UFW"
elif ! ETAT_UFW=$(lancer ufw status verbose 2>&1); then
    alerte "État du pare-feu illisible (ufw status) : lancer avec sudo" 'sudo bash scripts/verifier_vps.sh'
elif ! printf '%s\n' "$ETAT_UFW" | grep -q '^Status: active'; then
    erreur "Pare-feu ufw INACTIF : rien ne filtre les connexions entrantes" "$CORRECTION_UFW"
else
    bon "Pare-feu ufw actif"
    controler_regles_ufw
fi

# --- 3. fail2ban ------------------------------------------------------------------------------
section "Protection contre les essais de connexion (fail2ban)"
if ! existe fail2ban-client; then
    erreur "fail2ban n’est pas installé" $'sudo apt install fail2ban\nsudo systemctl enable --now fail2ban'
elif [ "$RACINE" -eq 0 ]; then
    alerte "fail2ban installé, état illisible sans sudo" 'sudo bash scripts/verifier_vps.sh'
else
    ACTIF_F2B=inconnu
    if existe systemctl; then ACTIF_F2B=$(lancer systemctl is-active fail2ban 2>/dev/null); fi
    if [ "$ACTIF_F2B" != active ] && [ "$ACTIF_F2B" != inconnu ]; then
        erreur "fail2ban est installé mais ne tourne pas (${ACTIF_F2B:-?})" 'sudo systemctl enable --now fail2ban'
    elif PRISON=$(lancer fail2ban-client status sshd 2>/dev/null); then
        BANNIES=$(printf '%s\n' "$PRISON" | awk -F':' '/Currently banned/ { gsub(/[ \t]/, "", $2); print $2 }')
        TOTAL=$(printf '%s\n' "$PRISON" | awk -F':' '/Total banned/ { gsub(/[ \t]/, "", $2); print $2 }')
        bon "fail2ban actif, prison sshd en place (${BANNIES:-0} adresse(s) bannie(s) en ce moment, ${TOTAL:-0} depuis son démarrage)"
    else
        alerte "fail2ban tourne mais sans prison sshd" \
            'Voir docs/SECURITE_VPS.md, partie fail2ban (fichier /etc/fail2ban/jail.local)'
    fi
fi

# --- 4. Mises à jour --------------------------------------------------------------------------
section "Mises à jour automatiques"
if ! existe dpkg-query; then
    alerte "dpkg-query introuvable : mises à jour non vérifiées (système autre qu’Ubuntu ?)" ''
else
    # shellcheck disable=SC2016 # ${Status} est un champ de dpkg-query, pas une variable
    PAQUET_UU=$(lancer dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null)
    if [ "$PAQUET_UU" != "install ok installed" ]; then
        erreur "unattended-upgrades n’est pas installé : les correctifs de sécurité ne s’installent pas seuls" \
            $'sudo apt install unattended-upgrades\nsudo dpkg-reconfigure -plow unattended-upgrades   (répondre « Oui » ou « Yes »)'
    else
        PERIODE_UU=""
        if existe apt-config; then
            PERIODE_UU=$(lancer apt-config dump 2>/dev/null |
                awk '$1 == "APT::Periodic::Unattended-Upgrade" { gsub(/[";]/, "", $2); v = $2 } END { print v }')
        fi
        case $PERIODE_UU in
            '' | 0)
                erreur "unattended-upgrades installé mais DÉSACTIVÉ (APT::Periodic::Unattended-Upgrade ${PERIODE_UU:-absent})" \
                    'sudo dpkg-reconfigure -plow unattended-upgrades   (répondre « Oui » ou « Yes »)'
                ;;
            *) bon "unattended-upgrades installé et activé" ;;
        esac
        if existe systemctl; then
            MINUTEUR=$(lancer systemctl is-active apt-daily-upgrade.timer 2>/dev/null)
            if [ "$MINUTEUR" != active ]; then
                alerte "Minuteur apt-daily-upgrade arrêté (${MINUTEUR:-?}) : les mises à jour automatiques ne se lancent pas" \
                    'sudo systemctl enable --now apt-daily.timer apt-daily-upgrade.timer'
            fi
        fi
    fi
fi
if ! existe apt-get; then
    alerte "apt-get introuvable : mises à jour en attente non comptées" ''
elif SIMULATION=$(lancer apt-get -s -o Debug::NoLocking=1 dist-upgrade 2>/dev/null); then
    # Simulation (-s) : apt calcule ce qu’il ferait, sans rien installer ni verrouiller.
    EN_ATTENTE=$(printf '%s\n' "$SIMULATION" | grep -c '^Inst ')
    SECURITE=$(printf '%s\n' "$SIMULATION" | grep '^Inst ' | grep -c -- '-security')
    if [ "$SECURITE" -gt 0 ]; then
        alerte "$SECURITE mise(s) à jour de sécurité en attente (sur $EN_ATTENTE en tout)" \
            'sudo apt update && sudo apt upgrade'
    elif [ "$EN_ATTENTE" -gt 0 ]; then
        bon "Aucune mise à jour de sécurité en attente ($EN_ATTENTE autre(s) disponible(s))"
    else
        bon "Aucune mise à jour en attente"
    fi
    LISTES=$(stat -c %y /var/lib/apt/periodic/update-success-stamp 2>/dev/null | cut -c1-16)
    if [ -n "$LISTES" ]; then detail "D’après les listes de paquets du $LISTES."; fi
else
    alerte "Simulation des mises à jour impossible (apt-get -s)" 'sudo apt update'
fi
if [ -e /var/run/reboot-required ]; then
    PAQUETS=""
    if [ -r /var/run/reboot-required.pkgs ]; then
        PAQUETS=$(sort -u /var/run/reboot-required.pkgs | tr '\n' ' ')
        PAQUETS=${PAQUETS% }
    fi
    alerte "Redémarrage du serveur nécessaire pour finir des mises à jour${PAQUETS:+ ($PAQUETS)}" \
        'sudo reboot   (le bot repart tout seul ; choisir un moment calme, puis vérifier : make ps)'
else
    bon "Aucun redémarrage en attente"
fi

# --- 5. Docker --------------------------------------------------------------------------------
controler_docker() {
    local publications nom projet service hote port interne cle corr
    local ui_locale=0 ui_publique="" exposes=0 ports_proxy=""
    local -A vus=()
    # Une ligne par port publié : conteneur|projet|service|adresse|port|port interne
    publications=$(printf '%s\n' "$CONTENEURS" | awk -F'|' '
        {
            n = split($5, morceaux, /, */)
            for (i = 1; i <= n; i++) {
                m = morceaux[i]; fleche = index(m, "->")
                if (fleche == 0) continue
                gauche = substr(m, 1, fleche - 1); droite = substr(m, fleche + 2)
                if (!match(gauche, /:[0-9-]+$/)) continue
                hote = substr(gauche, 1, RSTART - 1); port = substr(gauche, RSTART + 1)
                gsub(/\[|\]/, "", hote)
                print $1 "|" $3 "|" $4 "|" hote "|" port "|" droite
            }
        }')
    while IFS='|' read -r nom projet service hote port interne; do
        [ -n "$nom" ] || continue
        if adresse_locale "$hote"; then
            if [ "$projet" = "$PROJET_COMPOSE" ] && [ "$service" = ui ] && [ "$interne" = 8501/tcp ]; then
                ui_locale=1
            fi
            continue
        fi
        if [ "$projet" = "$PROJET_COMPOSE" ] && [ "$service" = proxy ] && dans_liste "$port" "80 443"; then
            dans_liste "$port" "$ports_proxy" || ports_proxy+=" $port"
            continue
        fi
        cle="$nom|$port|$interne"
        [ -z "${vus[$cle]:-}" ] || continue
        vus[$cle]=1
        exposes=$((exposes + 1))
        if [ "$projet" = "$PROJET_COMPOSE" ] && [ "$service" = ui ]; then
            ui_publique="$hote:$port"
            # shellcheck disable=SC2016 # texte affiché tel quel
            erreur "Interface BSM publiée sur Internet ($hote:$port) : aucune protection, et ufw ne la filtre pas" \
                'Dans docker-compose.yml, « ui » doit publier "127.0.0.1:${BSM_UI_PORT:-8501}:8501" ; puis : make up'
        else
            printf -v corr 'Dans le docker-compose.yml de %s, publier ce port sur 127.0.0.1 (« 127.0.0.1:%s:… »), puis : docker compose up -d' "$nom" "$port"
            erreur "$nom publie le port $port sur $hote : ouvert à Internet (Docker contourne ufw)" "$corr"
        fi
    done < <(printf '%s\n' "$publications")

    if [ -z "$ui_publique" ]; then
        if [ "$ui_locale" -eq 1 ]; then
            bon "Interface BSM (8501) publiée sur 127.0.0.1 uniquement"
        else
            alerte "Conteneur « ui » de BSM arrêté ou introuvable : interface non vérifiée" 'make ps   (puis make up si besoin)'
        fi
    fi
    if [ "$PUBLIC" -eq 1 ]; then
        if [ -n "$ports_proxy" ]; then
            info "Accès public Caddy utilisé : port(s)$ports_proxy publiés par le proxy (HTTPS + mot de passe)"
        else
            info "Accès public Caddy configuré, proxy arrêté en ce moment"
        fi
    else
        info "Accès public Caddy non utilisé"
    fi
    if [ "$exposes" -eq 0 ]; then bon "Aucun autre port publié sur Internet par Docker"; fi
}

section "Docker (rappel : Docker publie ses ports en contournant ufw)"
case $DOCKER_ETAT in
    absent) alerte "Docker introuvable : ports publiés non vérifiés" '' ;;
    injoignable) alerte "Docker injoignable (docker ps) : lancer avec sudo" 'sudo bash scripts/verifier_vps.sh' ;;
    *) controler_docker ;;
esac

# --- 6. Droits --------------------------------------------------------------------------------
verifier_droits() { # $1 = chemin, $2 = nom affiché, $3 = obligatoire|facultatif. Jamais lu.
    local chemin=$1 nom=$2 releve mode proprio corrections="" ligne
    if [ ! -e "$chemin" ]; then
        if [ "$3" = obligatoire ]; then
            alerte "$nom absent : normal seulement si les clés sont saisies dans l’interface (Settings → Sécurité)" ''
        fi
        return
    fi
    if ! releve=$(stat -L -c '%a %U' -- "$chemin" 2>/dev/null); then
        alerte "Droits de $nom illisibles" 'sudo bash scripts/verifier_vps.sh'
        return
    fi
    mode=${releve%% *}
    proprio=${releve#* }
    case $mode in
        '' | *[!0-7]*)
            alerte "Droits de $nom illisibles ($releve)" ''
            return
            ;;
    esac
    if [ -n "$PROPRIETAIRE" ] && [ "$proprio" != "$PROPRIETAIRE" ]; then
        printf -v corrections 'sudo chown %s: %s' "$PROPRIETAIRE" "$chemin"
    fi
    if [ $((8#$mode & 8#077)) -ne 0 ]; then
        printf -v ligne 'chmod 600 %s' "$chemin"
        if [ -n "$corrections" ]; then corrections+=$'\n'; fi
        corrections+=$ligne
        erreur "$nom lisible par d’autres comptes du serveur (droits $mode, attendu 600)" "$corrections"
    elif [ -n "$corrections" ] && [ "$proprio" = root ]; then
        alerte "$nom appartient à root (attendu : $PROPRIETAIRE) : make et docker compose risquent de ne pas le lire" "$corrections"
    elif [ -n "$corrections" ]; then
        erreur "$nom appartient à $proprio (attendu : $PROPRIETAIRE, propriétaire du projet)" "$corrections"
    else
        bon "$nom : droits $mode, propriétaire $proprio"
    fi
}

section "Droits des fichiers sensibles (relevés avec stat, contenu jamais lu)"
verifier_droits "$FICHIER_ENV" .env obligatoire
verifier_droits "$PROJET/deploy/users.caddy" deploy/users.caddy facultatif

# --- 7. Sauvegardes ---------------------------------------------------------------------------
section "Sauvegardes"
CLE_SAUVEGARDE=$PROJET/deploy/sauvegarde.age.pub
if [ ! -e "$CLE_SAUVEGARDE" ]; then
    alerte "Sauvegardes chiffrées pas encore en place (deploy/sauvegarde.age.pub absent)" \
        'Voir docs/SECURITE_VPS.md, partie « Sauvegardes chiffrées »'
elif grep -q 'AGE-SECRET-KEY-' "$CLE_SAUVEGARDE" 2>/dev/null; then
    erreur "deploy/sauvegarde.age.pub contient une CLÉ PRIVÉE : elle ne doit jamais être sur le VPS" \
        'Supprimer ce fichier du VPS, créer une nouvelle paire de clés sur le PC et n’envoyer que la ligne publique age1… (docs/SECURITE_VPS.md)'
else
    bon "Clé publique de sauvegarde en place (deploy/sauvegarde.age.pub)"
fi
DERNIERE=""
for f in "$PROJET"/backups/bsm-*.tar.gz.age; do
    if [ -e "$f" ]; then DERNIERE=$f; fi
done
if [ -z "$DERNIERE" ]; then
    alerte "Aucune sauvegarde chiffrée dans backups/" 'make backup-chiffre   (puis la planifier : docs/SECURITE_VPS.md)'
else
    MTIME=$(stat -c %Y -- "$DERNIERE" 2>/dev/null)
    MAINTENANT=$(date +%s)
    case $MTIME in
        '' | *[!0-9]*) alerte "Date de la dernière sauvegarde chiffrée illisible" '' ;;
        *)
            HEURES=$(((MAINTENANT - MTIME) / 3600))
            if [ "$HEURES" -le 36 ]; then
                bon "Dernière sauvegarde chiffrée : ${DERNIERE##*/} (il y a $HEURES h)"
            else
                alerte "Dernière sauvegarde chiffrée il y a $((HEURES / 24)) jour(s) : la sauvegarde planifiée tourne-t-elle ?" \
                    'crontab -l   (la ligne « make backup-chiffre » doit y être), puis à la main : make backup-chiffre'
            fi
            ;;
    esac
fi
CLAIRES=0
for f in "$PROJET"/backups/bsm-*.tar.gz; do
    if [ -e "$f" ]; then CLAIRES=$((CLAIRES + 1)); fi
done
if [ "$CLAIRES" -gt 0 ]; then
    alerte "$CLAIRES archive(s) NON chiffrée(s) dans backups/ (anciennes « make backup »)" \
        'Une fois une sauvegarde chiffrée récupérée et vérifiée sur le PC : rm backups/bsm-*.tar.gz'
fi

# --- 8. Ports en écoute -----------------------------------------------------------------------
controler_ecoute() {
    local resume portee port processus locaux=""
    # Une ligne par port : local|public, port, programme (« ? » sans sudo)
    resume=$(printf '%s\n' "$ECOUTE" | awk '
        {
            adr = $4
            if (!match(adr, /:[0-9]+$/)) next
            port = substr(adr, RSTART + 1); adr = substr(adr, 1, RSTART - 1)
            sub(/%.*/, "", adr); gsub(/\[|\]/, "", adr)
            prog = "?"
            if (match($0, /users:\(\("[^"]+"/)) prog = substr($0, RSTART + 9, RLENGTH - 10)
            portee = (adr ~ /^127\./ || adr == "::1" || adr ~ /^::ffff:127\./) ? "local" : "public"
            print portee "|" port "|" prog
        }' | sort -t'|' -k1,1 -k2,2n -u)
    while IFS='|' read -r portee port processus; do
        [ -n "$port" ] || continue
        if [ "$portee" = local ]; then
            locaux+=" $port ($processus)"
            continue
        fi
        case $port in
            2375 | 2376)
                erreur "API Docker ouverte sur le réseau (port $port, $processus) : prise de contrôle totale du serveur possible" \
                    'Retirer « -H tcp://… » de la configuration de Docker (/etc/docker/daemon.json ou son service), puis : sudo systemctl restart docker'
                continue
                ;;
        esac
        if dans_liste "$port" "$PORTS_SSH_EFFECTIFS"; then
            bon "Port $port ouvert : SSH ($processus)"
        elif [ "$processus" = docker-proxy ]; then
            if [ "$PUBLIC" -eq 1 ] && dans_liste "$port" "80 443"; then
                bon "Port $port ouvert : accès public Caddy (docker-proxy)"
            else
                detail "Port $port ouvert par Docker : voir la partie Docker ci-dessus."
            fi
        else
            alerte "Port $port ouvert sur toutes les adresses ($processus) : à vérifier (ufw le bloque s’il est actif)" \
                'sudo ss -tlnp   (repérer le programme, puis l’arrêter ou le limiter à 127.0.0.1)'
        fi
    done < <(printf '%s\n' "$resume")
    if [ -n "$locaux" ]; then detail "Seulement sur la machine (127.0.0.1) :$locaux"; fi
}

section "Ports en écoute (ss -tlnp)"
if ! existe ss; then
    alerte "Commande ss introuvable : ports en écoute non listés" ''
elif ECOUTE=$(lancer ss -H -tlnp 2>/dev/null) || ECOUTE=$(lancer ss -tlnp 2>/dev/null | tail -n +2); then
    controler_ecoute
else
    alerte "Ports en écoute illisibles (ss)" 'sudo ss -tlnp'
fi

# --- Résumé -----------------------------------------------------------------------------------
printf '\n%sRésumé%s : %d ✔ bon · %d ⚠ à regarder · %d ✘ à corriger\n' \
    "$GRAS" "$NORMAL" "$NB_BON" "$NB_ALERTE" "$NB_ERREUR"
if [ "$RACINE" -eq 0 ]; then
    printf 'Contrôle incomplet sans sudo. Relancer : sudo bash scripts/verifier_vps.sh\n'
fi
if [ "${#TITRES[@]}" -gt 0 ]; then
    printf '\n%sÀ faire%s (commandes à taper toi-même ; ce script n’a rien modifié) :\n' "$GRAS" "$NORMAL"
    PRECEDENTE=""
    for i in "${!TITRES[@]}"; do
        printf '\n  %s\n' "${TITRES[i]}"
        if [ -z "${COMMANDES[i]}" ]; then continue; fi
        if [ "${COMMANDES[i]}" = "$PRECEDENTE" ]; then
            printf '      (même correction que ci-dessus)\n'
            continue
        fi
        printf '%s\n' "${COMMANDES[i]}" | while IFS= read -r ligne; do printf '      %s\n' "$ligne"; done
        PRECEDENTE=${COMMANDES[i]}
    done
fi
printf '\nGuide pas à pas : docs/SECURITE_VPS.md\n'
if [ "$NB_ERREUR" -gt 0 ]; then exit 1; fi
exit 0
