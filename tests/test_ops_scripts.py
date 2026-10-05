"""Scripts d'exploitation du VPS : contrôle en lecture seule, sauvegardes chiffrées, récupération.

Rien ne touche au vrai système, à Docker ni au réseau :
- le contrôle (scripts/verifier_vps.sh) tourne avec un PATH réduit au strict nécessaire, où
  sshd, ufw, docker, ss… sont des doublures qui notent chaque appel ; son .env est un tube
  nommé, qui bloquerait le script s'il était ouvert en lecture ;
- la sauvegarde (scripts/sauvegarde_chiffree.sh) est chargée par `source` et ses fonctions
  Docker sont remplacées ; age est le vrai s'il est installé (cycle chiffrer/déchiffrer
  complet), sinon une doublure qui ne sert qu'à tester l'enchaînement ;
- la récupération (scripts/recuperer_sauvegardes.sh) parle à des doublures de rsync et scp.
Les exécutions demandent bash (Linux) ; sous Windows, seules les vérifications de texte tournent.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verifier_vps.sh"
SAUVEGARDE = ROOT / "scripts" / "sauvegarde_chiffree.sh"
RECUPERER = ROOT / "scripts" / "recuperer_sauvegardes.sh"
SCRIPTS = [VERIFIER, SAUVEGARDE, RECUPERER]
DOC = ROOT / "docs" / "SECURITE_VPS.md"

POSIX = os.name != "nt"
BASH = shutil.which("bash") if POSIX else None
AGE = shutil.which("age") if POSIX else None
AGE_KEYGEN = shutil.which("age-keygen") if POSIX else None
SHELLCHECK = shutil.which("shellcheck") if POSIX else None
besoin_bash = pytest.mark.skipif(BASH is None, reason="bash sous Linux requis pour exécuter les scripts")
besoin_age = pytest.mark.skipif(not (BASH and AGE and AGE_KEYGEN),
                                reason="age non installé : cycle réel chiffrer/déchiffrer non testé")


def lire(chemin: Path) -> str:
    return chemin.read_text(encoding="utf-8").replace("\r\n", "\n")


# --------------------------------------------------------------------------------------------
# Lecture du code bash : ce qui est exécuté, sans commentaires ni texte affiché
# --------------------------------------------------------------------------------------------

HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n.*?^[ \t]*\2[ \t]*$", re.MULTILINE | re.DOTALL)


def code_executable(texte: str, *, garder_chaines: bool = False) -> str:
    """Le script sans commentaires ni documents intégrés (<<FIN) et, sauf `garder_chaines`, sans
    le texte des chaînes : '…', $'…' et texte littéral des "…". Les substitutions $(…) restent,
    même entre guillemets. Suffisant pour ces scripts : pas de `case` dans un $(…), pas
    d'apostrophe droite dans un message (les messages utilisent ’)."""
    texte = HEREDOC.sub("<<FIN", texte)
    sortie: list[str] = []
    pile: list[list] = [["code", 0]]
    i, n = 0, len(texte)
    while i < n:
        c = texte[i]
        contexte = pile[-1]
        if contexte[0] == "guillemets":
            if c == "\\":
                if garder_chaines:
                    sortie.append(texte[i:i + 2])
                i += 2
            elif c == '"':
                pile.pop()
                sortie.append(c)
                i += 1
            elif texte.startswith("$(", i):
                pile.append(["code", 1])
                sortie.append("$(")
                i += 2
            else:
                if garder_chaines:
                    sortie.append(c)
                i += 1
            continue
        if c == "\\":
            sortie.append(texte[i:i + 2])
            i += 2
            continue
        if c == "#" and (i == 0 or texte[i - 1] in " \t\n;|&("):
            fin = texte.find("\n", i)
            i = n if fin < 0 else fin
            continue
        if c == "'" or texte.startswith("$'", i):
            ansi = c == "$"
            j = i + (2 if ansi else 1)
            while texte[j] != "'":
                j += 2 if ansi and texte[j] == "\\" else 1
            sortie.append(texte[i:j + 1] if garder_chaines else "''")
            i = j + 1
            continue
        if c == '"':
            pile.append(["guillemets", 0])
            sortie.append(c)
            i += 1
            continue
        if texte.startswith("$(", i):
            pile.append(["code", 1])
            sortie.append("$(")
            i += 2
            continue
        if len(pile) > 1 and c == "(":
            contexte[1] += 1
        elif len(pile) > 1 and c == ")":
            contexte[1] -= 1
            if contexte[1] == 0:
                pile.pop()
                sortie.append(c)
                i += 1
                continue
        sortie.append(c)
        i += 1
    return "".join(sortie)


#: Premier mot d'une commande : début de ligne, après ; & | ( { ! $( ou un mot-clé.
COMMANDE = re.compile(r"(?:^|[;&|({!]|\$\(|\b(?:then|do|else|elif|if|while|until)\b)[ \t]*([A-Za-z_][\w.+-]*)",
                      re.MULTILINE)
REDIRECTION = re.compile(r"(\d*)(>>?|&>)[ \t]*(&?[^\s;|&)]*)")


def test_lecture_du_code_bash_ignore_texte_et_garde_les_substitutions():
    code = code_executable(
        "# rm -rf /\n"
        "alerte \"n’efface rien ; rm reste du texte\" 'sudo rm -rf /tmp/x'\n"
        "x=\"$(lancer docker ps 2>/dev/null)\"  # commentaire\n"
        "y=$'a\\'b' ; echo ${#TITRES[@]} 8#077\n"
    )
    assert "rm" not in code and "sudo" not in code
    assert "lancer docker ps 2>/dev/null" in code
    assert "${#TITRES[@]} 8#077" in code
    assert [m.group(1) for m in COMMANDE.finditer(code)][:3] == ["alerte", "x", "lancer"]


# --------------------------------------------------------------------------------------------
# Vérifications de texte (toutes plateformes)
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_bash_robustes(script):
    texte = lire(script)
    assert texte.startswith("#!/usr/bin/env bash\n")
    code = code_executable(texte)
    assert re.search(r"^set -u", code, re.MULTILINE), "set -u attendu"
    # Pas de « set -e » global : un contrôle en échec ne doit pas couper les suivants.
    assert not re.search(r"^set -[a-z]*e", code, re.MULTILINE)


def test_documentation_lance_les_scripts_par_bash_ou_make():
    doc, readme, makefile = lire(DOC), lire(ROOT / "README.md"), lire(ROOT / "Makefile")
    assert "sudo bash scripts/verifier_vps.sh" in doc and "sudo bash scripts/verifier_vps.sh" in readme
    assert "bash scripts/recuperer_sauvegardes.sh" in doc
    assert re.search(r"^backup-chiffre: .*## ", makefile, re.MULTILINE)
    assert re.search(r"^\t@?COMPOSE=.* bash scripts/sauvegarde_chiffree\.sh --garder", makefile, re.MULTILINE)
    phony = re.search(r"^\.PHONY:(.*?)(?:\n(?!\s))", makefile, re.MULTILINE | re.DOTALL).group(1)
    assert "backup-chiffre" in phony.split()


