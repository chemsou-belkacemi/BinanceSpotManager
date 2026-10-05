# Sécurité du VPS et sauvegardes chiffrées

Cette page donne les commandes exactes, dans l'ordre, pour :

1. **contrôler** le serveur (un script qui ne modifie rien) ;
2. **corriger** les problèmes les plus courants (SSH, pare-feu, fail2ban, mises à jour, droits) ;
3. faire des **sauvegardes chiffrées**, les **récupérer** sur le PC et savoir **restaurer**.

Rien ne s'applique tout seul : c'est toi qui tapes chaque commande. Le bot reste verrouillé
sur Binance Demo ; aucune de ces commandes ne touche à ce verrou ni ne passe d'ordre.

Conventions :

- **Sur le VPS** : dans une session SSH, dans le dossier du projet
  `~/BinanceSpotManager/BinanceSpotManager` (disposition de `docs/VPS.md` de CSI).
- **Sur le PC** : dans un terminal du PC Ubuntu.
- `csi@ADRESSE` : ton utilisateur et l'adresse IP du VPS (ce que tu tapes après `ssh`).

---

## 1. Contrôler le serveur (lecture seule)

Sur le VPS :

```bash
cd ~/BinanceSpotManager/BinanceSpotManager
sudo bash scripts/verifier_vps.sh
```

Le script affiche une ligne par contrôle :

| Signe | Sens |
|---|---|
| ✔ | bon |
| ⚠ | à regarder |
| ✘ | à corriger |

À la fin : un résumé, puis la partie **« À faire »** avec, pour chaque ⚠ ou ✘, la commande à
taper. Code de sortie : 0 s'il n'y a aucun ✘, 1 sinon. Relance-le après chaque correction.

Ce qu'il vérifie :

| Partie | Contrôle |
|---|---|
| SSH | mot de passe refusé, root interdit (ou clé seulement), clé acceptée, clé installée pour ton compte, port |
| Pare-feu | `ufw` actif, entrées refusées par défaut, règles listées ; seuls SSH et, si l'accès public Caddy est utilisé, 80/443 |
| fail2ban | installé, actif, prison `sshd` en place |
| Mises à jour | `unattended-upgrades` installé et activé, mises à jour de sécurité en attente, redémarrage nécessaire |
| Docker | aucun port publié sur Internet sauf 80/443 du proxy Caddy ; l'interface 8501 reste sur `127.0.0.1` |
| Droits | `.env` et `deploy/users.caddy` en 600, à ton nom (relevés avec `stat` : le contenu n'est **jamais** lu) |
| Sauvegardes | clé publique en place, dernière sauvegarde chiffrée de moins de 36 h, archives en clair restantes |
| Ports en écoute | résumé de `ss -tlnp` ; tout port ouvert sur Internet qui n'est pas attendu est signalé |

Sans `sudo`, le script tourne quand même mais plusieurs contrôles sont marqués « illisibles ».

> **Docker contourne ufw.** Un port publié par Docker sur `0.0.0.0` est joignable depuis
> Internet même si ufw le bloque. Dans les `docker-compose.yml`, toujours écrire
> `127.0.0.1:` devant un port (c'est le cas de l'interface 8501 de BSM et des ports de CSI).

---

## 2. Corrections usuelles

Fais-les dans cet ordre, puis relance le contrôle.

### 2.1 SSH : connexion par clé seulement

**Règle d'or : ne ferme jamais ta session SSH avant d'avoir réussi une nouvelle connexion
dans un autre terminal.** En cas d'erreur, la session restée ouverte permet de réparer.

1. Sur le PC, crée une clé SSH si tu n'en as pas encore :
   ```bash
   ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519
   ```
2. Sur le PC, installe-la sur le VPS (le mot de passe est encore accepté à ce moment-là) :
   ```bash
   ssh-copy-id csi@ADRESSE
   ```
3. Sur le PC, dans un **nouveau** terminal : `ssh csi@ADRESSE` doit entrer **sans demander le
   mot de passe du serveur** (au plus la phrase de passe de ta clé).
4. Sur le VPS, coupe les mots de passe et la connexion directe de root :
   ```bash
   printf "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n" | sudo tee -a /etc/ssh/sshd_config.d/00-securite.conf
   sudo sshd -t && sudo systemctl restart ssh
   ```
   `sshd -t` vérifie la configuration (aucun message = bon). Le fichier `00-securite.conf` est
   lu en premier, et c'est la première valeur lue qui compte : il passe donc devant
   `50-cloud-init.conf`, qui réactive parfois les mots de passe sur les VPS.
   Si tu te connectes directement en `root` (sans compte comme `csi`), écris
   `PermitRootLogin prohibit-password` au lieu de `PermitRootLogin no`.
5. Sur le PC, dans un **nouveau** terminal : `ssh csi@ADRESSE` doit toujours marcher. Vérifie :
   ```bash
   sudo sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|pubkeyauthentication) '
   ```
6. **Si la nouvelle connexion échoue**, dans la session restée ouverte :
   ```bash
   sudo rm /etc/ssh/sshd_config.d/00-securite.conf && sudo systemctl restart ssh
   ```
   Dernier recours : la console web de l'hébergeur.

Si tu as changé le port SSH, utilise ce port dans les règles ufw et fail2ban ci-dessous.

### 2.2 Pare-feu ufw

```bash
sudo apt install ufw            # en général déjà présent sur Ubuntu
sudo ufw allow OpenSSH          # SSH D'ABORD (port 22 ; autre port : sudo ufw allow <port>/tcp)
sudo ufw allow 80,443/tcp       # SEULEMENT si l'accès public Caddy est utilisé (COMPOSE_PROFILES=public)
sudo ufw enable                 # répondre y
sudo ufw status verbose
```

Retirer une règle en trop : `sudo ufw status numbered`, puis `sudo ufw delete <numéro>`. Une
règle à la fois : les numéros changent après chaque suppression.

### 2.3 fail2ban (bannit les adresses qui essaient des mots de passe)

```bash
sudo apt install fail2ban
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd
```

Si la dernière commande répond que la prison `sshd` n'existe pas, et seulement si
`/etc/fail2ban/jail.local` n'existe pas encore (`ls /etc/fail2ban/jail.local` répond « No such
file ») :

