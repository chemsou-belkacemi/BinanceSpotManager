# BinanceSpotManager V2

Gestionnaire intelligent de positions Spot Binance. Chaque stratégie a son propre ID, même sur une paire identique.
Développé en **Python + Streamlit**, conçu pour fonctionner **exclusivement sur Binance Demo**
pendant la phase de développement et de validation.

> **Sécurité — à lire en premier.** Toute écriture (création d'ordre, annulation,
> protection, vente) est refusée si l'URL cible n'est pas dans la liste blanche Demo,
> avec le message `SECURITE : operation interdite hors Binance Demo`. Le mode Live
> n'est pas implémenté : `BSM_RUN_MODE=LIVE` est ramené à `DRY_RUN` au chargement.

---

## 1. Prérequis

Tout s'exécute dans des conteneurs Docker : ni Python ni paquet à installer
sur la machine. Seuls sont nécessaires :

- **Docker** : Docker Desktop (macOS, Windows) ou Docker Engine (Linux) ;
- **make** : fourni par macOS (outils en ligne de commande Xcode) et Linux ;
  sous Windows, passer par WSL ;
- **git**, pour récupérer le projet.

`make help` liste toutes les commandes. La première commande qui en a besoin
construit l'image (connexion Internet requise, environ une minute).

## 2. Configuration

```bash
make init     # crée .env à partir de .env.example (n'écrase jamais un .env existant)
```

Ouvrir ensuite `.env` dans un éditeur et renseigner les clés. Variables essentielles :

| Variable | Rôle | Valeur par défaut |
|---|---|---|
| `BSM_ENV` | `DEMO` ou `LIVE` | `DEMO` |
| `BSM_RUN_MODE` | `DRY_RUN`, `DEMO_MANUAL`, `DEMO_AUTO` | `DRY_RUN` |
| `BSM_DEMO_BASE_URL` | URL Binance Demo | `https://testnet.binance.vision` |
| `BSM_DEMO_API_KEY` | clé API Demo | vide |
| `BSM_DEMO_API_SECRET` | secret API Demo | vide |
| `BSM_QUOTE_ASSET` | actif de cotation | `USDT` |
| `BSM_UI_PORT` | port de l'interface sur `127.0.0.1` | `8501` |
| `BSM_CSI_API_URL` | API locale de CryptoSignalIntelligence (avis sur les signaux, lecture seule) | `http://csi-api:8503` sous Compose |
| `CSI_API_TOKEN` | jeton facultatif de cette API, identique à celui défini côté CSI | vide |

**Quelle URL utiliser ?** Le cahier des charges mentionnait `https://demo-api.binance.com`.
Le testnet Spot public de Binance est `https://testnet.binance.vision`, et **les deux sont
acceptés** par la liste blanche. Mets dans `.env` l'URL correspondant à tes clés — si elles
viennent de `testnet.binance.vision`, la valeur par défaut convient.

Le fichier `.env` est ignoré par Git, lu au démarrage des conteneurs et jamais
copié dans l'image. Les clés ne sont jamais affichées par l'application. Après
toute modification de `.env`, lancer `make restart` : sous Docker, le bouton
*Recharger la configuration* de Settings ne relit pas le fichier.

## 3. Vérification avant tout lancement

```bash
make check
```

Affiche : mode, URL, appartenance à la liste blanche, ping, offset horloge, état du compte,
solde, existence de la paire et filtres. **Ne crée aucun ordre.**

Si les clés sont absentes, la vérification reste utilisable pour tout ce qui est public (prix, filtres).

## 4. Lancement

```bash
make up       # construit si besoin et démarre l'interface et le worker
make ps       # état des services et des healthchecks
make logs     # journaux de tous les services (make logs SERVICE=worker)
make down     # arrête et supprime les conteneurs ; les données sont conservées
```

Interface : **http://127.0.0.1:8501**, publiée uniquement sur la machine locale.
Deux services partagent la même image : `ui` (Streamlit) et `worker`.

Pages disponibles dans le menu de gauche :

| Page | Rôle |
|---|---|
| **Dashboard** | worker, portefeuille, positions, ordres réels, annulation, journal |
| **New Trade** | création d'une position complète (Quick / Advanced) |
| **Positions** | détail, ordres Binance, réconciliation, SL manuel, automatisation |
| **History** | positions terminées, statistiques, duplication de stratégie |
| **Settings** | sécurité, risque, presets, notifications, diagnostic |
| **Investissement** | achat Market Demo simple sans sortie, ou avec TP seul / SL seul |
| **Operations** | demandes au worker, intentions incertaines, protection, alertes, comptabilité et sauvegardes |

## 5. Le worker

Le worker est un conteneur **séparé** de l'interface. Docker le maintient en vie
et le relance après un crash ou un redémarrage de la machine. Il écrit son
heartbeat et son état dans `data/bot_runtime.json`, tourne à l'intervalle
choisi dans Settings (5 secondes par défaut), et
reste vivant même sans aucune position. L'état affiché par le Dashboard repose
sur ce heartbeat.

**Depuis le Dashboard :** *Arrêter proprement* met le worker **en veille** :
aucun suivi, aucun ordre, heartbeat maintenu. *Démarrer* le relance. Une veille
survit au redémarrage du conteneur et de la machine : un worker mis en veille
ne reprend jamais seul.

**Depuis le terminal :**

| Commande | Effet |
|---|---|
| `make worker-restart` | redémarre le conteneur ; remplace l'arrêt forcé d'un worker bloqué |
| `make worker-stop` | arrête le conteneur (SIGTERM, fin de boucle propre) |
| `make worker-start` | redémarre un conteneur arrêté |

