# BinanceSpotManager V2

Gestionnaire intelligent de positions Spot Binance. Une paire = une position active.
Développé en **Python + Streamlit**, conçu pour fonctionner **exclusivement sur Binance Demo**
pendant la phase de développement et de validation.

> **Sécurité — à lire en premier.** Toute écriture (création d'ordre, annulation,
> protection, vente) est refusée si l'URL cible n'est pas dans la liste blanche Demo,
> avec le message `SECURITE : operation interdite hors Binance Demo`. Le mode Live
> n'est pas implémenté : `BSM_RUN_MODE=LIVE` est ramené à `DRY_RUN` au chargement.

---

## 1. Installation

Windows / PowerShell, depuis la racine du projet :

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Ne jamais modifier `.venv` manuellement.

## 2. Configuration

```powershell
Copy-Item .env.example .env
notepad .env
```

Variables essentielles :

| Variable | Rôle | Valeur par défaut |
|---|---|---|
| `BSM_ENV` | `DEMO` ou `LIVE` | `DEMO` |
| `BSM_RUN_MODE` | `DRY_RUN`, `DEMO_MANUAL`, `DEMO_AUTO` | `DRY_RUN` |
| `BSM_DEMO_BASE_URL` | URL Binance Demo | `https://testnet.binance.vision` |
| `BSM_DEMO_API_KEY` | clé API Demo | vide |
| `BSM_DEMO_API_SECRET` | secret API Demo | vide |
| `BSM_QUOTE_ASSET` | actif de cotation | `USDT` |

**Quelle URL utiliser ?** Le cahier des charges mentionnait `https://demo-api.binance.com`.
Le testnet Spot public de Binance est `https://testnet.binance.vision`, et **les deux sont
acceptés** par la liste blanche. Mets dans `.env` l'URL correspondant à tes clés — si elles
viennent de `testnet.binance.vision`, la valeur par défaut convient.

Le fichier `.env` est ignoré par Git. Les clés ne sont jamais affichées par l'application.

## 3. Vérification avant tout lancement

```powershell
python scripts/check_connection.py
```

Affiche : mode, URL, appartenance à la liste blanche, ping, offset horloge, état du compte,
solde, existence de la paire et filtres. **Ne crée aucun ordre.**

Si les clés sont absentes, le script reste utilisable pour tout ce qui est public (prix, filtres).

## 4. Lancement de l'interface

```powershell
streamlit run app.py
```

Pages disponibles dans le menu de gauche :

| Page | Rôle |
|---|---|
| **Dashboard** | worker, portefeuille, positions, ordres réels, annulation, journal |
| **New Trade** | création d'une position complète (Quick / Advanced) |
| **Positions** | détail, ordres Binance, réconciliation, SL manuel, automatisation |
| **History** | positions terminées, statistiques, duplication de stratégie |
| **Settings** | sécurité, risque, presets, notifications, diagnostic |
| **Investissement** | achat Market Demo simple sans sortie, ou avec TP seul / SL seul |

## 5. Lancement du worker

Le worker est un processus **séparé** de Streamlit. Deux façons :

**Depuis l'interface** — Dashboard → *Démarrer le worker*. Le processus est détaché
(sans fenêtre visible sous Windows) et son état s'affiche dans le bandeau.

**En ligne de commande :**

```powershell
python scripts/bot_worker.py
```

Le worker écrit son PID, son heartbeat et son état dans `data/bot_runtime.json`,
tourne à l'intervalle choisi dans Settings (1 seconde dans les préférences
locales actuelles), et reste vivant même sans aucune position.

Les prix des paires suivies utilisent, quand il est disponible, le flux public
`miniTicker` du Spot Testnet. Un prix WebSocket de plus de 5 secondes est ignoré
et le client revient automatiquement à REST ; les statuts d'ordres restent
vérifiés séparément sur Binance. Ce flux n'est activé que pour `testnet.binance.vision` et
requiert le paquet `websockets` indiqué dans `requirements.txt`. Sans ce paquet,
le fonctionnement REST antérieur est conservé.

**Arrêt :** bouton *Arrêter proprement* du Dashboard, ou suppression de `data/bot_stop.flag`.
L'*Arrêt forcé* tue le processus — à réserver à un worker bloqué.

Un verrou (`data/bot_worker.lock`) empêche deux workers de piloter le même compte.
Si un worker démarre alors qu'un autre est vivant, le démarrage est refusé.

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

Si une position existe déjà sur la paire, **aucune seconde position n'est créée** :
les nouvelles Entries sont ajoutées à la position existante.

### Investissement long terme

La page **Investissement** propose un achat simple (sans position bot, TP ou SL),
ou deux sorties exclusives : TP seul ou SL seul. Un achat simple peut utiliser
la devise de cotation de la paire (par exemple USDC dans BTCUSDC ou BTC dans
ETHBTC) et reste possible sur une paire déjà suivie : il ne change pas l'OCO
ni les quantités de la position existante. Les sorties suivies TP/SL acceptent
les paires cotées en USDT et USDC. Le risque est converti en USDT au taux
Binance Demo, sans supposer une parité fixe ; si un taux nécessaire manque,
l'achat avec sortie est refusé.
Elle exige un worker actif, une paire sans position ouverte, un achat Market et
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
| `DEMO_MANUAL` | Demo avec validation humaine possible avant chaque envoi. |
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
├── scripts/                    bot_worker, check_connection,
│                               check_open_orders, demo_tests
├── tests/                      test_engines, test_risk_and_store,
│                               test_automation, test_demo_integration
├── data/                       positions/, signals/, bot_runtime.json, ...
└── logs/                       events.jsonl, bot.log, errors.log
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
La migration explicite `scripts/migrate_oco_demo.py --position-id ID --execute`
arrête le worker, annule le SL indépendant, crée l'OCO, vérifie ses deux
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
est nécessaire. En cas de `429` ou `418`, le client respecte `Retry-After`.