```bash
printf "[sshd]\nenabled = true\n" | sudo tee /etc/fail2ban/jail.local
sudo systemctl restart fail2ban
```

Port SSH différent de 22 : ajoute une ligne `port = <port>`. Si `/var/log/auth.log` n'existe pas
sur le serveur, ajoute `backend = systemd`. Par défaut, 5 échecs bannissent l'adresse 10 minutes.

### 2.4 Mises à jour automatiques

```bash
sudo apt install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades     # répondre « Oui » (ou « Yes »)
apt-config dump | grep Unattended-Upgrade            # attendu : APT::Periodic::Unattended-Upgrade "1";
```

Mises à jour en attente : `sudo apt update && sudo apt upgrade`.
Redémarrage nécessaire : choisis un moment calme, puis `sudo reboot`. Reconnecte-toi après une
ou deux minutes et vérifie avec `cd ~/BinanceSpotManager/BinanceSpotManager && make ps` : les
conteneurs repartent seuls (`restart: unless-stopped`). Pendant le redémarrage, le worker ne
surveille rien ; les stops déjà posés chez Binance restent en place.

### 2.5 Droits des fichiers sensibles

```bash
cd ~/BinanceSpotManager/BinanceSpotManager
chmod 600 .env
chmod 600 deploy/users.caddy        # seulement s'il existe (accès public Caddy)
ls -l .env deploy/users.caddy
```

Si un fichier appartient à `root` : `sudo chown $USER: .env` (même chose pour `users.caddy`).

### 2.6 Un port publié sur Internet par Docker

Dans le `docker-compose.yml` du projet signalé, remplace par exemple `"8501:8501"` par
`"127.0.0.1:8501:8501"`, puis relance ce projet (`make up` pour BSM, `docker compose up -d`
ailleurs). Ne jamais publier un port sur `0.0.0.0`, sauf 80/443 du proxy Caddy.

---

## 3. Sauvegardes chiffrées

**Le principe.** Le VPS ne reçoit que la **clé publique** : elle permet de chiffrer, pas de
relire. La **clé privée** reste sur ton PC. Même si quelqu'un prend le contrôle du VPS, il ne
peut pas lire les sauvegardes. **Ne copie jamais la clé privée sur le VPS** : elle ouvrirait
toutes les sauvegardes à celui qui pirate le serveur.