Les prix des paires suivies utilisent, quand il est disponible, le flux public
`miniTicker` de l'environnement Demo sélectionné. Un prix WebSocket de plus de 5 secondes est ignoré
et le client revient automatiquement à REST ; les statuts d'ordres restent
vérifiés séparément sur Binance. Ce flux est limité à `demo-api.binance.com` et `testnet.binance.vision`.

Un verrou système (`data/bot_worker.lock.lease`) empêche deux workers d'utiliser
les mêmes données, y compris depuis deux conteneurs : un second worker est
refusé au démarrage. Ne jamais placer les volumes sur un partage réseau
(verrous et SQLite exigent un seul hôte), ni lancer plusieurs replicas du worker.

### Protection marché, rapport quotidien, suivi des résultats

- **Protection en cas de chute du marché** (Settings → Worker & risque, active par
  défaut) : si BTC baisse d'au moins 3 % en 4 h (plus haut des clôtures 15 minutes
  de la fenêtre → dernier prix), les **nouvelles entrées automatiques** sont
  suspendues 6 h ; les signaux restent dans la boîte et ne partent ensuite que s'ils
  sont encore assez récents. Rien n'est vendu. Option (désactivée par défaut) :
  remonter au seuil de rentabilité, frais compris, le stop des positions en gain,
  jamais à la baisse, jamais une position dont un achat attend encore. Début et fin
  sont notifiés ; le Dashboard affiche la pause. Une même chute ne relance pas la
  pause : après elle, seule une nouvelle baisse depuis le déclenchement compte.
- **Feu de protection CSI** (« météo du marché », Settings → Worker & risque, **désactivé par défaut**) :
  le worker lit `GET /meteo` de CryptoSignalIntelligence (même client, même `BSM_CSI_API_URL`, même
  `CSI_API_TOKEN`), au plus toutes les 5 minutes. Le feu de CSI vient d'une règle déclarée, jamais optimisée :
  volatilité prévue de BTC à 24 h (rang sur 365 jours), BTC contre sa moyenne 50 jours, part des paires
  au-dessus de la leur. **C'est un outil de gestion du risque, comme la perte maximale du jour : pas une
  stratégie, aucun gain démontré** ; l'étude en préparation de CSI (branche `recherche/meteo`) teste une
  règle voisine, pas ce feu, qui ne sera mesuré que par son propre journal côté CSI. Si les données présentes
  suffisent à donner ROUGE alors qu'une composante manque, CSI renvoie ROUGE (sinon INCONNU).
  Réglages : au **ROUGE**, « aucune nouvelle entrée automatique » (défaut) ou « aucune nouvelle entrée, manuelle
  ou automatique » ; à l'**ORANGE**, « taille réduite à X % » (50 % par défaut, même calcul que le trader
  perdant, notée dans le routage) ou « aucune action » ; si CSI est **injoignable ou INCONNU**, « aucune action »
  (défaut : sur le VPS, CSI du PC n'est pas forcément joignable) ou « prudence » (traité comme l'orange). Les
  positions ouvertes restent suivies, rien n'est vendu ni annulé ; les signaux retenus restent dans la boîte.
  Bandeau sur le Dashboard quand le feu bloque ou réduit, notification au début et à la fin d'un ROUGE, ligne
  « Feu CSI » dans `/statut`. Une lecture réussie reste valable 15 minutes si CSI ne répond plus, même après un
  redémarrage du worker. Délais de l'appel : 2 s pour la connexion, 3 s pour la lecture (résolution DNS non bornée).
- **Rapport quotidien** (Settings → Notifications, 20 h UTC par défaut) : résultat
  du jour et des 7 derniers jours (frais compris), positions ouvertes, capital
  engagé, latent, risque si tous les stops sont touchés, meilleur et pire trader ou canal.
  Un seul envoi par jour, même après un redémarrage. « Aujourd'hui » (depuis 00:00 UTC)
  et « 7 derniers jours » ne comptent que les positions **terminées** dans la période ;
  le « PnL réalisé » du Dashboard compte tout **depuis le début**, TP déjà vendus des
  positions encore ouvertes compris. Le rapport donne aussi ce chiffre (ligne « Depuis
  le début ») et le Dashboard ceux du rapport, calculés par la même fonction.
- **BSM face au marché** (Dashboard) : pour les achats exécutés de la période, le
  résultat de BSM comparé à « garder les mêmes cryptos » jusqu'à maintenant et à
  « BTC à la place » (même montant, même moment), mêmes frais d'achat, prix Binance
  Demo. Les références gardent chaque achat jusqu'à maintenant alors que BSM libère
  le capital plus tôt : sur quelques jours, l'écart dépend surtout du marché.
- **Par trader ou canal** (History) : nom du trader écrit en tête du signal, sinon canal ;
  voir `docs/SIGNAUX.md`, section « Trader ou canal d'origine ».