@pytest.mark.skipif(not POSIX, reason="bit d'exécution POSIX")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_executables(script):
    assert os.access(script, os.X_OK), f"chmod +x {script.relative_to(ROOT)}"


SECRETS = [
    re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{20,}"),                       # clé privée age
    re.compile(r"\bage1[02-9ac-hj-np-z]{58}\b"),                         # vraie clé publique age
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b"),                    # jeton de bot Telegram
    re.compile(r"\b[A-Za-z0-9]{64}\b"),                                   # clé ou secret API Binance
    re.compile(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}"),                    # empreinte bcrypt
    re.compile(r"(?i)\b(api_?key|secret|password|token)\s*=\s*['\"]?[A-Za-z0-9+/_-]{12,}"),
]


@pytest.mark.parametrize("chemin", [*SCRIPTS, DOC], ids=lambda p: p.name)
def test_aucun_secret_dans_les_scripts_et_la_doc(chemin):
    texte = lire(chemin)
    for motif in SECRETS:
        assert not motif.search(texte), f"{chemin.name} : {motif.pattern}"


#: Commandes qui écrivent, suppriment, élèvent les droits ou téléchargent.
ECRITURES = {"rm", "rmdir", "mv", "cp", "dd", "tee", "touch", "mkdir", "mkfifo", "chmod", "chown", "chgrp",
             "ln", "truncate", "shred", "install", "sudo", "su", "kill", "pkill", "killall", "reboot",
             "shutdown", "poweroff", "halt", "crontab", "passwd", "useradd", "usermod", "userdel",
             "iptables", "ip6tables", "nft", "curl", "wget", "apt", "snap", "tar", "mount", "umount",
             "eval", "source", "exec", "trap", "ssh", "scp", "rsync", "age"}
#: Outils système inspectés : seulement par « lancer », seulement ces sous-commandes de lecture.
LECTURES_AUTORISEES = {"sshd": {"-T"}, "ufw": {"status"}, "systemctl": {"is-active", "is-enabled"},
                       "fail2ban-client": {"status"}, "docker": {"ps"}, "ss": {"-H", "-tlnp"},
                       "apt-get": {"-s"}, "apt-config": {"dump"}, "dpkg-query": {"-W"}}


def test_verifier_vps_ne_lance_que_des_lectures():
    code = code_executable(lire(VERIFIER))
    lances = [m.group(1) for m in COMMANDE.finditer(code)]
    assert not set(lances) & ECRITURES, sorted(set(lances) & ECRITURES)
    assert not set(lances) & set(LECTURES_AUTORISEES), "outil système appelé sans « lancer »"
    assert not re.search(r"(?:^|[;&|])[ \t]*\.[ \t]+\S", code, re.MULTILINE), "source par « . »"
    assert not re.search(r"\bsed\b[^\n|;]*\s(-[a-zA-Z]*i\b|--in-place)", code)
    assert not re.search(r"-(delete|exec|execdir|fprint)\b", code)
    appels = re.findall(r"\blancer[ \t]+([\w.-]+)[ \t]*([^\s;|&)]*)", code)
    assert {outil for outil, _ in appels} == set(LECTURES_AUTORISEES)
    for outil, premier in appels:
        assert premier in LECTURES_AUTORISEES[outil], f"lancer {outil} {premier}"
    for redirection in REDIRECTION.finditer(code):
        cible = redirection.group(3)
        assert cible == "/dev/null" or cible.startswith("&"), f"écriture vers {cible!r}"


def test_verifier_vps_ne_lit_jamais_le_fichier_env():
    texte = code_executable(lire(VERIFIER), garder_chaines=True)
    lignes = [ligne.strip() for ligne in texte.splitlines() if ".env" in ligne or "FICHIER_ENV" in ligne]
    assert lignes == ["FICHIER_ENV=$PROJET/.env", 'verifier_droits "$FICHIER_ENV" .env obligatoire']
    corps = re.search(r"^verifier_droits\(\) \{(.*?)^\}", texte, re.MULTILINE | re.DOTALL).group(1)
    usages = [ligne.strip() for ligne in corps.splitlines() if "$chemin" in ligne]
    assert usages, "le chemin doit être relevé"
    for ligne in usages:
        assert re.search(r'\[ ! -e "\$chemin" \]|\bstat -L -c .* "\$chemin"|^printf -v \w+ \'[^\']*\' .*"\$chemin"',
                         ligne), f"usage non autorisé du chemin : {ligne}"


def test_sauvegarde_chiffre_dans_un_tube_sans_archive_en_clair():
    texte = lire(SAUVEGARDE)
    code = code_executable(texte, garder_chaines=True)
    assert re.search(r'^\s*produire_archive \| age -R "\$CLE_PUBLIQUE" -o "\$PARTIEL"$', code, re.MULTILINE)
    archive = re.search(r"^produire_archive\(\) \{(.*?)\}$", code, re.MULTILINE).group(1)
    assert "tar czf - -C /app data logs" in archive          # vers la sortie standard
    assert "worker" in archive and "run --rm --no-deps -T" in archive
    assert not re.search(r"\.tar\.gz(?!\.age)\b", re.sub(r"bsm-\*\.tar\.gz\.age", "", code))
    assert not re.search(r"\bage\b[^\n|]*\s(-d|--decrypt|-i|--identity)\b", code), "jamais de déchiffrement sur le VPS"
    assert re.search(r'^\s*PARTIEL="\$DOSSIER/\.\$nom\.partiel"', code, re.MULTILINE)
    assert re.search(r"^umask 077$", code, re.MULTILINE)
    assert "bsm-keys" not in code_executable(texte)           # le volume de la clé maîtresse n'est pas copié


def test_recuperation_ne_supprime_rien():
    code = code_executable(lire(RECUPERER), garder_chaines=True)
    assert "--delete" not in code and "--remove-source-files" not in code
    assert not ({m.group(1) for m in COMMANDE.finditer(code_executable(lire(RECUPERER)))} & {"rm", "rmdir", "mv"})
    assert re.search(r"--include=\"\$MOTIF\" --exclude='\*'", code)
    assert "MOTIF='bsm-*.tar.gz.age'" in code


def test_cle_publique_hors_git_et_hors_image():
    assert "deploy/sauvegarde.age.pub" in lire(ROOT / ".gitignore").split()
    assert "deploy/sauvegarde.age.pub" in lire(ROOT / ".dockerignore").split()