`make backup-chiffre` fait la même copie que `make backup` (`data/` et `logs/`, worker arrêté
quelques secondes puis relancé ; ses ordres déjà posés chez Binance ne bougent pas) mais :

- l'archive est chiffrée **à la volée** par `age` : aucune archive en clair n'est écrite sur le
  disque du VPS ;
- le résultat va dans `backups/bsm-AAAAMMJJ-HHMMSS.tar.gz.age` (date en UTC) ;
- seules les **14 plus récentes** sont gardées (`GARDER=<n>` pour changer) ; la rotation ne
  touche à aucun autre fichier ;
- un worker arrêté exprès n'est pas relancé ;
- la clé maîtresse du coffre (volume `bsm-keys`) n'est **jamais** incluse.

`.env` et `deploy/users.caddy` ne sont pas dans la sauvegarde : garde leur contenu dans ton
gestionnaire de mots de passe.

### 3.1 Une seule fois, sur le PC : créer la paire de clés

```bash
sudo apt install age
mkdir -p ~/.config
age-keygen -o ~/.config/bsm-sauvegarde.key       # affiche « Public key: age1… »
chmod 600 ~/.config/bsm-sauvegarde.key
age-keygen -y ~/.config/bsm-sauvegarde.key       # réaffiche la ligne publique age1… quand tu veux
```

`age-keygen` refuse d'écraser une clé existante : tant mieux, une clé remplacée rendrait les
anciennes sauvegardes illisibles. **Range une copie de `~/.config/bsm-sauvegarde.key` hors
ligne** (clé USB gardée chez toi, gestionnaire de mots de passe). Sans elle, les sauvegardes
sont perdues pour toujours. Jamais sur le VPS, jamais dans Git, jamais par e-mail ou Telegram.

### 3.2 Une seule fois : installer age et la clé publique sur le VPS

Sur le VPS :

```bash
sudo apt install age
```

Sur le PC (seule la ligne publique part vers le VPS) :

```bash
age-keygen -y ~/.config/bsm-sauvegarde.key | ssh csi@ADRESSE 'cat > ~/BinanceSpotManager/BinanceSpotManager/deploy/sauvegarde.age.pub'
```

Vérifie sur le VPS : `cat deploy/sauvegarde.age.pub` doit montrer une seule ligne `age1…`.
Ce fichier est ignoré par Git. Une ligne `AGE-SECRET-KEY-…` serait une clé privée : la
sauvegarde la refuse, le contrôle la signale ✘. Dans ce cas, supprime ce fichier du VPS et
crée une nouvelle paire de clés sur le PC.

### 3.3 Faire une sauvegarde

Sur le VPS :

```bash
cd ~/BinanceSpotManager/BinanceSpotManager
make backup-chiffre              # 14 gardées
make backup-chiffre GARDER=30    # ou en garder 30
ls -l backups/
```

Sans `age` ou sans clé publique, la commande refuse avant d'arrêter quoi que ce soit et donne
la commande d'installation.

Les anciennes archives de `make backup` (`backups/bsm-*.tar.gz`) sont **en clair**. Une fois
une sauvegarde chiffrée récupérée et vérifiée sur le PC (3.5), supprime-les :
`rm backups/bsm-*.tar.gz` (ce motif ne touche pas les fichiers `.age`).

### 3.4 Planifier chaque jour à 03:30 UTC

Regarde d'abord le fuseau du serveur :

```bash
timedatectl | grep "Time zone"
```

**Si c'est UTC** (`Etc/UTC`, le cas habituel sur un VPS), utilise cron :

```bash
crontab -e
```

Ajoute cette ligne (une seule ligne ; remplace `csi` par ton utilisateur), enregistre, quitte :

```
30 3 * * * cd /home/csi/BinanceSpotManager/BinanceSpotManager && make backup-chiffre >> /home/csi/sauvegarde-bsm.log 2>&1
```

Vérifie avec `crontab -l` ; le lendemain : `tail /home/csi/sauvegarde-bsm.log`, puis
`sudo bash scripts/verifier_vps.sh` (partie Sauvegardes).

**Si ce n'est pas UTC** : cron suit l'heure du serveur (pas de fuseau par utilisateur sur
Ubuntu). Utilise plutôt un minuteur systemd, qui indique UTC lui-même :

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/bsm-sauvegarde.service <<'FIN'
[Unit]
Description=Sauvegarde chiffrée de BinanceSpotManager