- **Exécution automatique sans listes** (Settings → Signaux, routage, **désactivés par défaut**) : « toutes les
  conversations autorisées sont de confiance » (plus de liste de groupes) et « toutes les cryptos sont acceptées »
  (plus de liste d'actifs). Les contrôles de risque, de liquidité, de perte du jour et l'avis de CSI restent ;
  les activer est un élargissement (case d'autorisation).
- **Taille selon le risque** (Settings → Signaux, **désactivée par défaut**) : budget = capital ×
  perte visée ÷ distance du stop (entrée moyenne → stop). Chaque signal perd alors le même montant
  au stop (0,4 % du capital par défaut, hors frais), plafonné à 20 % du capital par signal et par la
  réserve. Un SL à la clôture de bougie est calculé sur son stop de secours (la vente peut se faire
  jusque-là) ; sans stop de secours, la perte n'est pas bornée et le signal part « à confirmer ».
  Activée, elle remplace la stratégie de budget (part réduite comprise). Proposée aussi sur la page Signaux.
- **Trader ou canal perdant** (Settings → Signaux, **désactivé par défaut**) : au-delà d'un nombre de
  positions terminées et d'une perte nette (frais compris) que tu choisis, ses signaux passent « à
  confirmer » ou partent avec une taille réduite (50 % par défaut), réduction notée dans le routage.
  Un résultat négatif sur peu de trades peut être de la malchance.
- **Conseil de taille de CSI** (information, jamais appliqué) : pour chaque signal automatique, le
  routage note ce que CSI proposerait (taille relative à risque égal entre paires, d'après la seule
  prévision de volatilité confirmée de CSI, et le stop comparé à l'ampleur typique des 24 h). Affiché
  sur la page Signaux ; protocole de mesure dans `docs/RISK_PROTOCOL.md` de CSI.

### Sécurité : surveillance, garde-fous, commandes

- **Alerte « bot muet »** (service `watchdog`, lancé par `make up`) : un service séparé, données en lecture
  seule, prévient sur Telegram si le worker se tait depuis 2 minutes ou s'arrête, avec les positions dont le
  stop n'est pas posé chez Binance ; rappel toutes les 30 min ; retour annoncé ; veille avec positions ouvertes
  annoncée. **Si le VPS entier tombe**, ce service tombe aussi : pour être prévenu quand même, créer un contrôle
  gratuit sur un service externe (par exemple healthchecks.io : période 5 min, délai de grâce 10 min, alerte par
  e-mail ou Telegram), puis mettre son adresse `https://…` dans `.env` (`BSM_HEALTHCHECK_URL=…`) et lancer
  `make up`. Le service n'envoie qu'une requête vide, seulement quand le worker vit.
- **Perte maximale du jour** (Settings → Worker & risque, active, 3 %) : si le résultat du jour (réalisé depuis
  00:00 UTC, frais compris, + latent) descend à −3 % du capital, plus aucune nouvelle entrée, manuelle ou
  automatique, jusqu'à 00:00 UTC. Les positions restent suivies, rien n'est vendu.
- **Commandes Telegram**, dans ta conversation privée avec le bot et depuis ton compte uniquement : `/pause`
  (plus aucune nouvelle entrée jusqu'à `/reprise`), `/reprise`, `/statut`. Réglage : Settings → Notifications.
- **Alertes de connexion** (Settings → Notifications, interrupteur pour les couper) : un message à chaque
  connexion à l'interface et quand un compte est bloqué après 5 échecs.
- **Filtre de liquidité** (Settings → Signaux) : un signal automatique sur une paire à moins de 500 000 USDT de
  volume sur 24 h, ou à plus de 0,5 % d'écart achat/vente, passe « À confirmer ».
- **Stop de secours chez Binance** (Settings → Signaux, 3 % par défaut, 0 = aucun) : un SL « à la clôture de
  bougie » n'est surveillé que par le worker ; un vrai ordre stop est posé chez Binance 3 % sous le niveau de
  clôture, et protège la position même si le bot est arrêté. Il ne part que sur une chute franche (une mèche
  sous ce niveau suffit). S'applique aux prochains signaux.

### Données et sauvegardes

`data/` et `logs/` vivent dans les volumes Docker `bsm-data` et `bsm-logs`, pas
dans le dossier du projet. `make down` les conserve ; **ne jamais lancer
`docker compose down -v`**, qui supprime positions et journal d'intentions.

- `make backup` écrit une archive **non chiffrée** dans `backups/`, worker
  arrêté le temps de la copie. La conserver dans un emplacement privé.
- **Sur un serveur, préférer `make backup-chiffre`** : même copie, chiffrée à la volée
  avec la clé **publique** `age` du propriétaire (`deploy/sauvegarde.age.pub`, hors Git),
  sans archive en clair sur le disque ; `backups/bsm-<date>.tar.gz.age`, 14 gardées
  (`GARDER=<n>`). Sur le PC : `bash scripts/recuperer_sauvegardes.sh <hote-ssh>` rapatrie
  les copies sans rien supprimer sur le serveur. La clé privée ne quitte jamais le PC.
  Clés, planification, déchiffrement et restauration : [docs/SECURITE_VPS.md](docs/SECURITE_VPS.md).
- `make import-data` reprend un ancien dossier `./data` créé hors Docker.
  L'import est refusé si le volume contient déjà des positions. Démarrer sur un
  volume vide alors que des positions sont ouvertes sur Demo les laisserait sans suivi.
- `make shell` ouvre un shell dans un conteneur en cours d'exécution ;
  `make run CMD="python scripts/<script>.py ..."` lance un script ponctuel relié
  aux données.

### Accès public (serveur, VPS)

Sans compte créé, l'interface n'a **aucune authentification** (voir « Location » ci-dessous
pour l'activer) : ne jamais publier le port 8501
(par exemple `0.0.0.0:8501`), d'autant que Docker contourne les règles `ufw`
pour les ports publiés. L'accès public passe par un proxy Caddy : HTTPS
automatique (Let's Encrypt) et un identifiant par personne pour toutes les pages.

1. Ouvrir les ports **80** et **443** du serveur (80 sert au certificat et à la
   redirection vers HTTPS ; avec ufw : `sudo ufw allow 80,443/tcp`). Rien d'autre ne
   doit déjà les utiliser.
2. Compléter `.env` :
   ```bash
   COMPOSE_PROFILES=public
   BSM_PUBLIC_HOST=62-84-177-241.sslip.io   # ou un nom de domaine pointant vers le serveur
   ```
   Sans nom de domaine, `<ip-avec-tirets>.sslip.io` pointe vers l'IP du serveur
   et permet un certificat valide.
3. Créer au moins un utilisateur, puis démarrer :
   ```bash
   make user-add NAME=imad      # demande le mot de passe (12 caractères minimum)
   make up                      # puis ouvrir https://<BSM_PUBLIC_HOST>
   ```

| Commande | Effet |
|---|---|
| `make user-add NAME=<nom>` | ajoute une personne, ou change son mot de passe |
| `make user-remove NAME=<nom>` | retire son accès immédiatement (refusé pour le dernier utilisateur) |
| `make users` | liste les utilisateurs |
| `make logs SERVICE=proxy` | journal d'accès ; `user_id` indique qui a fait chaque requête |

Les modifications s'appliquent sans redémarrage. Les identifiants sont dans
`deploy/users.caddy` (hash bcrypt, lisible par son seul propriétaire), jamais
dans Git ni dans l'image. Le proxy refuse de démarrer sans hôte ni utilisateur :
il n'y a jamais d'interface publique sans mot de passe, et il ne reçoit aucune
clé Binance.

**Tous les utilisateurs ont les mêmes droits** : trades, annulations, réglages
et arrêt du worker, sur le même compte Demo. Il n'y a pas de rôles ; ne donner
un accès qu'à des personnes de confiance. L'adresse `sslip.io` apparaît dans les
journaux publics des certificats et sera rapidement sondée : utiliser des mots de
passe longs et aléatoires. L'interface reste aussi joignable par tunnel SSH
(`ssh -L 8501:127.0.0.1:8501 <serveur>`).

### Sécurité du serveur (VPS)

```bash
sudo bash scripts/verifier_vps.sh   # contrôle en lecture seule : ne modifie rien
```

Une ligne par contrôle (✔ bon, ⚠ à regarder, ✘ à corriger), puis un résumé et la commande
à taper pour chaque point : SSH par clé seulement (`sshd -T`), pare-feu `ufw`, `fail2ban`,
mises à jour automatiques, ports publiés par Docker (qui contourne `ufw` : l'interface 8501
doit rester sur `127.0.0.1`), droits de `.env` et `deploy/users.caddy` (relevés avec `stat`,
jamais lus), sauvegardes chiffrées, ports en écoute. Code de sortie 0 sans ✘, 1 sinon.
Corrections pas à pas et sauvegardes chiffrées : **[docs/SECURITE_VPS.md](docs/SECURITE_VPS.md)**.

### Location : clés du client, connexion, licence

Pour louer le bot sans jamais recevoir les clés des clients : **[docs/LOCATION.md](docs/LOCATION.md)**
(modèles possibles, sécurité, points juridiques à faire vérifier, choix à trancher).

- **Clés API** : le client les saisit dans *Settings → Sécurité*, dans sa propre instance ;
  elles sont chiffrées sur place (AES-256-GCM, `data/key_vault.json`) avec une clé maîtresse
  rangée hors des données (`BSM_MASTER_KEY_FILE` ; Docker : volume `bsm-keys`, `make master-key`).
  Les clés de `.env`, si elles existent, restent prioritaires.
- **Connexion** : `make compte NAME=<nom>` (ou `python scripts/creer_compte.py <nom>`) crée un
  compte mot de passe + code TOTP. Dès qu'un compte existe, chaque page exige la connexion
  (`BSM_AUTH_REQUIRED=true|false` pour forcer). 5 échecs bloquent 15 minutes ; la session expire
  après `BSM_SESSION_IDLE_MINUTES` (30 par défaut) d'inactivité.
- **Licence** : `BSM_LICENCE_REQUIRED=true` + `BSM_LICENCE_PUBLIC_KEY` ; sans licence valide,
  aucune nouvelle entrée, mais les positions ouvertes restent suivies. Émission chez le
  propriétaire : `scripts/emettre_licence.py`.

## 6. Créer un trade

1. **New Trade** → saisir la paire (`BTCUSDT`). Le bot vérifie immédiatement la paire,
   affiche le prix, les soldes, `tickSize`, `stepSize`, `minQty`, `minNotional`.
2. Choisir le capital : montant fixe, pourcentage du solde, ou risque maximum.
   La réserve de capital est prélevée **avant** tout calcul et n'est jamais engagée.
3. Définir les Entries (1 à N, jusqu'à 20 dans l'interface). Chaque Entry est Market
   ou Limit, en prix ou en pourcentage, avec référence `Entry 1` / `Entry précédente` /
   `Prix actuel`.
4. Définir les TP (1 à N), chacun indépendant, avec son pourcentage de vente et sa
   règle de SL après atteinte.
5. Définir le SL (prix fixe ou pourcentage sous prix moyen / Entry 1 / dernière Entry).
6. Lire la simulation : prix moyen, quantités, SL, perte max, gain par TP, gain total,
   scénarios A/B/C, exposition et risque portefeuille.
7. Cocher la confirmation et lancer. Le worker doit être démarré pour que les TP et le
   SL soient surveillés.

Chaque lancement crée une **position indépendante avec son propre ID**, même
sur une paire déjà suivie. Ses Entries, TP, SL, quantités et prix moyen restent
séparés. Les limites de risque cumulent toutefois toutes les positions de la paire.
Les positions déjà fusionnées restent inchangées ; aucun ordre existant n'est déplacé.

### Investissement long terme

La page **Investissement** propose un achat simple (sans position bot, TP ou SL),
ou deux sorties exclusives : TP seul ou SL seul. Un achat simple peut utiliser
la devise de cotation de la paire (par exemple USDC dans BTCUSDC ou BTC dans
ETHBTC) et reste possible sur une paire déjà suivie : il ne change pas l'OCO
ni les quantités de la position existante. Les sorties suivies TP/SL acceptent
les paires cotées en USDT et USDC. Le risque est converti en USDT au taux
Binance Demo, sans supposer une parité fixe ; si un taux nécessaire manque,
l'achat avec sortie est refusé.
Elle exige un worker actif, un achat Market et
le respect des limites de risque du portefeuille. L'achat est calculé avec une
marge de 2 % pour éviter de dépasser le budget lors d'un mouvement du cours.
Après confirmation du fill, le worker place l'ordre de sortie sur Binance Demo :
`LIMIT` GTC pour le TP, `STOP_LOSS_LIMIT` GTC pour le SL. Il ne crée jamais
l'autre branche. Un TP seul laisse la totalité du capital investi exposée ;
un SL stop-limit peut se déclencher sans se remplir en marché rapide.
Vérifier l'ordre effectivement ouvert dans le Dashboard après l'achat.

Le Dashboard affiche aussi tous les soldes Spot Demo non nuls, libres **et**
bloqués par les ordres, avec une valorisation indicative en USDT et EUR à
partir des cotations Demo. Un actif sans taux est signalé et exclu du total,
jamais valorisé à zéro en silence.

## 7. Modes opérationnels

| Mode | Comportement |
|---|---|
| `DRY_RUN` | Prix réels, calculs réels, **aucun ordre envoyé**. Idéal pour tester. |
| `DEMO_MANUAL` | Demo ; tout signal demande une confirmation manuelle (réglable dans Settings → Signaux). |
| `DEMO_AUTO` | Demo, le worker agit automatiquement. |
| `LIVE` | **Non implémenté.** Toute écriture est refusée. |

Pour changer de mode : éditer `BSM_RUN_MODE` dans `.env`, puis *Recharger la configuration*
dans Settings. Aucune bascule Live automatique n'est possible.

## 8. Architecture

```
BinanceSpotManager/
├── app.py                      point d'entrée Streamlit
├── ui_common.py                helpers d'interface partagés
├── requirements.txt
├── Dockerfile / docker-compose.yml / Makefile / .dockerignore
├── .env.example / .gitignore / pytest.ini
├── binance_spot_manager/
│   ├── config.py               Settings + garde-fous de sécurité
│   ├── binance_client.py       client Spot signé HMAC-SHA256
│   ├── symbol_rules.py         tickSize / stepSize / minQty / minNotional
│   ├── models.py               modèles dynamiques (Pydantic v2)
│   ├── position_store.py       persistance JSON atomique
│   ├── event_store.py          journal JSONL
│   ├── strategy_engine.py      résolution des niveaux, sizing, scénarios
│   ├── risk_engine.py          limites portefeuille
│   ├── position_engine.py      logique métier d'une position
│   ├── execution_engine.py     envoi d'ordres, idempotence
│   ├── automation_engine.py    surveillance TP, SL évolutif
│   ├── reconciliation_engine.py comparaison état local ↔ Binance
│   ├── notification_engine.py  Telegram / Email (+ SMS / WhatsApp réservés)
│   ├── bot_process_manager.py  démarrage/arrêt du worker, verrou
│   └── dashboard_service.py    façade de lecture pour l'interface
├── pages/                      1_Dashboard, 2_New_Trade, 3_Positions,
│                               4_History, 5_Settings
├── scripts/                    bot_worker, check_connection, check_open_orders,
│                               demo_tests, migrate_oco_demo, worker_healthcheck
├── tests/                      test_engines, test_risk_and_store,
│                               test_automation, test_demo_integration
├── data/  (volume bsm-data)    positions/, signals/, bot_runtime.json, ...
└── logs/  (volume bsm-logs)    events.jsonl, bot.log, errors.log
```

### Responsabilités

- **Strategy Engine** ne touche jamais au réseau : il transforme une saisie en plan chiffré.
- **Position Engine** ne touche jamais au réseau : il recalcule l'état local.
- **Execution Engine** est le seul à envoyer des ordres, et vérifie toujours
  l'existence d'un ordre avant d'en créer un.
- **Automation Engine** décide *quand* agir, l'Execution Engine *comment*.
- **Reconciliation Engine** ne corrige automatiquement qu'un fill réel non ambigu.

## 9. Choix techniques importants

**Un seul SL côté Binance dans le mode historique.** Le worker
surveille les TP et déclenche les ventes. Cela évite `Filter failure: MAX_NUM_ALGO_ORDERS`.
Il n'y a qu'un `STOP_LOSS_LIMIT` pour la quantité restante, recalculé après chaque TP.
Avant une vente TP, le worker annule ce SL pour libérer le solde BTC réservé, puis
le recrée sur le reliquat si la position reste ouverte. La quantité vendable retire
les commissions d'achat prélevées dans l'actif de base et reste bornée par le
solde libre Binance. Une vente limitée au déclenchement utilise `FOK` : elle
s'exécute immédiatement en entier ou expire, permettant de restaurer le SL.

**OCO Demo expérimental (un TP à 100 %).** Le Dashboard peut calculer les paramètres
d'un OCO de vente pour une position à un seul TP (100 %), en tenant compte des
commissions d'achat et des filtres Binance. L'aperçu seul ne place aucun ordre.
La migration explicite `make migrate-oco POSITION=ID EXECUTE=1` met le worker
en veille, annule le SL indépendant, crée l'OCO, vérifie ses deux
branches, enregistre les identifiants et relance le worker. En cas de refus
confirmé de l'OCO, elle tente de restaurer le SL. Le worker lit ensuite les
deux branches sans envoyer de deuxième vente. Les exécutions partielles ou
un résultat incertain sont signalés comme désynchronisation, sans nouvelle
vente automatique. Cette fonction reste limitée à Binance Demo et à un TP.

**Idempotence.** Chaque ordre porte un `clientOrderId` du type `BSM-D-BTC-<position>-E3`,
construit pour rester sous les 36 caractères. Avant un envoi, le bot vérifie si
l'ordre existe déjà et l'adopte si nécessaire. Après un timeout ou un `5xx`,
il cherche l'ordre côté Binance, mais ne renvoie pas automatiquement une
écriture dont le résultat reste incertain : une vérification/réconciliation
est nécessaire. Un journal SQLite réserve durablement chaque identifiant avant
le POST et interdit son renvoi, y compris après redémarrage. Ce journal ne prouve
pas que l'ordre existe ; il ne faut pas le supprimer pour débloquer une opération.
Seule exception, pour un SL : si son identifiant reste inconnu de Binance une fois
`recvWindow` + 60 s écoulés depuis l'inscription de l'intention, la requête ne peut
plus être acceptée ; le SL est recréé sous un nouvel identifiant (`…-SL1`).
En cas de `429` ou `418`, le client respecte `Retry-After`.

**Confirmation obligatoire des TP.** Un TP n'est jamais considéré exécuté parce que le prix
a touché le niveau. Il faut un fill confirmé par Binance ; un TP partiellement exécuté
enregistre ce qui a réellement été vendu et le cycle suivant prend le reste.

**Prix en `Decimal`, quantités toujours arrondies vers le bas.** Les arrondis passent par
`tickSize` et `stepSize` récupérés dynamiquement — jamais codés en dur — et les valeurs sont
envoyées en texte, pour éviter la notation scientifique que Binance refuse.

**Fenêtre non protégée signalée.** Si le SL ne peut pas être recréé après un déplacement,
l'événement est journalisé en niveau `CRITICAL` plutôt que masqué.

**Stop déjà franchi.** Si Binance refuse le SL parce que le prix est déjà sous
le stop (cas typique : SL remonté au break-even après un TP alors que le cours
est retombé), le worker relit le prix en REST et, s'il reste sous le stop, vend
au marché la quantité nette de cette position via la clôture au marché
(raison `STOP_CROSSED`). Si le prix est remonté, le SL est simplement recréé.
Si la vente est impossible ou incertaine, la position est mise en pause avec
une alerte critique, sans renvoi automatique. Settings → *Vendre au marché si
le stop est déjà franchi* permet de toujours choisir la pause. Une vente au
marché peut s'exécuter sous le stop.

**Refus Binance et nouvel identifiant.** Un ordre SL ou TP refusé de façon
certaine par Binance est retenté avec un nouveau `clientOrderId` ; seul un
résultat incertain (timeout, `5xx`) bloque pour vérification.

**Reprise et ordres orphelins.** Au démarrage du worker et à sa sortie de veille,
toutes les positions sont réconciliées au premier tour, puis tous les 12 tours ;
entre deux, une position dont un achat attend son remplissage est relue au plus
toutes les 10 s (horloge monotone), pour que le SL soit posé peu après l'achat
constaté sans approcher la limite de poids Binance. Au même premier tour (avant
tout signal automatique), puis tous les 60 tours, le worker compare aussi les
ordres ouverts du compte (toutes paires) aux positions
locales : un ordre `BSM-…` inconnu (second worker sur la même clé, stockage
restauré…) déclenche une alerte critique et suspend l'exécution automatique des
signaux, jusqu'à ce qu'un contrôle n'en trouve plus. Le suivi des positions et
les commandes confirmées dans l'interface continuent ; rien n'est annulé.

**Notifications limitées.** Les erreurs et désynchronisations répétées sont
envoyées au plus une fois par minute, et un message identique au plus une fois
par quart d'heure ; le message suivant indique combien ont été regroupés. Les
TP, SL et fins de position ne sont jamais limités. Le worker envoie depuis un
thread dédié pour ne pas ralentir la surveillance.

## 10. Tests

Tests hors ligne (aucun réseau, aucun ordre), dans un conteneur jetable sans
volume ni secret :

```bash
make test
```

Couvrent : arrondis prix/quantité, minQty, minNotional, formatage sans notation
scientifique, conversions prix ↔ pourcentage, les trois modes de capital, répartitions,
prix moyen pondéré, scénarios A/B/C, détection de plan invalide, application des fills,
idempotence du recalcul, les six règles de SL évolutif, le risque portefeuille,
les positions indépendantes par ID, l'écriture atomique, et toute la chaîne
d'automation TP/SL avec un client Binance simulé.

Tests d'intégration Demo (lecture seule) :

```bash
make integration              # tests/test_demo_integration.py, clés .env, sans les volumes de données
make demo-tests               # essais Demo en lecture seule
make demo-tests EXECUTE=1     # ajoute /order/test (aucune exécution)
make open-orders SYMBOL=BTCUSDT   # ordres ouverts et rapprochement, lecture seule
```

Aucun test destructif n'est lancé automatiquement au démarrage de l'application.

## 11. Où sont les données et les logs

Dans les conteneurs, sous `/app/data` (volume `bsm-data`) et `/app/logs` (volume
`bsm-logs`). Consultation : `make shell SERVICE=worker`, ou `make backup` pour
une copie hors Docker (`make backup-chiffre` sur un serveur).

| Chemin | Contenu |
|---|---|
| `data/positions/<position_id>.json` | une position par fichier, écriture atomique |
| `data/positions/.backups/` | instantané local précédent, renouvelé au plus une fois par minute |
| `data/order_intents.sqlite3` | intentions d'ordres durables, sans clés API |
| `data/bot_runtime.json` | état, PID, heartbeat du worker |
| `data/bot_stop.flag` | présent = arrêt demandé |
| `data/bot_worker.lock` | verrou anti double-worker |
| `data/bot_worker.lock.lease` | verrou système ; ne pas supprimer pendant l'exécution |
| `data/settings.json` | réglages de la page Settings |
| `data/presets.json` | presets enregistrés |
| `data/market_guard.json` | état de la protection marché (pause en cours, déclenchement) |
| `data/daily_report.json` | date du dernier rapport quotidien envoyé |
| `data/daily_guard.json` | perte maximale du jour : blocage en cours (jour, montant, seuil) |
| `data/pause_manuelle.json` | pause manuelle des nouvelles entrées (commande Telegram `/pause`) |
| `data/csi_light.json` | feu de protection CSI : dernière couleur lue, effet en cours, ROUGE en cours |
| `logs/events.jsonl` | journal d'événements, une ligne JSON par événement |
| `logs/bot.log` | journal d'exécution |
| `logs/errors.log` | erreurs applicatives |

Hors des volumes :

| Emplacement | Contenu |
|---|---|
| `backups/bsm-<date>.tar.gz` (projet) | archives **en clair** de `make backup` |
| `backups/bsm-<date>.tar.gz.age` (projet) | sauvegardes **chiffrées** de `make backup-chiffre`, 14 gardées |
| `deploy/sauvegarde.age.pub` (projet, hors Git) | clé **publique** `age` qui chiffre les sauvegardes |
| volume `bsm-keys` | clé maîtresse du coffre, jamais dans les sauvegardes |
| `~/sauvegardes-bsm/` (PC) | copies rapatriées par `scripts/recuperer_sauvegardes.sh` |
| `~/.config/bsm-sauvegarde.key` (PC seulement) | clé **privée** qui déchiffre les sauvegardes ; jamais sur le serveur |

## 12. Résolution de problèmes

**Le worker ne démarre pas.** `make ps` puis `make logs SERVICE=worker`. Le message
« Un worker est deja actif » signifie qu'un autre worker utilise les mêmes données
(par exemple un `make run` resté ouvert) : l'arrêter d'abord.

**Le Dashboard affiche « Worker en veille ».** Un arrêt propre a été demandé :
*Démarrer* le relance.

**Le Dashboard n'a plus de heartbeat du worker.** Le conteneur est arrêté ou bloqué :
`make worker-restart`, puis `make logs SERVICE=worker`.

**Les ordres n'apparaissent pas.** En `DRY_RUN`, aucun ordre n'existe côté Binance — c'est
le comportement attendu. Sinon, vérifier que les clés API sont renseignées et que le compte
Demo est actif.

**`Filter failure`** — les quantités ou prix ne respectent pas les filtres. Vérifier via
`make check` : `minQty`, `minNotional`, `tickSize`, `stepSize` de la paire.

**Paire inexistante ou non vérifiable** — erreur réseau ou symbole erroné ; New Trade
affiche le détail.

## 13. Limites connues de cette version

- **Mode Live non implémenté.** L'architecture est prête (configuration séparée, URL
  distincte, clés distinctes) mais l'exécution Live est refusée par le code.
- **Signaux Telegram avec exécution automatique optionnelle.** Le worker peut recevoir en tâche de fond
  les conversations explicitement autorisées avec `getUpdates`, dédupliquer et analyser les
  textes. Il propose un budget fixe, proportionnel ou adaptatif (5 % du portefeuille, réduit
  à 2 % lorsque le capital libre passe sous 30 % par défaut). Une autorisation séparée dans
  Settings peut envoyer les nouveaux signaux valides directement au worker Demo. L'âge du
  message, la réserve, les frais et les limites de risque restent contrôlés.
- **Dépôt direct de signaux par fichiers** (`data/signal_drop/`) : JSON v1 du générateur ML
  local, ou TXT `SIGNAL_VERSION=3` de CryptoSignalIntelligence (fenêtre `VALID_FROM` /
  `EXPIRES_AT`, expiration de l'entrée `ENTRY_EXPIRES_AT`, écart d'entrée maximal, clé
  d'idempotence, poids de TP, seules politiques de sortie `BSM_MARKET_TP_*_V2` avec leur
  empreinte, exécution automatique réservée à `DEMO_ELIGIBLE`, retour d'exécution v2 JSONL
  dans `outgoing/` avec frais réels lus sur myTrades, hors `DRY_RUN`). Détails et limites :
  [docs/SIGNAUX.md](docs/SIGNAUX.md).
- **Routage des signaux** : confirmation manuelle obligatoire si le risque est élevé ou si la
  confiance est faible ou inconnue ; seul un signal sans motif de revue part automatiquement
  (Demo, automatisation autorisée). Confiance déclarée (statut CSI `DEMO_ELIGIBLE`, groupe
  Telegram de confiance, liste d'actifs validés ; JSON v1 toujours manuel), risque frais
  compris, limites dures en miroir, coupe-circuits. Les autres signaux passent « À confirmer »
  dans la page Signaux. Voir [docs/SIGNAUX.md](docs/SIGNAUX.md#routage--confirmation-manuelle-ou-exécution-automatique).
- **SMS et WhatsApp** : interfaces présentes, envoi désactivé.
- **Le montant engagé dans le sizing dépend du solde lu au moment du calcul.** Si le solde
  change entre la simulation et l'exécution, les quantités envoyées peuvent différer.
- **Persistance JSON locale.** Migrable vers SQLite/PostgreSQL : tous les accès passent par
  `PositionStore`, aucun appel direct au système de fichiers ailleurs.
- **Le break-even avec frais** estime le point d'équilibre à partir des commissions d'achat
  réellement constatées, sans coût de sortie estimé. Il est présenté comme une estimation.
- **Un seul TP local est traité par cycle**, à la cadence des Settings. Toucher plusieurs
  niveaux rapidement ne garantit ni leur exécution ni leur prix : les ventes Market
  peuvent subir un glissement et les ventes FOK peuvent expirer.
- **Multi-TP en OCO : aperçu uniquement.** L'exécution d'un OCO par tranche et le
  déplacement coordonné de leurs SL ne sont pas encore implémentés.

## Audit et feuille de route

Voir [l'audit du 28 septembre 2026](docs/AUDIT_SECURITE_2026-09-28.md) pour les
correctifs appliqués, les vérifications et les travaux restants avant commercialisation.
L'interface est désormais liée à `127.0.0.1` par défaut, avec CORS et protection XSRF
activés dans `.streamlit/config.toml`. Redémarrer Streamlit pour appliquer ces réglages.

### Commandes persistantes et opérations

Les boutons d'achat, d'annulation, de déplacement de SL et de fermeture locale
ne passent plus leurs ordres depuis Streamlit. Ils enregistrent une demande dans
`data/commands.sqlite3`, que le worker traite séquentiellement. Un worker actif
est donc nécessaire, également pour une demande de simulation.

- Les confirmations sont dédupliquées et les demandes en attente expirent après
  deux minutes. Un déplacement du marché de plus de 1 % depuis la confirmation
  refuse l'achat ; les soldes et le risque sont recontrôlés côté worker.
- Une demande `SUCCEEDED` signifie **traitée**, pas nécessairement entièrement
  exécutée par Binance. Voir les identifiants et statuts d'ordres.
- Une demande interrompue en cours devient `UNCERTAIN` au redémarrage : jamais
  de renvoi automatique. Operations permet de lire le statut d'une intention.
- Une annulation manuelle met en pause l'automatisation de la position concernée
  afin qu'elle ne recrée pas immédiatement la sortie supprimée.
- La file est séparée par hôte, mode et empreinte de compte. Les positions
  historiques restent monocompte : ne pas partager ce répertoire entre comptes.

Operations affiche un contrôle de protection horodaté, périmé après 30 secondes,
et une boîte d'alertes persistante avec acquittement. Acquitter ne corrige pas le
problème ; les notifications navigateur restent tributaires des autorisations
du système. Une notification réclamée puis interrompue avant affichage reste
visible dans l'historique, sans garantie de son ou de popup.

La comptabilité inclut les frais d'achat en cotation dans le coût réparti entre
vendu et restant. Les commissions en base réduisent les quantités. Les frais
BNB/autres sans conversion historique restent explicitement non valorisés ;
le PnL est signalé incomplet. Aucun taux actuel n'est présenté comme historique.

Pour exporter une sauvegarde : arrêter proprement le worker, ouvrir Operations,
préparer puis télécharger l'archive. Les positions, préférences, presets,
commandes et intentions sont inclus ; `.env`, `.venv` et les journaux sont exclus.
L'archive est **non chiffrée** : la conserver dans un emplacement privé.
Le contrôle vérifie les empreintes, JSON, SQLite et chemins autorisés. Il ne
restaure rien automatiquement et ne prouve pas l'origine d'une archive non signée.
Ne jamais remplacer le journal d'intentions récent par un ancien sans rapprochement.

Cette étape ne met pas en service le multi-OCO, l'authentification multiutilisateur
ni une restauration automatique. Voir [l'état d'avancement](docs/OPERATIONS_V2.md).

## 14. Évolutions prévues

Politiques automatiques distinctes par source, TradingView et
autres sources, statistiques avancées (win rate par source, drawdown, fréquence
d'atteinte des TP), SMS / WhatsApp, et — après validation complète sur Demo — un mode Live
avec ses propres contrôles et confirmations renforcées.

## Clôture manuelle au marché (Binance Demo)

Dans **Positions → Clôturer la position au marché**, confirmer puis cliquer sur
**Annuler les TP/SL et vendre au marché**. Le worker annule les achats en attente
et les protections de cet ID, vérifie leur état final et vend uniquement sa
quantité nette (jamais le solde entier de la paire). Une réponse incertaine bloque
tout nouvel envoi : la reprise interroge Binance sans renvoyer la vente.

Redémarrer le worker après la mise à jour pour activer cette commande. Le résultat
est estimé avant vente ; les exécutions confirmées alimentent le PnL réalisé.
Les gains sont verts, les pertes rouges. Le détail de position affiche l'équivalent
USDT et le pourcentage du capital acheté ; une conversion manquante ou des frais
BNB non valorisés sont signalés. Les poussières non vendables restent au portefeuille :
si, après annulation des ordres, le reste est sous les minimums Binance (minQty ou
minNotional), rien n'est vendu et la position est terminée (reliquat noté dans son
historique). Une position restée en CLOSING pour cette raison est terminée par le
worker au cycle suivant, dès qu'aucun de ses ordres ne peut encore vivre chez Binance.
Après annulation des protections, un autre refus de vente laisse la position en pause,
sans recréer automatiquement les TP/SL : consulter **Operations** avant de réessayer.

Les blocs de lecture du Dashboard et de Positions s'actualisent chaque seconde :
prix, PnL, états, quantités, TP/SL, historique et résultat de clôture. Ils relisent
le stockage du worker ; les champs d'édition ne sont pas réinitialisés par ces
actualisations. Les lectures REST communes aux blocs sont partagées pendant au
maximum une seconde et ce cache n'est pas utilisé pour valider les transactions.