**Confirmation obligatoire des TP.** Un TP n'est jamais considéré exécuté parce que le prix
a touché le niveau. Il faut un fill confirmé par Binance ; un TP partiellement exécuté
enregistre ce qui a réellement été vendu et le cycle suivant prend le reste.

**Prix en `Decimal`, quantités toujours arrondies vers le bas.** Les arrondis passent par
`tickSize` et `stepSize` récupérés dynamiquement — jamais codés en dur — et les valeurs sont
envoyées en texte, pour éviter la notation scientifique que Binance refuse.

**Fenêtre non protégée signalée.** Si le SL ne peut pas être recréé après un déplacement,
l'événement est journalisé en niveau `CRITICAL` plutôt que masqué.

## 10. Tests

Tests hors ligne (aucun réseau, aucun ordre) :

```powershell
pytest -q
```

Couvrent : arrondis prix/quantité, minQty, minNotional, formatage sans notation
scientifique, conversions prix ↔ pourcentage, les trois modes de capital, répartitions,
prix moyen pondéré, scénarios A/B/C, détection de plan invalide, application des fills,
idempotence du recalcul, les six règles de SL évolutif, le risque portefeuille,
la règle « une paire = une position », l'écriture atomique, et toute la chaîne
d'automation TP/SL avec un client Binance simulé.

Tests d'intégration Demo (lecture seule) :

```powershell
pytest tests/test_demo_integration.py -q
python scripts/demo_tests.py
python scripts/demo_tests.py --execute   # ajoute /order/test (aucune exécution)
```

ou pour ne lancer que le hors ligne :

```powershell
pytest -q -m unit
```

Aucun test destructif n'est lancé automatiquement au démarrage de l'application.

## 11. Où sont les données et les logs

| Chemin | Contenu |
|---|---|
| `data/positions/<position_id>.json` | une position par fichier, écriture atomique |
| `data/bot_runtime.json` | état, PID, heartbeat du worker |
| `data/bot_stop.flag` | présent = arrêt demandé |
| `data/bot_worker.lock` | verrou anti double-worker |
| `data/settings.json` | réglages de la page Settings |
| `data/presets.json` | presets enregistrés |
| `logs/events.jsonl` | journal d'événements, une ligne JSON par événement |
| `logs/bot.log` | journal d'exécution |
| `logs/errors.log` | erreurs applicatives |

## 12. Résolution de problèmes

**Le worker ne démarre pas.** Vérifier `data/bot_worker.lock` : s'il contient un PID mort,
Settings → *Nettoyer l'état du worker*. Si un worker vivant le détient, l'arrêter d'abord.

**Le dashboard affiche « Worker inactif » avec un heartbeat ancien.** Le processus est
probablement bloqué : *Arrêt forcé*, puis nettoyage, puis redémarrage.

**Les ordres n'apparaissent pas.** En `DRY_RUN`, aucun ordre n'existe côté Binance — c'est
le comportement attendu. Sinon, vérifier que les clés API sont renseignées et que le compte
Demo est actif.

**`Filter failure`** — les quantités ou prix ne respectent pas les filtres. Vérifier via
`check_connection.py` : `minQty`, `minNotional`, `tickSize`, `stepSize` de la paire.

**Paire inexistante ou non vérifiable** — erreur réseau ou symbole erroné ; New Trade
affiche le détail.

## 13. Limites connues de cette version

- **Mode Live non implémenté.** L'architecture est prête (configuration séparée, URL
  distincte, clés distinctes) mais l'exécution Live est refusée par le code.
- **Analyse de signaux Telegram non implémentée.** L'abstraction existe (canal, déduplication
  prévue par `content_hash`), l'intégration viendra après le cœur du bot.
- **SMS et WhatsApp** : interfaces présentes, envoi désactivé.
- **Le montant engagé dans le sizing dépend du solde lu au moment du calcul.** Si le solde
  change entre la simulation et l'exécution, les quantités envoyées peuvent différer.
- **Persistance JSON locale.** Migrable vers SQLite/PostgreSQL : tous les accès passent par
  `PositionStore`, aucun appel direct au système de fichiers ailleurs.
- **Le break-even avec frais** estime le point d'équilibre à partir des commissions d'achat
  réellement constatées, sans coût de sortie estimé. Il est présenté comme une estimation.
- **Un seul TP est traité par cycle** (5 s) pour garder un état cohérent — sur une chute très
  rapide du prix, plusieurs TP peuvent être atteints avant que le cycle suivant ne les traite,
  mais ils seront bien exécutés aux niveaux prévus.

## 14. Évolutions prévues

Telegram (listener, parser, déduplication, validation humaine), TradingView et autres
sources de signaux, statistiques avancées (win rate par source, drawdown, fréquence
d'atteinte des TP), SMS / WhatsApp, et — après validation complète sur Demo — un mode Live
avec ses propres contrôles et confirmations renforcées.