[Service]
Type=oneshot
WorkingDirectory=%h/BinanceSpotManager/BinanceSpotManager
ExecStart=/usr/bin/make backup-chiffre
FIN
cat > ~/.config/systemd/user/bsm-sauvegarde.timer <<'FIN'
[Unit]
Description=Sauvegarde chiffrée de BSM chaque jour à 03:30 UTC

[Timer]
OnCalendar=*-*-* 03:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
FIN
sudo loginctl enable-linger "$USER"        # le minuteur tourne même sans session ouverte
systemctl --user daemon-reload
systemctl --user enable --now bsm-sauvegarde.timer
systemctl --user list-timers bsm-sauvegarde.timer
```

Journal : `journalctl --user -u bsm-sauvegarde --since yesterday`. Choisis cron **ou** le
minuteur, pas les deux.

**Avec la sauvegarde de CSI** (`CryptoSignalIntelligence/scripts/sauvegarde.sh`, `docs/VPS.md`
de CSI) : si elle tourne aussi à 3 h 30, décale l'une des deux d'un quart d'heure (par exemple
`45 3 * * *` pour celle de CSI) : les deux arrêtent le worker du bot. Les archives de ce
script de CSI (`~/sauvegardes`), elles, ne sont pas chiffrées.

### 3.5 Récupérer les sauvegardes sur le PC

Sur le PC, dans le dossier BSM :

```bash
cd ~/BinanceSpotManager/BinanceSpotManager
bash scripts/recuperer_sauvegardes.sh csi@ADRESSE                       # → ~/sauvegardes-bsm
bash scripts/recuperer_sauvegardes.sh csi@ADRESSE /media/$USER/CLE/bsm  # ou vers une clé USB
```

- copie par `rsync` (ou `scp` s'il manque) les seuls fichiers `bsm-*.tar.gz.age` ;
- ne supprime **rien**, ni sur le VPS ni sur le PC : le PC garde plus d'historique que le VPS ;
- si `age` est installé sur le PC et la clé privée dans `~/.config/bsm-sauvegarde.key`, la
  plus récente est **vérifiée** (déchiffrée dans un tube et relue par `tar`, rien n'est écrit
  en clair) : `✔ Vérifiée : … se déchiffre et l'archive est complète`.

Autre dossier sur le VPS : `DOSSIER_DISTANT=/chemin/vers/backups bash scripts/recuperer_sauvegardes.sh csi@ADRESSE`.
Rythme conseillé : une fois par semaine, et après tout changement important.

### 3.6 Lire une sauvegarde sur le PC

Lister le contenu sans rien extraire (bon exercice, une fois par mois) :

```bash
age -d -i ~/.config/bsm-sauvegarde.key ~/sauvegardes-bsm/bsm-AAAAMMJJ-HHMMSS.tar.gz.age | tar tzv | head -20
```

Extraire pour consulter (copie en clair sur le PC, à supprimer ensuite avec
`rm -rf ~/restauration-bsm`) :

```bash
mkdir -p ~/restauration-bsm
age -d -i ~/.config/bsm-sauvegarde.key ~/sauvegardes-bsm/bsm-AAAAMMJJ-HHMMSS.tar.gz.age | tar xz -C ~/restauration-bsm
```

---

## 4. Restaurer