def test_documentation_couvre_le_parcours_complet():
    doc = lire(DOC)
    for attendu in ("sudo bash scripts/verifier_vps.sh", "age-keygen -o ~/.config/bsm-sauvegarde.key",
                    "age-keygen -y ~/.config/bsm-sauvegarde.key", "make backup-chiffre", "30 3 * * *",
                    "bash scripts/recuperer_sauvegardes.sh", "age -d -i", "make import-data",
                    "bot_stop.flag", "PasswordAuthentication no", "sudo ufw enable", "fail2ban",
                    "unattended-upgrades"):
        assert attendu in doc, attendu
    assert re.search(r"jamais.{0,80}clé privée.{0,40}VPS|clé privée.{0,80}jamais.{0,40}VPS", doc, re.IGNORECASE | re.DOTALL)
    readme = lire(ROOT / "README.md")
    assert "make backup-chiffre" in readme and "docs/SECURITE_VPS.md" in readme


# --------------------------------------------------------------------------------------------
# Exécution : outils communs
# --------------------------------------------------------------------------------------------

@besoin_bash
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_syntaxe_bash(script):
    resultat = subprocess.run([BASH, "-n", str(script)], check=False, capture_output=True, encoding="utf-8")
    assert resultat.returncode == 0, resultat.stderr


@pytest.mark.skipif(SHELLCHECK is None, reason="shellcheck non installé")
def test_shellcheck():
    resultat = subprocess.run([SHELLCHECK, "-S", "warning", *map(str, SCRIPTS)], check=False,
                              capture_output=True, encoding="utf-8")
    assert resultat.returncode == 0, resultat.stdout


DOUBLURE = """#!@PYTHON@
import json, os, sys
nom = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ["JOURNAL_APPELS"], "a", encoding="utf-8") as journal:
    journal.write(json.dumps([nom, *args]) + "\\n")
with open(os.environ["REPONSES_DOUBLURES"], encoding="utf-8") as fichier:
    reponses = json.load(fichier)
for regle in reponses.get(nom, []):
    if args[:len(regle["args"])] == regle["args"]:
        sys.stdout.write(regle.get("sortie", ""))
        sys.exit(regle.get("code", 0))
sys.stderr.write(nom + " : appel inattendu " + " ".join(args) + "\\n")
sys.exit(97)
"""


def ecrire_executable(chemin: Path, contenu: str) -> None:
    chemin.write_text(contenu.replace("@PYTHON@", sys.executable), encoding="utf-8")
    chemin.chmod(0o755)


def dossier_bin(tmp_path: Path, utilitaires: list[str], doublures: list[str] = ()) -> Path:
    """PATH réduit : les seuls utilitaires nommés (liens vers les vrais) et des doublures."""
    dossier = tmp_path / "bin"
    dossier.mkdir(exist_ok=True)
    for nom in utilitaires:
        vrai = shutil.which(nom)
        if vrai and not (dossier / nom).exists():
            (dossier / nom).symlink_to(vrai)
    for nom in doublures:
        ecrire_executable(dossier / nom, DOUBLURE)
    return dossier


def instantane(dossier: Path) -> dict:
    """État de l'arborescence (sans ouvrir aucun fichier : le .env est un tube nommé)."""
    etat = {}
    for racine, _, fichiers in os.walk(dossier):
        for nom in [*fichiers, "."]:
            infos = os.lstat(os.path.join(racine, nom))
            etat[os.path.relpath(os.path.join(racine, nom), dossier)] = (infos.st_mode, infos.st_size, infos.st_mtime_ns)
    return etat


def utilisateur_courant() -> str:
    import pwd  # POSIX seulement

    return pwd.getpwuid(os.getuid()).pw_name


# --------------------------------------------------------------------------------------------
# Contrôle du VPS
# --------------------------------------------------------------------------------------------

UTILITAIRES_VERIFIER = ["awk", "cut", "date", "getent", "grep", "head", "sed", "sort", "stat", "tail", "timeout", "tr"]
OUTILS_SYSTEME = ["id", "sshd", "ufw", "systemctl", "fail2ban-client", "docker", "ss", "dpkg-query", "apt-config", "apt-get"]

SSHD_BON = ("port 22\npermitrootlogin no\npubkeyauthentication yes\npasswordauthentication no\n"
            "kbdinteractiveauthentication no\nusepam yes\npermitemptypasswords no\nauthenticationmethods any\n")
