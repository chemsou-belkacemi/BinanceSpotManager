# Louer BinanceSpotManager : clés des clients, sécurité, modèles possibles

Document pour le propriétaire. Branche `feat/location`, rédigé le 2026-10-01.

**Rien ici n'est un avis juridique.** Les points de droit sont des pistes à faire vérifier par
un avocat spécialisé en crypto-actifs (ou directement auprès de l'AMF) avant de louer quoi que
ce soit. Le bot reste **verrouillé sur Binance Demo** sur cette branche : rien n'a été rendu
« prêt pour le réel ».

---

## 1. En bref

- **Ta question : « comment relier leurs clés sans qu'ils me les envoient ? »** Le client saisit
  ses clés **lui-même**, dans **sa propre instance** du bot, après s'être connecté. Elles sont
  chiffrées sur place (AES-256-GCM) et ne passent jamais par toi : pas d'e-mail, pas de
  message, pas de fichier envoyé.
- **Recommandation pour démarrer : modèle A** (le client héberge le logiciel sous licence), ou
  **modèle B** (une pile Docker séparée par client sur ton serveur), avec une réserve importante
  pour B : si c'est toi qui héberges, tu as **techniquement** accès à la machine, donc aux clés.
  « Ne pas me les envoyer » est vrai ; « je ne peux pas y accéder » ne l'est qu'avec le modèle A.
- **Ce qui est fait sur cette branche** : coffre chiffré des clés, saisie dans l'interface,
  connexion obligatoire avec second facteur (TOTP), licence signée, durcissement réseau et
  conteneurs (détail au §6).
- **Ce qui manque** pour un vrai service à plusieurs clients sur une même instance est listé au
  §8 : c'est un chantier séparé.
- **Avant tout : aucune performance n'est garantie.** CryptoSignalIntelligence n'a démontré
  aucun avantage statistique (toutes ses stratégies sont rejetées à ce jour). Le bot exécute des
  plans ; il ne rend pas un trade gagnant.

---

## 2. Les modèles possibles

### A. Logiciel auto-hébergé sous licence

Le client installe le bot chez lui (PC, VPS à son nom). Tu lui fournis le logiciel, une licence
signée et du support.

- **Clés** : saisies par le client dans son instance (Settings → Sécurité), chiffrées sur sa
  machine. Tu ne les vois jamais et tu n'y as pas accès. C'est le seul modèle (avec C et E) où
  cette affirmation est vraie au sens technique.
- **Sécurité** : la surface d'attaque est chez lui (son PC, son VPS). Une fuite chez un client ne
  touche pas les autres. À toi de livrer des mises à jour de sécurité.
- **Exploitation** : installation et mises à jour plus difficiles pour un client non technique ;
  support à distance sans jamais demander les clés (seulement les journaux, qui n'en contiennent
  pas).
- **Licence** : sur une machine qu'il contrôle, un client de mauvaise foi peut modifier le code
  ou la configuration et contourner la licence. La licence matérialise le contrat ; elle ne
  protège pas techniquement. Ta protection est le contrat, les mises à jour et le support.
- **Droit (à vérifier)** : la vente d'un logiciel que le client configure et utilise seul est
  probablement la situation la plus simple. Le risque de requalification augmente si c'est toi
  qui choisis les trades (signaux imposés, exécution automatique sur tes recommandations).

### B. Une pile par client, hébergée par toi

Sur ton serveur, une pile Docker complète et séparée par client (interface + worker + volumes +
réseau), chacune avec son domaine, ses comptes, son coffre et sa clé maîtresse.

- **Clés** : saisies par le client dans l'interface, chiffrées dans **son** volume. Elles ne
  transitent pas par toi. Mais tu es administrateur du serveur : avec un accès root, la clé
  maîtresse et le coffre sont lisibles. Il faut le dire honnêtement au client, et réduire
  l'exposition : clé API restreinte à l'IP du serveur, **retraits désactivés**, trading Spot seul.
- **Sécurité** : une compromission du serveur touche tous les clients. Il faut un serveur dédié,
  durci, surveillé, avec sauvegardes chiffrées et procédure d'incident.
- **Exploitation** : une pile par client = mémoire, mises à jour et supervision multipliées. Le
  poids des requêtes Binance est compté **par adresse IP** : plusieurs clients sur la même IP se
  partagent la limite (≈ 6 000 de poids par minute selon la documentation Spot, à vérifier pour
  Demo). Il faut alors réduire le rafraîchissement de l'interface ou répartir sur plusieurs IP.
- **Droit (à vérifier)** : héberger et faire tourner un automate qui passe des ordres sur le
  compte d'un client se rapproche d'un service sur crypto-actifs (exécution d'ordres, voire
  gestion de portefeuille). C'est le point à faire trancher en priorité par un professionnel.

### C. Programme « Broker » / « Link » de Binance (connexion OAuth)

Binance propose à des partenaires acceptés des programmes (courtier, connexion de comptes) qui
permettent à un client d'autoriser une application **sans copier de clé**, à la manière d'une
connexion OAuth, avec parfois un partage de commissions.

- **Clés** : aucune clé à manipuler, l'autorisation est révocable par le client chez Binance.
- **Conditions** : partenariat à demander et à obtenir de Binance, contrat, exigences de
  conformité, intégration technique propre. Je ne connais pas les conditions actuelles : à
  vérifier directement auprès de Binance. Ce n'est pas réalisable sur Demo ni par ce code tel
  quel.
- **Droit** : le statut d'entreprise et d'enregistrement exigé par Binance et par la
  réglementation sera probablement le même que pour B, voire plus strict.

### D. Abonnement aux signaux seulement

Tu vends des signaux (texte, Telegram, format TXT de CSI) ; chaque client les exécute lui-même,
à la main ou avec son propre outil.

- **Clés** : aucune. Tu ne touches jamais à leurs comptes.
- **Sécurité** : minimale de ton côté.
- **Exploitation** : la plus légère.
- **Droit (à vérifier)** : vendre des recommandations d'achat peut relever du **conseil en
  investissement** ou du conseil sur crypto-actifs, et la promotion de produits financiers est
  encadrée en France (loi « influenceurs » de 2023, recommandations de l'AMF). Avec des signaux
  dont **aucune performance n'est démontrée**, le risque commercial et juridique est réel.

### E. Copy trading de Binance

Tu deviens « lead trader » sur la plateforme de copy trading de Binance ; les clients copient
tes positions depuis leur propre compte Binance, et Binance gère tout le reste.

- **Clés** : aucune. Binance gère comptes, fonds et répartition.
- **Conditions** : trader **en réel** sur ton propre compte (incompatible avec le verrou Demo
  actuel), critères d'éligibilité et règles de Binance, historique public de tes performances.
- **Droit** : c'est le cadre de Binance qui s'applique en premier ; ton statut personnel est à
  vérifier.

### Tableau de synthèse

| | A. Auto-hébergé | B. Pile par client chez toi | C. Broker / Link | D. Signaux | E. Copy trading |
|---|---|---|---|---|---|
| Le client te donne ses clés | non | non | non (OAuth) | non | non |
| Tu peux techniquement y accéder | **non** | **oui** (root) | non | non | non |
| Fonctionne avec ce code | oui (Demo) | oui (Demo) | non | partiellement (CSI) | non |
| Charge d'exploitation | faible | élevée | élevée | faible | faible |
| Exposition réglementaire (à vérifier) | plus faible | élevée | élevée | moyenne | cadre Binance |

---

## 3. Point juridique (France / UE) — à faire vérifier

Pistes, **pas des conclusions** :

1. **MiCA** (règlement (UE) 2023/1114) encadre les prestataires de services sur crypto-actifs
   (PSCA, *CASP*). Parmi ces services : réception et transmission d'ordres, exécution d'ordres
   pour le compte de clients, gestion de portefeuille, conseil. En France, l'ancien statut PSAN
   bénéficiait d'une période transitoire ; à la date de ce document, elle est
   vraisemblablement terminée. Faire vérifier si l'activité envisagée exige un agrément et
   lequel.
2. **La frontière probable** : vendre un **outil** que le client paramètre et pilote (A) n'est
   pas la même chose que **décider ou exécuter pour lui** (B avec signaux imposés, gestion
   automatique). Plus tu décides à sa place, plus le risque de requalification monte.
3. **Publicité et promotion** : toute communication sur des gains est très encadrée. Ne jamais
   annoncer de performance ; CSI n'en a démontré aucune.
4. **Consommateurs** : conditions générales de vente et d'utilisation, droit de rétractation pour
   les services numériques, médiation de la consommation, facturation.
5. **Données personnelles (RGPD)** : comptes, adresses, identifiants Telegram, journaux. Registre
   des traitements, durée de conservation, sous-traitants (hébergeur).
6. **Conditions d'utilisation de Binance** : usage de l'API par un tiers, programmes partenaires,
   pays des clients.
7. **Fiscalité** de ton activité de location (et information des clients sur la leur).

---

## 4. Comment un client relie ses clés (ce qui est fait)

1. Il crée sur Binance (Demo aujourd'hui) une **clé dédiée** au bot : **retraits désactivés**,
   **restriction à l'IP** du serveur qui fait tourner le bot, trading Spot seulement.
2. Il se connecte à **son** instance (identifiant, mot de passe, code à 6 chiffres).
3. Settings → Sécurité → « Mes clés API Binance Demo » : il colle la clé et le secret dans des
   champs masqués, puis « Chiffrer et enregistrer mes clés ».
4. Les clés sont chiffrées (AES-256-GCM) dans `data/key_vault.json`. La clé maîtresse est dans un
   fichier séparé, hors des données et hors des sauvegardes. Elles ne sont **jamais réaffichées** :
   seuls les 4 derniers caractères de la clé API et la date sont montrés.
5. Il redémarre le worker pour qu'il les prenne en compte (`make worker-restart` sous Docker).
6. Il peut supprimer ses clés à tout moment (refusé tant que des positions sont ouvertes, pour
   ne jamais les laisser sans stop). Pour une révocation immédiate, il supprime la clé **chez
   Binance** : c'est la seule coupure certaine.

Contrôle des droits d'une clé : `binance_spot_manager/api_key_policy.py` évalue la réponse de
`GET /sapi/v1/account/apiRestrictions` (retraits activés → refus, trading Spot absent → refus,
IP non restreinte → avertissement). **Elle n'est appelée nulle part** : la route n'existe pas
sur Binance Demo, et le client n'appelle aucune route `/sapi`. Elle servira le jour où tu
déciderais d'un passage en réel, pour refuser une clé dangereuse avant tout ordre.

---

## 5. Mise en route

### Installation actuelle (la tienne)

Rien ne change tant que tu ne crées pas de compte : les clés de `.env` restent prioritaires,
l'interface reste sans connexion (`BSM_AUTH_REQUIRED` non défini), la licence n'est pas exigée.

### Une instance louée (modèle A ou B)

```bash
make init                         # .env sans clé Binance (le client les saisira dans l'interface)
make master-key                   # crée la clé maîtresse dans le volume bsm-keys (hors sauvegardes)
make compte NAME=client           # mot de passe + secret TOTP affiché UNE fois (QR via l'URI otpauth)
make up
```

Variables à ajouter au `.env` de l'instance (aucune n'est secrète) :

```bash
BSM_AUTH_REQUIRED=true            # connexion toujours exigée
BSM_SESSION_IDLE_MINUTES=30       # déconnexion après inactivité (5 à 480)
BSM_LICENCE_REQUIRED=true         # sans licence valide : aucune nouvelle entrée
BSM_LICENCE_PUBLIC_KEY=<ta clé publique en base64>
# BSM_LICENCE_FILE=data/licence.json
# Hors Docker seulement (Docker fixe /var/lib/bsm-keys/master.key) :
# BSM_MASTER_KEY_FILE=~/.config/binance-spot-manager/master.key
```

`.env.example` n'a **pas** été mis à jour sur cette branche (voir §8) : ces lignes sont à y
ajouter, ainsi que la correction de `BSM_LIVE_BASE_URL` (aujourd'hui
`https://demo-api.binance.com`, trompeur ; valeur indicative attendue `https://api.binance.com`,
qui reste inerte puisque LIVE est bloqué).

### Licences (chez toi uniquement)

```bash
# une fois : la paire de clés (clé privée chiffrée par phrase de passe, HORS du dépôt)
python scripts/emettre_licence.py generer --cle-privee ~/licences/bsm_licence.pem
# pour chaque client
python scripts/emettre_licence.py emettre --cle-privee ~/licences/bsm_licence.pem \
    --client "Nom du client" --offre mensuelle --fin 2026-12-31 --sortie licence-client.json
```

Le client installe le fichier dans Settings → Sécurité → Licence (vérifié avant installation).
Sans licence valide (absente, falsifiée, expirée, clé publique absente) et avec
`BSM_LICENCE_REQUIRED=true` :

- **refusé** : nouvelles positions (`SUBMIT_POSITION`, `SIMPLE_BUY`) et exécution automatique
  des signaux, avec un message clair et un événement critique ;
- **toujours fait** : suivi des positions ouvertes, stops, objectifs, clôtures, annulations.
  Une position n'est jamais abandonnée à cause de la licence.

---

## 6. Ce qui est fait sur cette branche

| Sujet | Où | Ce que ça garantit |
|---|---|---|
| Coffre des clés | `binance_spot_manager/key_vault.py` | AES-256-GCM, nonce neuf à chaque écriture, données associées (nom + métadonnées), écriture atomique 0600, clé maîtresse 0600 créée au premier usage, refusée dans le dépôt ou dans `data/`, refusée si lisible par d'autres comptes ; « mauvaise clé » distinguée de « fichier altéré » |
| Clés dans la configuration | `config.py` (`load_settings`) | l'environnement reste prioritaire ; sinon le coffre ; jamais de secret dans `repr`, le dump ou les journaux ; un coffre illisible n'empêche pas le démarrage (message sans secret) |
| Saisie des clés | `pages/5_Settings.py`, onglet Sécurité | champs masqués, jamais réaffichées, effacées de la session serveur après envoi, suppression avec confirmation, refus de changer ou supprimer avec des positions ouvertes, rappel des bonnes pratiques |
| Contrôle des droits d'une clé | `api_key_policy.py` | fonction pure testée hors ligne ; non appelée (Demo) |
| Connexion | `auth.py`, `ui_common.require_login`, chaque page | scrypt (N=2^17, r=8, p=1, sel 16 octets), TOTP RFC 6238 (±1 pas, anti-rejeu), 5 échecs → 15 min de blocage, comparaisons à temps constant, identifiant inconnu au même coût, session expirée après inactivité (contrôlée aussi par le rafraîchissement automatique), sessions fermées si le compte est réinitialisé ou supprimé, bouton de déconnexion |
| Création de compte | `scripts/creer_compte.py`, `make compte` | secret TOTP montré une seule fois, vérification facultative d'un code |
| Licence | `licence.py`, `scripts/emettre_licence.py`, worker, commandes | Ed25519 hors ligne ; nouvelles entrées seulement bloquées |
| Migration SQLite | `signal_inbox.py` | migration du schéma sûre entre connexions concurrentes (corrige un test intermittent) |
| TLS SMTP | `notification_engine.py` | certificat et nom d'hôte vérifiés |
| Proxys de l'environnement | `binance_client.py`, `market_price_stream.py` | ni `HTTPS_PROXY`, ni `REQUESTS_CA_BUNDLE` pour les requêtes Binance ; pas de proxy pour le flux de prix |
| Verrou Demo | `config.py` + tests hors ligne | `BSM_RUN_MODE=LIVE` → DRY_RUN ; `BSM_ENV=LIVE` → environnement inerte (DRY_RUN, écritures refusées) ; testé sans réseau |
| Architecture réseau | `tests/test_hardening.py` | seuls les modules listés importent une bibliothèque réseau ; aucun hôte ni route Binance hors du client |
| Conteneurs | `docker-compose.yml`, `Dockerfile`, `Makefile` | `no-new-privileges`, `cap_drop: ALL`, `read_only` + `/tmp` en mémoire, limites mémoire/CPU/processus, rotation des journaux ; clé maîtresse dans un volume séparé, en lecture seule dans `ui` et `worker`, écrite seulement par l'outil ponctuel `keytool` (sans réseau) |
| Image | `.dockerignore`, `.gitignore` | `.claude/`, `*.key`, `*.pem` exclus |

`read_only` a été vérifié **sans Docker** : Streamlit lancé avec le code et le dossier personnel
en lecture seule a exécuté l'accueil, le Dashboard et Settings sans erreur, en n'écrivant que dans
`data/` et `logs/`. Un vrai `make up` reste à faire de ton côté (voir §8).

---

## 7. Mentions de risque à donner aux clients

À reprendre dans les conditions générales et dans l'interface :

- Le trading de crypto-actifs peut faire perdre tout le capital engagé.
- **Aucune performance n'est garantie ni annoncée.** Les stratégies de CryptoSignalIntelligence
  n'ont démontré **aucun avantage** après coûts ; ses avis sont informatifs.
- Le bot exécute des plans sur Binance **Demo** ; Demo n'a ni les mêmes prix ni les mêmes
  exécutions que le marché réel.
- Le client reste responsable de ses clés (droits, restriction IP, révocation) et de ses
  décisions.
- Une panne (serveur, réseau, Binance) peut empêcher la pose ou le déplacement d'un stop.

---

## 8. Ce qui reste à faire

**Non fait sur cette branche, et pourquoi :**

- **`.env.example`** : la règle de refus du poste (lecture et écriture de `.env*`) a bloqué sa
  modification. Les variables à ajouter et la correction de `BSM_LIVE_BASE_URL` sont au §5.
- **Interface sans secret** : l'interface lit encore les clés, car le Dashboard fait des lectures
  signées (soldes, ordres ouverts). La clé maîtresse est donc montée (en lecture seule) dans `ui`
  **et** `worker`. Pour que seul le worker déchiffre, il faut que le worker publie un instantané
  (soldes, ordres) que l'interface lit.
- **Le worker ne relit pas les clés à chaud** : après un changement, il faut le redémarrer.
- **Telegram et SMTP** : le jeton Telegram et le mot de passe SMTP restent dans `.env` (pas encore
  dans le coffre).
- **Vérification Docker réelle** : `make master-key`, `make compte`, `make up` avec `read_only`,
  `cap_drop` et le volume `bsm-keys` n'ont pas été lancés (interdit sur cette branche). Point à
  surveiller : les droits du volume `bsm-keys` à sa création (il doit appartenir à l'utilisateur
  `bsm`, uid 10001).

**Pour un vrai service à plusieurs clients :**

1. **Multi-locataires réel** : un identifiant de compte partout (positions, réglages, commandes,
   signaux, intentions d'ordres, événements, alertes), plus de singletons partagés dans
   l'interface, un worker par client ou un ordonnanceur qui les isole.
2. **Base transactionnelle** (PostgreSQL) au lieu des fichiers JSON et des bases SQLite par
   fichier.
3. **Flux privé WebSocket** (*user data stream*) au lieu du sondage REST des ordres.
4. **Budget de requêtes par IP** partagé entre clients, flux de prix public commun, fin du
   rafraîchissement REST à la seconde dans l'interface.
5. **Supervision** : alertes d'exploitation (worker arrêté, stop non posé, licence bientôt
   expirée), journaux centralisés sans secret.
6. **Sauvegardes chiffrées** par client (`make backup-chiffre` chiffre déjà avec la clé
   publique `age` du propriétaire, voir [SECURITE_VPS.md](SECURITE_VPS.md) ; reste à organiser
   une clé et un rapatriement par client. Le coffre y est chiffré, la clé maîtresse n'y est pas).
7. **Facturation**, **conditions générales**, mentions légales, politique de confidentialité.
8. **Rôles** (client / opérateur), journal d'audit des actions sensibles, en-têtes de sécurité
   (CSP) côté application.

---

## 9. Choix que toi seul peux trancher

1. **Quel modèle** : A (auto-hébergé), B (pile par client chez toi), C, D ou E — ou A pour
   commencer puis B ?
2. **Statut juridique** : consulter un avocat spécialisé et/ou l'AMF avant le premier client
   (agrément éventuel, conditions générales, mentions de risque). Qui, et quand ?
3. **Demo seulement, ou réel un jour** : louer un bot verrouillé sur Demo, est-ce une offre qui a
   du sens ? Un passage en réel est une décision à part entière (et ne se fera pas sans ta
   demande explicite).
4. **CSI dans l'offre** : inclure ses avis, sachant qu'aucune stratégie n'a démontré d'avantage ?
   Si oui, avec quelles mentions ?
5. **Hébergement (si B)** : un serveur par client ou plusieurs piles par serveur (limite de
   requêtes Binance par IP) ? Quelle procédure si le serveur est compromis ?
6. **Licence** : durée, prix, période de grâce à l'expiration (aujourd'hui : aucune, les entrées
   s'arrêtent le lendemain du dernier jour), ce qu'il se passe en fin de contrat.
7. **Support** : par quel canal, avec quelle règle écrite « nous ne demandons jamais vos clés » ?
8. **Accès public** : chaque client passe-t-il par un domaine HTTPS (Caddy + connexion de
   l'application) ou par un tunnel SSH ?
9. **Prochaine étape technique** : interface sans secret (§8), secrets Telegram/SMTP dans le
   coffre, ou multi-locataires réel ?
10. **Fusion** : relire cette branche (`feat/location`) et décider de la fusionner, ou non.