**Quand ?** Sur un **nouveau VPS** (l'ancien est perdu ou piraté), ou quand le volume de
données est **vide** (par exemple après un `docker compose down -v`).

**Jamais** pour revenir en arrière sur un serveur qui a encore ses données : un ancien journal
d'intentions d'ordres pourrait laisser le bot renvoyer un ordre déjà passé (voir le README).
`make import-data` refuse d'ailleurs un volume qui contient déjà des positions.

**Un seul bot à la fois** sur le même compte Demo : l'ancien serveur doit être arrêté
(`make down`) ou injoignable.

1. **Sur le nouveau VPS** : prépare le serveur comme dans `docs/VPS.md` de CSI (utilisateur,
   Docker, `git clone`), recopie `.env` depuis ton gestionnaire de mots de passe, puis
   `chmod 600 .env`. Ne lance pas encore `make up`.
2. **Sur le PC** : déchiffre et envoie `data/` directement dans le dossier du projet sur le
   VPS (aucune archive en clair n'est écrite ; refusé si un dossier `data` y existe déjà) :
   ```bash
   age -d -i ~/.config/bsm-sauvegarde.key ~/sauvegardes-bsm/bsm-AAAAMMJJ-HHMMSS.tar.gz.age | ssh csi@ADRESSE 'cd ~/BinanceSpotManager/BinanceSpotManager && test ! -e data && tar xzf - data'
   ```
3. **Sur le VPS** : le worker démarrera **en veille** (aucun suivi, aucun ordre) tant que tu
   n'as pas contrôlé :
   ```bash
   cd ~/BinanceSpotManager/BinanceSpotManager
   touch data/bot_stop.flag
   ```
   **Nouvelle machine seulement** (ou volume `bsm-keys` perdu) : la clé maîtresse n'est pas
   dans la sauvegarde, c'est voulu. Le coffre des clés et les comptes de connexion restaurés
   seraient illisibles : retire-les avant l'import.
   ```bash
   rm -f data/key_vault.json data/accounts.json
   ```
4. **Import** (refusé si le volume contient déjà des positions : dans ce cas, arrête-toi et
   demande avant toute autre manipulation) :
   ```bash
   make import-data
   ```
5. **Supprime la copie en clair** (les données vivent maintenant dans le volume Docker) :
   ```bash
   rm -rf ~/BinanceSpotManager/BinanceSpotManager/data
   ```
6. **Nouvelle machine seulement** : `make master-key`, puis ressaisis les clés API Demo
   (*Settings → Sécurité*, ou dans `.env`) et recrée les comptes de connexion s'il y en avait
   (`make compte NAME=<nom>`).
7. **Contrôle avant reprise** : depuis le PC, `ssh -N -L 8501:127.0.0.1:8501 csi@ADRESSE`, puis
   http://127.0.0.1:8501. Le Dashboard affiche « Worker en veille ». Regarde *Operations*
   (contrôle des protections) et compare les positions aux ordres ouverts sur Binance Demo.
   Ensuite seulement, Dashboard → *Démarrer* : le premier tour rapproche toutes les positions
   de Binance.
8. Lance `sudo bash scripts/verifier_vps.sh` sur le nouveau serveur.

Les journaux (`logs/`) ne sont pas réimportés : garde-les sur le PC (3.6) pour consultation.

---

## 5. Limites

- Le contrôle est un **instantané** : il ne voit ni le pare-feu de l'hébergeur, ni les blocs
  `Match` de la configuration SSH, ni ce que font les conteneurs. Un ✔ partout ne remplace pas
  une revue de sécurité.
- Les sauvegardes sont chiffrées mais **pas signées** : toute personne qui a la clé publique peut
  fabriquer un fichier valide. Ne restaure que des fichiers que tu as récupérés toi-même.
- Pendant la copie, l'interface reste allumée (comme avec `make backup`) : une action faite
  dans l'interface à 3 h 30 pile pourrait être copiée à moitié.
- La restauration par-dessus des données existantes n'est pas prévue. Sur une nouvelle machine,
  il faut ressaisir les clés API et recréer les comptes de connexion.
- `.env`, `deploy/users.caddy`, la clé maîtresse et les certificats HTTPS ne sont pas dans les
  sauvegardes. Les certificats se refont seuls.

## Récapitulatif

| Où | Commande | Quand |
|---|---|---|
| VPS | `sudo bash scripts/verifier_vps.sh` | après chaque changement, puis une fois par mois |
| PC | `age-keygen -o ~/.config/bsm-sauvegarde.key` | une seule fois |
| PC → VPS | `age-keygen -y ~/.config/bsm-sauvegarde.key \| ssh csi@ADRESSE 'cat > ~/BinanceSpotManager/BinanceSpotManager/deploy/sauvegarde.age.pub'` | une seule fois |
| VPS | `make backup-chiffre` | chaque nuit (cron ou minuteur) |
| PC | `bash scripts/recuperer_sauvegardes.sh csi@ADRESSE` | chaque semaine |
| PC | `age -d -i ~/.config/bsm-sauvegarde.key FICHIER.age \| tar tzv \| head` | chaque mois |
| PC + VPS | partie 4, puis `make import-data` | seulement pour restaurer |