UFW_BON = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), deny (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
80,443/tcp                 ALLOW IN    Anywhere
22/tcp (v6)                ALLOW IN    Anywhere (v6)
80,443/tcp (v6)            ALLOW IN    Anywhere (v6)
"""
DOCKER_BON = "\n".join([
    "binance-spot-manager-ui-1|running|binance-spot-manager|ui|127.0.0.1:8501->8501/tcp",
    "binance-spot-manager-worker-1|running|binance-spot-manager|worker|8501/tcp",
    ("binance-spot-manager-proxy-1|running|binance-spot-manager|proxy|0.0.0.0:80->80/tcp, [::]:80->80/tcp, "
     "0.0.0.0:443->443/tcp, 0.0.0.0:443->443/udp, [::]:443->443/tcp, [::]:443->443/udp"),
    "crypto-signal-intelligence-api-1|running|crypto-signal-intelligence|api|127.0.0.1:8503->8503/tcp",
]) + "\n"
SS_BON = "\n".join([
    'LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=600,fd=15))',
    'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1000,fd=3))',
    'LISTEN 0 128 [::]:22 [::]:* users:(("sshd",pid=1000,fd=4))',
    'LISTEN 0 4096 127.0.0.1:8501 0.0.0.0:* users:(("docker-proxy",pid=2000,fd=4))',
    'LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("docker-proxy",pid=2100,fd=4))',
    'LISTEN 0 4096 [::]:443 [::]:* users:(("docker-proxy",pid=2102,fd=4))',
]) + "\n"
FAIL2BAN_SSHD = ("Status for the jail: sshd\n|- Filter\n|  |- Currently failed:\t0\n|  |- Total failed:\t12\n"
                 "|  `- File list:\t/var/log/auth.log\n`- Actions\n   |- Currently banned:\t1\n"
                 "   |- Total banned:\t4\n   `- Banned IP list:\t203.0.113.7\n")


def reponses_serveur_bien_regle(utilisateur: str) -> dict:
    return {
        "id": [{"args": ["-u"], "sortie": "0\n"}, {"args": ["-un"], "sortie": utilisateur + "\n"}],
        "sshd": [{"args": ["-T"], "sortie": SSHD_BON}],
        "ufw": [{"args": ["status"], "sortie": UFW_BON}],
        "systemctl": [{"args": ["is-active"], "sortie": "active\n"}],
        "fail2ban-client": [{"args": ["status", "sshd"], "sortie": FAIL2BAN_SSHD}],
        "docker": [{"args": ["ps"], "sortie": DOCKER_BON}],
        "ss": [{"args": ["-H", "-tlnp"], "sortie": SS_BON}],
        "dpkg-query": [{"args": ["-W"], "sortie": "install ok installed"}],
        "apt-config": [{"args": ["dump"], "sortie": 'APT::Periodic::Update-Package-Lists "1";\n'
                                                   'APT::Periodic::Unattended-Upgrade "1";\n'}],
        "apt-get": [{"args": ["-s"], "sortie": "NOTE: This is only a simulation!\n"}],
    }


def reponses_serveur_mal_regle(utilisateur: str) -> dict:
    reponses = reponses_serveur_bien_regle(utilisateur)
    reponses["sshd"][0]["sortie"] = (SSHD_BON.replace("passwordauthentication no", "passwordauthentication yes")
                                     .replace("permitrootlogin no", "permitrootlogin yes"))
    reponses["ufw"][0]["sortie"] = "Status: inactive\n"
    reponses["docker"][0]["sortie"] = ("binance-spot-manager-ui-1|running|binance-spot-manager|ui|"
                                       "0.0.0.0:8501->8501/tcp, :::8501->8501/tcp\n"
                                       "redis-1|running|autre|redis|0.0.0.0:6379->6379/tcp\n")
    reponses["ss"][0]["sortie"] = SS_BON + "\n".join([
        'LISTEN 0 4096 0.0.0.0:2375 0.0.0.0:* users:(("dockerd",pid=900,fd=9))',
        'LISTEN 0 511 0.0.0.0:3000 0.0.0.0:* users:(("node",pid=3000,fd=20))',
    ]) + "\n"
    reponses["dpkg-query"][0]["sortie"] = "deinstall ok config-files"
    reponses["apt-get"][0]["sortie"] = (
        "Inst libssl3t64 [3.0.13-0ubuntu3.4] (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-updates, "
        "Ubuntu:24.04/noble-security [amd64])\nInst vim [2:9.1.0016] (2:9.1.0016-1ubuntu7.2 Ubuntu:24.04/noble-updates [amd64])\n")
    return reponses


def projet_a_verifier(tmp_path: Path, *, droits_env: int = 0o600) -> Path:
    projet = tmp_path / "projet"
    for dossier in ("scripts", "deploy", "backups"):
        (projet / dossier).mkdir(parents=True)
    shutil.copy2(VERIFIER, projet / "scripts" / "verifier_vps.sh")
    os.mkfifo(projet / ".env")                        # l'ouvrir en lecture bloquerait le script
    os.chmod(projet / ".env", droits_env)
    utilisateurs = projet / "deploy" / "users.caddy"
    utilisateurs.write_text("imad (empreinte factice)\n", encoding="utf-8")
    utilisateurs.chmod(0o600)
    (projet / "deploy" / "sauvegarde.age.pub").write_text("age1" + "q" * 58 + "\n", encoding="utf-8")
    recente = datetime.now(timezone.utc).strftime("bsm-%Y%m%d-%H%M%S.tar.gz.age")
    (projet / "backups" / recente).write_bytes(b"age-encryption.org/v1\n")
    return projet


def lancer_verifier(tmp_path: Path, projet: Path, reponses: dict, *, doublures=OUTILS_SYSTEME):
    bin_ = dossier_bin(tmp_path, UTILITAIRES_VERIFIER, doublures)
    journal, fichier_reponses = tmp_path / "appels.jsonl", tmp_path / "reponses.json"
    journal.write_text("", encoding="utf-8")
    fichier_reponses.write_text(json.dumps(reponses), encoding="utf-8")
    env = {"PATH": str(bin_), "HOME": str(tmp_path), "LANG": "C.UTF-8",
           "JOURNAL_APPELS": str(journal), "REPONSES_DOUBLURES": str(fichier_reponses)}
    avant = instantane(projet)
    # Groupe de processus à part : en cas de blocage, tout le groupe est arrêté (aucun orphelin).
    processus = subprocess.Popen([BASH, str(projet / "scripts" / "verifier_vps.sh")], cwd=projet, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                                 start_new_session=True)
    try:
        sortie, erreurs = processus.communicate(timeout=60)
    except subprocess.TimeoutExpired:  # pragma: no cover - seulement si le script lit .env
        os.killpg(processus.pid, 9)
        processus.communicate()
        pytest.fail("verifier_vps.sh bloqué : il a ouvert le .env (tube nommé) au lieu d'un simple stat")
    resultat = subprocess.CompletedProcess(processus.args, processus.returncode, sortie, erreurs)
    assert instantane(projet) == avant, "le contrôle a modifié des fichiers du projet"
    appels = [json.loads(ligne) for ligne in journal.read_text(encoding="utf-8").splitlines()]
    return resultat, appels


def appel_en_lecture(appel: list[str]) -> bool:
    nom, *args = appel
    regles = {
        "id": lambda a: a in (["-u"], ["-un"]),
        "sshd": lambda a: a == ["-T"],
        "ufw": lambda a: a[:1] == ["status"],
        "systemctl": lambda a: a[:1] in (["is-active"], ["is-enabled"]),
        "fail2ban-client": lambda a: a[:1] == ["status"],
        "docker": lambda a: a[:1] == ["ps"],
        "ss": lambda a: a in (["-H", "-tlnp"], ["-tlnp"]),
        "dpkg-query": lambda a: a[:1] == ["-W"],
        "apt-config": lambda a: a == ["dump"],
        "apt-get": lambda a: "-s" in a,
    }
    return regles[nom](args)


@besoin_bash
def test_verifier_vps_serveur_bien_regle(tmp_path):
    projet = projet_a_verifier(tmp_path)
    resultat, appels = lancer_verifier(tmp_path, projet, reponses_serveur_bien_regle(utilisateur_courant()))
    sortie = resultat.stdout

    assert resultat.returncode == 0, sortie + resultat.stderr
    assert not re.search(r"^\s*✘", sortie, re.MULTILINE) and "· 0 ✘ à corriger" in sortie
    assert resultat.stderr == ""
    for attendu in ("✔ Connexion par mot de passe désactivée", "✔ Connexion directe de root interdite",
                    "✔ Authentification par clé active", "✔ Pare-feu ufw actif", "✔ Entrées refusées par défaut",
                    "✔ Seuls les ports attendus sont ouverts (SSH 22, 80 et 443",
                    "✔ fail2ban actif, prison sshd en place (1 adresse(s) bannie(s)",
                    "✔ unattended-upgrades installé et activé", "✔ Aucune mise à jour en attente",
                    "✔ Interface BSM (8501) publiée sur 127.0.0.1 uniquement",
                    "✔ Aucun autre port publié sur Internet par Docker", "✔ .env : droits 600",
                    "✔ deploy/users.caddy : droits 600", "✔ Clé publique de sauvegarde en place",
                    "✔ Dernière sauvegarde chiffrée : bsm-", "✔ Port 22 ouvert : SSH (sshd)",
                    "✔ Port 80 ouvert : accès public Caddy", "Règle : 22/tcp ← Anywhere", "Port SSH : 22",
                    "Résumé :"):
        assert attendu in sortie, attendu
    assert appels and all(appel_en_lecture(appel) for appel in appels), appels
    assert {appel[0] for appel in appels} == set(OUTILS_SYSTEME)


@besoin_bash
def test_verifier_vps_serveur_mal_regle(tmp_path):
    projet = projet_a_verifier(tmp_path, droits_env=0o644)
    doublures = [nom for nom in OUTILS_SYSTEME if nom != "fail2ban-client"]   # fail2ban absent
    resultat, appels = lancer_verifier(tmp_path, projet, reponses_serveur_mal_regle(utilisateur_courant()),
                                       doublures=doublures)
    sortie = resultat.stdout

    assert resultat.returncode == 1, sortie + resultat.stderr
    for attendu in ("✘ Connexion SSH par mot de passe AUTORISÉE (PasswordAuthentication yes)",
                    "✘ Connexion de root AUTORISÉE avec mot de passe (PermitRootLogin yes)",
                    "✘ Pare-feu ufw INACTIF", "✘ fail2ban n’est pas installé",
                    "✘ unattended-upgrades n’est pas installé",
                    "⚠ 1 mise(s) à jour de sécurité en attente (sur 2 en tout)",
                    "✘ Interface BSM publiée sur Internet (0.0.0.0:8501)",
                    "✘ redis-1 publie le port 6379 sur 0.0.0.0", "✘ API Docker ouverte sur le réseau (port 2375",
                    "⚠ Port 3000 ouvert sur toutes les adresses (node)",
                    "✘ .env lisible par d’autres comptes du serveur (droits 644, attendu 600)"):
        assert attendu in sortie, attendu
    assert sortie.split("Résumé")[0].count("✘ Interface BSM publiée") == 1   # IPv4 et IPv6 : une fois
    a_faire = sortie.split("À faire", 1)[1]
    # Corrections proposées, jamais appliquées : SSH autorisé AVANT d'activer le pare-feu.
    assert a_faire.index("sudo ufw allow 22/tcp") < a_faire.index("sudo ufw enable")
    assert ('printf "PasswordAuthentication no\\nKbdInteractiveAuthentication no\\n'
            'PermitRootLogin prohibit-password\\n" | sudo tee -a /etc/ssh/sshd_config.d/00-securite.conf') in a_faire
    assert "sudo sshd -t && sudo systemctl restart ssh" in a_faire
    assert f"chmod 600 {projet / '.env'}" in a_faire
    assert "sudo apt install fail2ban" in a_faire and "sudo apt install unattended-upgrades" in a_faire
    assert all(appel_en_lecture(appel) for appel in appels), appels


@besoin_bash
def test_verifier_vps_sans_sudo_ni_outils_ne_plante_pas(tmp_path):
    projet = projet_a_verifier(tmp_path)
    reponses = {"id": [{"args": ["-u"], "sortie": "1000\n"}, {"args": ["-un"], "sortie": utilisateur_courant() + "\n"}]}
    resultat, appels = lancer_verifier(tmp_path, projet, reponses, doublures=["id"])
    sortie = resultat.stdout

    assert "command not found" not in resultat.stderr and "introuvable" not in resultat.stderr
    assert resultat.returncode == 1                     # ufw, fail2ban, unattended-upgrades absents
    for attendu in ("⚠ Lancé sans sudo", "⚠ Commande sshd introuvable", "✘ ufw n’est pas installé",
                    "⚠ Docker introuvable", "⚠ Commande ss introuvable", "⚠ dpkg-query introuvable",
                    "Contrôle incomplet sans sudo", "Résumé :"):
        assert attendu in sortie, attendu
    assert all(appel[0] == "id" for appel in appels)


# --------------------------------------------------------------------------------------------
# Sauvegarde chiffrée
# --------------------------------------------------------------------------------------------

UTILITAIRES_SAUVEGARDE = ["cut", "date", "du", "flock", "grep", "gzip", "mkdir", "mv", "rm", "tar", "tr"]
AGE_FACTICE = """#!@PYTHON@
# Doublure de age : « chiffre » en base64 (enchaînement seulement, aucune sécurité).
import base64, json, os, sys
args = sys.argv[1:]
with open(os.environ["JOURNAL_AGE"], "a", encoding="utf-8") as journal:
    journal.write(json.dumps(args) + "\\n")
sortie = args[args.index("-o") + 1]
with open(sortie, "wb") as fichier:
    fichier.write(b"age-encryption.org/v1\\n-> X25519 factice\\n---\\n" + base64.b64encode(sys.stdin.buffer.read()))
"""
CANARI = "CANARI-SAUVEGARDE-7f3a91"


def q(chemin: Path | str) -> str:
    return shlex.quote(str(chemin))


def projet_a_sauvegarder(tmp_path: Path, *, cle_publique: str | None = "age1" + "q" * 58 + "\n"):
    projet = tmp_path / "projet"
    for dossier in ("scripts", "deploy"):
        (projet / dossier).mkdir(parents=True)
    shutil.copy2(SAUVEGARDE, projet / "scripts" / "sauvegarde_chiffree.sh")
    if cle_publique is not None:
        (projet / "deploy" / "sauvegarde.age.pub").write_text(cle_publique, encoding="utf-8")
    volumes = tmp_path / "volumes"
    (volumes / "data" / "positions").mkdir(parents=True)
    (volumes / "data" / "positions" / "pos1.json").write_text(json.dumps({"note": CANARI}), encoding="utf-8")
    (volumes / "logs").mkdir()
    (volumes / "logs" / "bot.log").write_text("journal\n", encoding="utf-8")
    return projet, volumes


def bin_sauvegarde(tmp_path: Path, *, age: str = "factice") -> Path:
    bin_ = dossier_bin(tmp_path, UTILITAIRES_SAUVEGARDE)
    if age == "factice":
        ecrire_executable(bin_ / "age", AGE_FACTICE)
    elif age == "vrai":
        (bin_ / "age").symlink_to(AGE)
    return bin_


def lancer_sauvegarde(tmp_path, projet, volumes, bin_, *args, worker_en_marche=True, copie_ok=True):
    """Charge le script par `source`, remplace ses fonctions Docker, puis lance principal."""
    journal = tmp_path / "worker.txt"
    script = f"""
source {q(projet / "scripts" / "sauvegarde_chiffree.sh")}
worker_en_marche() {{ return {0 if worker_en_marche else 1}; }}
arreter_worker() {{ echo arret >> {q(journal)}; }}
relancer_worker() {{ echo relance >> {q(journal)}; }}
produire_archive() {{ tar czf - -C {q(volumes)} data logs{"" if copie_ok else "; return 3"}; }}
principal "$@"
"""
    env = {"PATH": str(bin_), "HOME": str(tmp_path), "JOURNAL_AGE": str(tmp_path / "age.jsonl")}
    resultat = subprocess.run([BASH, "-c", script, "sauvegarde", *args], cwd=tmp_path, env=env, check=False,
                              capture_output=True, encoding="utf-8", timeout=60)
    actions = journal.read_text(encoding="utf-8").split() if journal.exists() else []
    return resultat, actions


def sauvegardes(projet: Path) -> list[str]:
    """Contenu de backups/, hors fichier de verrou (absent si flock n'est pas installé)."""
    dossier = projet / "backups"
    if not dossier.exists():
        return []
    return sorted(p.name for p in dossier.iterdir() if p.name != ".sauvegarde.lock")


def tourner(tmp_path: Path, dossier: Path, garder: str):
    script = f"source {q(SAUVEGARDE)}; tourner_sauvegardes \"$1\" \"$2\""
    return subprocess.run([BASH, "-c", script, "rotation", str(dossier), garder], check=False,
                          env={"PATH": str(dossier_bin(tmp_path, UTILITAIRES_SAUVEGARDE))},
                          capture_output=True, encoding="utf-8", timeout=60)


@besoin_bash
def test_rotation_garde_les_plus_recentes_et_rien_d_autre(tmp_path):
    dossier = tmp_path / "backups"
    dossier.mkdir()
    chiffrees = [f"bsm-202609{jour:02d}-033000.tar.gz.age" for jour in range(1, 21)]
    autres = ["bsm-20260901-033000.tar.gz", "notes.txt", "bsm-pas-une-date.tar.gz.age",
              ".bsm-20261005-033000.tar.gz.age.partiel", "bsm-20260930-033000.tar.gz.age.bak"]
    for nom in [*chiffrees, *autres]:
        (dossier / nom).write_bytes(b"x")

    resultat = tourner(tmp_path, dossier, "14")

    assert resultat.returncode == 0, resultat.stderr
    assert sorted(p.name for p in dossier.iterdir()) == sorted([*chiffrees[6:], *autres])
    assert resultat.stdout.count("Ancienne sauvegarde supprimée") == 6
    assert "conservées : 14" in resultat.stdout
    assert tourner(tmp_path, dossier, "014").returncode == 0       # 014 = 14, pas l'octal 12
    assert len([p for p in dossier.iterdir() if p.name in chiffrees]) == 14


@besoin_bash
@pytest.mark.parametrize("garder", ["0", "abc", "-3", "", "1.5"])
def test_rotation_refuse_un_nombre_invalide(tmp_path, garder):
    dossier = tmp_path / "backups"
    dossier.mkdir()
    for jour in range(1, 4):
        (dossier / f"bsm-202609{jour:02d}-033000.tar.gz.age").write_bytes(b"x")

    resultat = tourner(tmp_path, dossier, garder)

    assert resultat.returncode == 1 and "Refusé" in resultat.stderr
    assert len(list(dossier.iterdir())) == 3


@besoin_bash
def test_sauvegarde_enchainement_complet(tmp_path):
    projet, volumes = projet_a_sauvegarder(tmp_path)
    backups = projet / "backups"
    backups.mkdir()
    anciennes = [f"bsm-2026090{jour}-033000.tar.gz.age" for jour in range(1, 5)]
    for nom in [*anciennes, "bsm-20260901-033000.tar.gz"]:            # + une archive de make backup
        (backups / nom).write_bytes(b"ancienne")
    avant = instantane(projet)

    resultat, actions = lancer_sauvegarde(tmp_path, projet, volumes, bin_sauvegarde(tmp_path), "--garder", "3")

    assert resultat.returncode == 0, resultat.stdout + resultat.stderr
    assert actions == ["arret", "relance"]
    nouvelles = [nom for nom in sauvegardes(projet) if nom.endswith(".age") and nom not in anciennes]
    assert len(nouvelles) == 1 and re.fullmatch(r"bsm-\d{8}-\d{6}\.tar\.gz\.age", nouvelles[0])
    assert sauvegardes(projet) == sorted(["bsm-20260901-033000.tar.gz", *anciennes[2:], nouvelles[0]])
    contenu = (backups / nouvelles[0]).read_bytes()
    assert contenu.startswith(b"age-encryption.org/v1\n") and CANARI.encode() not in contenu
    assert oct((backups / nouvelles[0]).stat().st_mode & 0o777) == "0o600"
    appel_age = json.loads((tmp_path / "age.jsonl").read_text(encoding="utf-8"))
    assert appel_age[:2] == ["-R", str(projet / "deploy" / "sauvegarde.age.pub")]
    assert appel_age[2] == "-o" and Path(appel_age[3]).name.startswith(".bsm-") and appel_age[3].endswith(".partiel")
    # Hors de backups/, rien n'a changé dans le projet (aucune archive en clair nulle part).
    apres = instantane(projet)
    modifies = {chemin for chemin in apres if apres[chemin] != avant.get(chemin)}
    assert modifies <= {"backups", "backups/.sauvegarde.lock", f"backups/{nouvelles[0]}"}
    assert "Sauvegarde chiffrée : backups/bsm-" in resultat.stdout


@besoin_bash
@pytest.mark.parametrize("cas, cle, age, message", [
    ("age absent", "age1" + "q" * 58 + "\n", "absent", "sudo apt install age"),
    ("clé absente", None, "factice", "clé publique absente"),
    ("clé privée", "# public key: age1" + "q" * 58 + "\n" + "AGE-SECRET-KEY-1" + "Q" * 58 + "\n", "factice", "CLÉ PRIVÉE"),
    ("clé illisible", "bonjour\n", "factice", "aucune clé publique age valide"),
])
def test_sauvegarde_refusee_avant_d_arreter_le_worker(tmp_path, cas, cle, age, message):
    projet, volumes = projet_a_sauvegarder(tmp_path, cle_publique=cle)

    resultat, actions = lancer_sauvegarde(tmp_path, projet, volumes, bin_sauvegarde(tmp_path, age=age))

    assert resultat.returncode == 1, cas
    assert message in resultat.stderr, resultat.stderr
    assert actions == []                                   # worker jamais arrêté
    assert not [nom for nom in sauvegardes(projet) if ".age" in nom]


@besoin_bash
def test_sauvegarde_refuse_garder_zero_sans_rien_faire(tmp_path):
    projet, volumes = projet_a_sauvegarder(tmp_path)
    resultat, actions = lancer_sauvegarde(tmp_path, projet, volumes, bin_sauvegarde(tmp_path), "--garder", "0")
    assert resultat.returncode == 1 and "au moins une sauvegarde" in resultat.stderr
    assert actions == [] and sauvegardes(projet) == []


@besoin_bash
def test_sauvegarde_relance_le_worker_si_la_copie_echoue(tmp_path):
    projet, volumes = projet_a_sauvegarder(tmp_path)

    resultat, actions = lancer_sauvegarde(tmp_path, projet, volumes, bin_sauvegarde(tmp_path), copie_ok=False)

    assert resultat.returncode == 1
    assert "copie des données échouée (code 3)" in resultat.stderr
    assert actions == ["arret", "relance"]
    assert sauvegardes(projet) == []                        # ni sauvegarde ni fichier partiel


@besoin_bash
def test_sauvegarde_ne_relance_pas_un_worker_deja_arrete(tmp_path):
    projet, volumes = projet_a_sauvegarder(tmp_path)

    resultat, actions = lancer_sauvegarde(tmp_path, projet, volumes, bin_sauvegarde(tmp_path),
                                          worker_en_marche=False)

    assert resultat.returncode == 0, resultat.stderr
    assert actions == [] and "Worker déjà arrêté" in resultat.stdout
    assert len([nom for nom in sauvegardes(projet) if nom.endswith(".tar.gz.age")]) == 1


DOCKER_FACTICE = """#!@PYTHON@
# Doublure de docker : note les appels ; « compose run … tar » lit un faux /app.
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["JOURNAL_DOCKER"], "a", encoding="utf-8") as journal:
    journal.write(json.dumps(args) + "\\n")
if args[:2] == ["compose", "ps"]:
    print("3f2a9c")
elif args[:2] == ["compose", "run"]:
    i = args.index("tar")
    commande = ["tar"] + [os.environ["VOLUMES_FACTICES"] if a == "/app" else a for a in args[i + 1:]]
    sys.exit(subprocess.run(commande).returncode)
"""


@besoin_bash
def test_sauvegarde_lancee_directement_commandes_docker_exactes(tmp_path):
    projet, volumes = projet_a_sauvegarder(tmp_path)
    bin_ = bin_sauvegarde(tmp_path)
    ecrire_executable(bin_ / "docker", DOCKER_FACTICE)
    journal = tmp_path / "docker.jsonl"
    env = {"PATH": str(bin_), "HOME": str(tmp_path), "JOURNAL_AGE": str(tmp_path / "age.jsonl"),
           "JOURNAL_DOCKER": str(journal), "VOLUMES_FACTICES": str(volumes)}

    resultat = subprocess.run([BASH, str(projet / "scripts" / "sauvegarde_chiffree.sh"), "--garder", "2"],
                              cwd=tmp_path, env=env, check=False, capture_output=True, encoding="utf-8", timeout=60)

    assert resultat.returncode == 0, resultat.stdout + resultat.stderr
    assert [json.loads(ligne) for ligne in journal.read_text(encoding="utf-8").splitlines()] == [
        ["compose", "ps", "-q", "--status", "running", "worker"],
        ["compose", "stop", "worker"],
        ["compose", "run", "--rm", "--no-deps", "-T", "worker", "tar", "czf", "-", "-C", "/app", "data", "logs"],
        ["compose", "start", "worker"],
    ]
    assert len([nom for nom in sauvegardes(projet) if nom.endswith(".tar.gz.age")]) == 1


def generer_cle_age(tmp_path: Path) -> tuple[Path, str]:
    cle = tmp_path / "pc" / "bsm-sauvegarde.key"
    cle.parent.mkdir()
    subprocess.run([AGE_KEYGEN, "-o", str(cle)], check=True, capture_output=True)
    publique = subprocess.run([AGE_KEYGEN, "-y", str(cle)], check=True, capture_output=True, encoding="utf-8").stdout
    return cle, publique


@besoin_age
def test_cycle_reel_chiffrer_puis_dechiffrer_avec_age(tmp_path):
    cle, publique = generer_cle_age(tmp_path)
    projet, volumes = projet_a_sauvegarder(tmp_path, cle_publique=publique)

    resultat, actions = lancer_sauvegarde(tmp_path, projet, volumes, bin_sauvegarde(tmp_path, age="vrai"))

    assert resultat.returncode == 0, resultat.stderr
    assert actions == ["arret", "relance"]
    [nom] = [nom for nom in sauvegardes(projet) if nom.endswith(".tar.gz.age")]
    chiffre = (projet / "backups" / nom).read_bytes()
    assert chiffre.startswith(b"age-encryption.org/v1\n") and CANARI.encode() not in chiffre
    with pytest.raises(subprocess.CalledProcessError):         # illisible sans la clé privée
        subprocess.run(["tar", "tzf", str(projet / "backups" / nom)], check=True, capture_output=True)
    restauration = tmp_path / "restauration"
    restauration.mkdir()
    subprocess.run(f"{q(AGE)} -d -i {q(cle)} {q(projet / 'backups' / nom)} | tar xzf - -C {q(restauration)}",
                   shell=True, check=True, executable=BASH)
    assert CANARI in (restauration / "data" / "positions" / "pos1.json").read_text(encoding="utf-8")
    assert (restauration / "logs" / "bot.log").read_text(encoding="utf-8") == "journal\n"


# --------------------------------------------------------------------------------------------
# Récupération sur le PC
# --------------------------------------------------------------------------------------------

RSYNC_FACTICE = """#!@PYTHON@
# Doublure de rsync : copie depuis un dossier local qui joue le VPS ; note ses arguments.
import fnmatch, json, os, shutil, sys
args = sys.argv[1:]
with open(os.environ["JOURNAL_COPIE"], "a", encoding="utf-8") as journal:
    journal.write(json.dumps(["rsync", *args]) + "\\n")
code = int(os.environ.get("CODE_RSYNC", "0"))
if code:
    sys.exit(code)
motif = next(a.split("=", 1)[1] for a in args if a.startswith("--include="))
source, destination = os.environ["VPS_FACTICE"], args[-1]
for nom in sorted(os.listdir(source)):
    if fnmatch.fnmatch(nom, motif) and not os.path.exists(os.path.join(destination, nom)):
        shutil.copy2(os.path.join(source, nom), destination)
        print(">f+++++++++ " + nom)
"""
SCP_FACTICE = """#!@PYTHON@
import fnmatch, json, os, shutil, sys
args = sys.argv[1:]
with open(os.environ["JOURNAL_COPIE"], "a", encoding="utf-8") as journal:
    journal.write(json.dumps(["scp", *args]) + "\\n")
motif = args[-2].rsplit("/", 1)[1]
for nom in sorted(os.listdir(os.environ["VPS_FACTICE"])):
    if fnmatch.fnmatch(nom, motif):
        shutil.copy2(os.path.join(os.environ["VPS_FACTICE"], nom), args[-1])
"""
UTILITAIRES_RECUPERER = ["grep", "gzip", "mkdir", "tar", "wc"]


def vps_factice(tmp_path: Path, fichiers: dict[str, bytes]) -> Path:
    vps = tmp_path / "vps-backups"
    vps.mkdir()
    for nom, contenu in fichiers.items():
        (vps / nom).write_bytes(contenu)
    return vps


def lancer_recuperation(tmp_path, vps, *args, rsync=True, code_rsync=0, age=False, extra_env=None):
    bin_ = dossier_bin(tmp_path, UTILITAIRES_RECUPERER)
    if rsync:
        ecrire_executable(bin_ / "rsync", RSYNC_FACTICE)
    ecrire_executable(bin_ / "scp", SCP_FACTICE)
    if age and not (bin_ / "age").exists():
        (bin_ / "age").symlink_to(AGE)
    maison = tmp_path / "maison"
    maison.mkdir(exist_ok=True)
    env = {"PATH": str(bin_), "HOME": str(maison), "VPS_FACTICE": str(vps),
           "JOURNAL_COPIE": str(tmp_path / "copie.jsonl"), "CODE_RSYNC": str(code_rsync), **(extra_env or {})}
    resultat = subprocess.run([BASH, str(RECUPERER), *args], env=env, check=False, capture_output=True,
                              encoding="utf-8", timeout=60)
    journal = tmp_path / "copie.jsonl"
    appels = [json.loads(ligne) for ligne in journal.read_text(encoding="utf-8").splitlines()] if journal.exists() else []
    return resultat, appels, maison


@besoin_bash
def test_recuperation_par_rsync_sans_rien_supprimer(tmp_path):
    vps = vps_factice(tmp_path, {"bsm-20261004-033000.tar.gz.age": b"a", "bsm-20261005-033000.tar.gz.age": b"b",
                                 "bsm-20261001-033000.tar.gz": b"clair", ".sauvegarde.lock": b""})

    resultat, appels, maison = lancer_recuperation(tmp_path, vps, "csi@vps.exemple")

    assert resultat.returncode == 0, resultat.stderr
    destination = maison / "sauvegardes-bsm"
    assert sorted(p.name for p in destination.iterdir()) == ["bsm-20261004-033000.tar.gz.age",
                                                              "bsm-20261005-033000.tar.gz.age"]
    assert oct(destination.stat().st_mode & 0o777) == "0o700"
    assert len(list(vps.iterdir())) == 4                           # rien supprimé « sur le VPS »
    [appel] = appels
    assert appel[0] == "rsync" and "--include=bsm-*.tar.gz.age" in appel and "--exclude=*" in appel
    assert "csi@vps.exemple:BinanceSpotManager/BinanceSpotManager/backups/" in appel
    assert not [a for a in appel if a.startswith("--delete") or a == "--remove-source-files"]
    assert "2 nouvelle(s) sauvegarde(s)" in resultat.stdout and "Vérification sautée" in resultat.stdout

    resultat, _, _ = lancer_recuperation(tmp_path, vps, "csi@vps.exemple")    # deuxième passage
    assert resultat.returncode == 0 and "0 nouvelle(s) sauvegarde(s) ; 2 en tout" in resultat.stdout


@besoin_bash
@pytest.mark.parametrize("rsync, code_rsync", [(False, 0), (True, 12)], ids=["rsync absent", "rsync absent du VPS"])
def test_recuperation_repli_sur_scp(tmp_path, rsync, code_rsync):
    vps = vps_factice(tmp_path, {"bsm-20261005-033000.tar.gz.age": b"b"})

    resultat, appels, _ = lancer_recuperation(tmp_path, vps, "csi@vps.exemple", str(tmp_path / "copies"),
                                                   rsync=rsync, code_rsync=code_rsync,
                                                   extra_env={"DOSSIER_DISTANT": "bsm/backups"})

    assert resultat.returncode == 0, resultat.stderr
    assert appels[-1] == ["scp", "-p", "csi@vps.exemple:bsm/backups/bsm-*.tar.gz.age", f"{tmp_path / 'copies'}/"]
    assert [p.name for p in (tmp_path / "copies").iterdir()] == ["bsm-20261005-033000.tar.gz.age"]


@besoin_bash
@pytest.mark.parametrize("args, env, code", [
    ([], {}, 2),
    (["-oProxyCommand=evil"], {}, 2),
    (["csi@vps"], {"DOSSIER_DISTANT": "backups;rm -rf ~"}, 2),
    (["csi@vps"], {"CODE_RSYNC": "23"}, 1),
])
def test_recuperation_refus_et_echecs(tmp_path, args, env, code):
    vps = vps_factice(tmp_path, {"bsm-20261005-033000.tar.gz.age": b"b"})
    env = dict(env)
    code_rsync = int(env.pop("CODE_RSYNC", "0"))

    resultat, appels, _ = lancer_recuperation(tmp_path, vps, *args, code_rsync=code_rsync, extra_env=env)

    assert resultat.returncode == code, resultat.stdout + resultat.stderr
    if code == 2:
        assert appels == []


@besoin_age
def test_recuperation_verifie_la_derniere_sauvegarde(tmp_path):
    cle, publique = generer_cle_age(tmp_path)
    destinataire = tmp_path / "pub.txt"
    destinataire.write_text(publique, encoding="utf-8")
    data = tmp_path / "volumes"
    (data / "data").mkdir(parents=True)
    (data / "data" / "x.json").write_text("{}", encoding="utf-8")
    avec_data = subprocess.run(["tar", "czf", "-", "-C", str(data), "data"], check=True, capture_output=True).stdout
    bonne = subprocess.run([AGE, "-R", str(destinataire)], input=avec_data, check=True, capture_output=True).stdout

    vps = vps_factice(tmp_path, {"bsm-20261004-033000.tar.gz.age": b"plus ancienne, non verifiee",
                                 "bsm-20261005-033000.tar.gz.age": bonne})
    resultat, _, _ = lancer_recuperation(tmp_path, vps, "csi@vps", age=True, extra_env={"CLE_SAUVEGARDE": str(cle)})
    assert resultat.returncode == 0, resultat.stderr
    assert "✔ Vérifiée : bsm-20261005-033000.tar.gz.age se déchiffre" in resultat.stdout

    abimee = tmp_path / "abimee"
    abimee.mkdir()
    (abimee / "bsm-20261006-033000.tar.gz.age").write_bytes(bonne[:-40])        # coupée en route
    resultat, _, _ = lancer_recuperation(tmp_path, abimee, "csi@vps", str(tmp_path / "autre"), age=True,
                                         extra_env={"CLE_SAUVEGARDE": str(cle)})
    assert resultat.returncode == 1 and "ne se déchiffre pas" in resultat.stderr
