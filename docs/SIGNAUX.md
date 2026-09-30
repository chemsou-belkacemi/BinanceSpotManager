# Signaux texte — première version

La page **Signaux** conserve les messages dans `data/signals.sqlite3`, séparés
par compte/mode Demo. Aucun message n'est un ordre à lui seul. Le parseur est
déterministe : pas de modèle IA ni d'envoi du texte à un service tiers.
Chaque signal confirmé crée une stratégie avec son propre `position_id`, même
si d'autres stratégies suivent la même paire. Les entrées d'un même signal
restent regroupées dans cette stratégie. Les limites de risque cumulent les
positions de la paire ; les ordres existants ne sont pas transférés.

## Formats

- PAIR / ENTRY 1, ENTRY 2 / T1… / SL : Suhaib, Cleo.
- Coin / Entry Zone / Target 1… / Stop Loss : ABK.
- #PAIRE / Entry1 / TP1… / Stop : Al-Mahwashi, y compris `Stop: prix(1h)`.
- Paire explicite, BUY, Entry Price, TP1… et SL : format simple.
- `SIGNAL_VERSION=2` en première ligne : contrat TXT V2 de CryptoSignalIntelligence
  (voir [Contrat v2](#contrat-v2-txt-cryptosignalintelligence)). Ce texte n'est jamais
  interprété par les modèles ci-dessus, et réciproquement.

Les exemples BICO, ARK et LSK sont reconnus. METIS/Bitget est analysé mais bloqué,
sans substitution de plateforme. XAU/USD, XRPUSD short, indices NIFTY,
shorts/levier GWEI et GOLD/XAUT sont hors périmètre. Un message par import.
Les modèles sont sélectionnables ; ajouter un nouveau format nécessite une
extension testée du parseur, pas une expression arbitraire lancée par le message.
Après l'ajout d'un format, **Réanalyser ce signal** actualise l'analyse du texte
déjà enregistré. Ce bouton conserve le même signal et ne crée aucune commande ;
les messages édités et les signaux déjà confirmés ne peuvent pas être réanalysés.

## Exécution manuellement confirmée

1. Analyser le texte puis contrôler paire, entrées, TP, SL, date et plateforme.
2. Contrôler le budget proposé en devise de cotation (USDT ou USDC). Il peut être
   configuré dans **Settings → Signaux** en montant fixe, pourcentage du portefeuille
   ou mode adaptatif. Par défaut, le mode adaptatif propose 5 % du portefeuille et
   descend à 2 % lorsque le libre de la devise de la paire passe sous 30 % du total.
   La réserve de capital reste prioritaire et le montant demeure modifiable.
3. Pour `SL (1h)` ou `(15min)`, ne rien exécuter si la règle est une clôture
   de bougie. Cette version permet seulement un choix explicite de stop au prix,
   ce qui **change la stratégie source**. Les clôtures de bougie ne sont pas gérées.
4. Simuler avec les règles et soldes Binance Demo ; confirmer dans les 120 secondes.
5. Le worker revalide solde, risque, prix, frais et expiration avant les achats.
   Redémarrer un ancien worker : une capacité `signal_v1` est exigée.

Les entrées LIMIT partagent également le budget, expirent après 24 heures,
et peuvent se remplir immédiatement si leur limite est au-dessus du marché.
Les TP représentent des parts égales de la position : le pourcentage transmis
au moteur existant est converti en pourcentage du restant, dernier TP à 100 %.
Le premier TP confirmé demande l'annulation des entrées encore ouvertes.
Cette version utilise les TP du worker et sa gestion SL existante, **pas un OCO
par tranche**. Frais, arrondis et exécutions partielles peuvent changer les
quantités réellement vendables. Les contrôles de minimum ne garantissent pas
l'exécution future. Le worker doit rester actif.

Un hash du texte normalisé déduplique les imports. Une confirmation conserve
un payload immuable, avec une clé de commande stable. Aucun rejeu d'une commande
incertaine, échouée ou expirée. Consulter **Opérations** avant de poursuivre.
Les dates historiques ne sont jamais présumées valides ; la revue manuelle est
obligatoire, y compris pour les messages sans date.
Les messages édités restent bloqués dans cette première version : après revue,
utiliser New Trade pour un nouveau plan, sans modifier les commandes précédentes.

## Telegram

**Settings → Signaux** : activer l'import et saisir une liste explicite
d'identifiants numériques de conversations. Le token existant est utilisé sans
être affiché ni modifié. Le chat des notifications n'est pas autorisé implicitement.
La relève automatique démarre un lecteur `getUpdates` unique dans un thread du
worker. Il utilise un long polling de 20 secondes : un message est retourné dès
son arrivée, sans attendre la fin des 20 secondes et sans ralentir la boucle TP/SL.
En cas d'erreur, la reprise attend successivement 2, 5, 10, 30 puis 60 secondes.
Le diagnostic est visible dans **Settings → Signaux** et dans la page **Signaux**.

### Exécution automatique optionnelle

**Settings → Signaux → Exécution automatique** permet d'autoriser explicitement
les nouveaux messages Telegram à passer directement dans la file du worker Demo.
Le budget fixe, proportionnel ou adaptatif configuré sur la même page est appliqué.
La date Telegram (ou la date d'origine Telegram d'un transfert) est la référence de
fraîcheur et doit rester dans la fenêtre réglée, 5 minutes par défaut. Elle est exprimée
en temps Unix et ne dépend pas du fuseau du conteneur. La date écrite dans le texte reste
informative car les fournisseurs n'utilisent pas tous correctement les fuseaux horaires.
Les messages reçus avant l'activation, édités, anciens,
ambigus, hors Binance Spot ou incompatibles avec les règles sont conservés avec un
motif de refus et ne créent aucune commande.

Un SL portant une mention `1h` ou `15min` reste refusé sauf si l'autorisation
séparée **Interpréter les SL comme des stops au toucher** est activée. Le worker
recontrôle ensuite le prix, les soldes, la réserve, les frais BNB et les limites de
risque avant toute écriture Binance. La déduplication du signal et de la commande
empêche un second envoi après redémarrage.

Lorsque la relève automatique est désactivée, **Signaux → Relever les messages
Telegram** conserve l'import ponctuel. Le bouton manuel n'appelle jamais
`getUpdates` en parallèle du worker automatique.

Le bot doit avoir accès aux messages du groupe/canal, ou recevoir un transfert
dans une conversation autorisée. Ce n'est pas un accès aux abonnements personnels
Telegram. Un seul consommateur `getUpdates` par bot ; un webhook actif empêche
ce mode. L'offset est conservé après stockage ; les messages hors liste sont
ignorés. Les éditions invalident l'ancienne version locale, sans modifier ni
annuler les ordres déjà transmis. Elles demandent une vérification manuelle.

Références : [Bot API getUpdates](https://core.telegram.org/bots/api#getupdates),
[messages accessibles à un bot](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

## Dépôt direct (générateur ML, CryptoSignalIntelligence)

Un projet local séparé (générateur ML v1 ou CryptoSignalIntelligence, dans son
propre conteneur sur le même hôte) peut remettre ses signaux au bot sans passer par
Telegram. Il monte le volume Docker `binance-spot-manager_bsm-data` (`/app/data`
dans les conteneurs BSM) et tourne avec l'uid `10001` (bsm).

Arborescence, créée par le worker au démarrage :

```
data/signal_drop/
├── incoming/     écrit par le producteur uniquement (*.json v1, *.txt v2)
├── processed/    fichiers enregistrés dans la boîte des signaux
├── rejected/     fichiers refusés + <nom>.reason.txt (motif)
└── outgoing/     execution_events.jsonl : retour d'exécution des signaux v2
```

### Contrat v1

- Le producteur écrit **uniquement** dans `incoming/`.
- Un fichier par signal, écrit atomiquement : `*.tmp` puis renommage en `*.json`.
  Tout fichier ne se terminant ni par `.json` ni par `.txt` est ignoré ; un fichier
  de plus de 64 Ko est déplacé dans `rejected/`.
- Schéma :

  ```json
  {"version": 1, "id": "ml-<hex>", "producer": "mlsignals", "created_at": 1790000000.0,
   "text": "PAIR: BTC/USDT\nPLATFORM: BINANCE\nENTRY 1: 84000\nT1: 86000\nT2: 88000\nSL: 82000",
   "meta": {"probability": 0.63, "model_version": "..."}}
  ```

  `version` vaut 1, `id` respecte `^[A-Za-z0-9_.:-]{1,100}$`, `created_at` est un
  horodatage Unix fini et `text` un message au format structuré déjà reconnu.
  Sinon le fichier part dans `rejected/` avec un fichier `<nom>.json.reason.txt`
  indiquant le motif. `producer` et `meta` sont informatifs et non utilisés.

À chaque cycle, le worker enregistre au plus 20 fichiers dans la boîte des signaux
(source `api`, identifiant externe `drop:<id>`, date de référence `created_at`) puis
les déplace dans `processed/`. Le déplacement a lieu après l'enregistrement : un
arrêt entre les deux provoque une réimportation dédupliquée (hash du texte et
identifiant externe), jamais un second signal.

### Contrat v2 (TXT CryptoSignalIntelligence)

Le producteur publie chaque signal dans `incoming/<SIGNAL_ID>.txt` : écriture de
`<SIGNAL_ID>.txt.tmp`, flush + fsync, puis renommage atomique. Le worker ne lit que
les `*.txt` (jamais `*.txt.tmp`), sans enveloppe JSON. Le texte suit
`docs/SIGNAL_FORMAT_V2.md` du producteur : `SIGNAL_VERSION=2` en première ligne,
une clé `CLE=VALEUR` par ligne, clés en majuscules, **clé inconnue ou dupliquée =
refus**, `NONE` pour les valeurs optionnelles absentes, décimales à point,
horodatages `YYYY-MM-DDTHH:MM:SSZ`, prose libre après `---ANALYSIS---` (ignorée, elle
ne peut rien changer au contrat).

Le parseur `parse_signal_v2` reproduit les règles du modèle producteur et bloque
tout écart : `INTENDED_EXECUTION_ENVIRONMENT=DEMO`, `MARKET_TYPE=SPOT`, `ACTION=BUY`,
`ENTRY_MODE=LIMIT`, `ENTRY_2=NONE`, paire USDT/USDC, `STOP_LOSS < ENTRY_1 < TP_1 < …`
strictement croissants, `TP_COUNT` cohérent avec les `TP_n`, `TP_WEIGHTS` strictement
positifs sommant à 1 (tolérance 1e-9), `RR_TPn_GROSS` recalculé
((TP_n − ENTRY_1) / (ENTRY_1 − STOP_LOSS), 3 décimales, toute autre valeur refusée),
`DATA_AS_OF ≤ CREATED_AT ≤ VALID_FROM < EXPIRES_AT`. Un signal
`VALIDATION_STATUS=SCHEMA_EXAMPLE_ONLY` (exemple de la spécification) est conforme
mais refusé : il n'est jamais exécuté.

Réception (chaque cycle du worker) :

- fichier accepté → boîte des signaux, source `api`, identifiant externe
  `csi:<SIGNAL_ID>`, date de référence `CREATED_AT`, puis `processed/` ;
- `EXPIRES_AT` déjà atteint, texte non conforme, ou clé d'idempotence / `SIGNAL_ID`
  déjà connu → `rejected/` avec `<nom>.txt.reason.txt`. Le registre
  `signal_keys` (table de `data/signals.sqlite3`, écrite dans la même transaction que
  le signal) garantit qu'une `IDEMPOTENCY_KEY` n'est acceptée qu'une fois par compte,
  même sous un autre `SIGNAL_ID` ou un autre texte ; un `SIGNAL_ID` est lui aussi
  unique. Le rejeu strict d'un fichier déjà enregistré (même identifiant, même
  texte : arrêt avant le déplacement) retrouve la ligne existante et part dans
  `processed/`.

Exécution automatique (mêmes interrupteurs que le contrat v1) :

- la fenêtre `VALID_FROM ≤ maintenant < EXPIRES_AT` remplace l'âge maximal de
  5 minutes : un signal pas encore valide attend sans être réclamé, un signal
  expiré est refusé avec le motif ;
- l'écart `|prix / ENTRY_1 − 1| × 10⁴` doit rester ≤ `MAX_ENTRY_DEVIATION_BPS`,
  sinon refus avec l'écart mesuré ;
- `EXIT_POLICY_ID` impose la règle de SL après chaque TP (sauf le dernier), à la
  place du réglage `signal_sl_after_tp` : `FIXED_SL_ONE_TP_V1` et
  `FIXED_SL_FOUR_TP_V1` → aucun changement, `BREAK_EVEN_AFTER_TP1_V1` → break-even,
  `TRAIL_PREVIOUS_TP_V1` → TP précédent ; toute autre politique est refusée ;
- `TP_WEIGHTS` fixe la part initiale de chaque TP ; le moteur reçoit la part du
  restant (`w_i / (1 − Σ_{j<i} w_j) × 100`, dernier TP à 100 %). Le contrôle de
  quantité minimale des tranches utilise le plus petit poids ;
- l'entrée LIMIT expire à `EXPIRES_AT` (et non 24 h après la préparation) ;
  `EXPIRES_AT`, `MAX_ENTRY_DEVIATION_BPS` et `ENTRY_1` sont gelés dans la commande
  et **recontrôlés par le worker juste avant l'achat** (en plus du prix à ±1 %, des
  soldes, de la réserve, des frais et du risque). Une confirmation manuelle dans
  **Signaux** passe par les mêmes gels et recontrôles.

Une entrée expirée sans aucun achat est annulée sur Binance puis la position est
terminée (`CANCELED_BEFORE_FILL`) : elle ne compte plus comme ouverte.

### Retour d'exécution (`outgoing/execution_events.jsonl`)

Pour chaque signal v2, le worker ajoute une ligne JSON par événement, UTF-8, ajout
en fin de fichier (flush + fsync), au format `FEEDBACK_FORMAT.md` du producteur :
`event_id`, `signal_id` (le `SIGNAL_ID`), `event_type`, `occurred_at`
(`…Z`, à la seconde), `environment` (`DEMO`), `producer` (`BinanceSpotManager`),
`symbol`, `quantity`, `price`, `quote_quantity`, `fee`, `fee_asset`, `order_id`,
`target_index`, `reason` ; nombres en chaînes décimales, `null` pour les champs
absents, `fee` et `fee_asset` toujours ensemble.

| Événement | Origine |
|---|---|
| `RECEIVED` | commande `signal:<ligne>` mise en file (signal non expiré, écart d'entrée contrôlé) |
| `REJECTED` (`reason`) | refus à la réception (expiré, non conforme, doublon d'une autre clé), refus de l'exécution automatique (`auto_detail`), commande échouée/expirée/annulée, ou signal jamais traité 60 s après `EXPIRES_AT` |
| `ENTRY_PARTIAL` / `ENTRY_FILLED` | achats réellement remplis, par **incrément** depuis le dernier cumul rapporté |
| `TP_FILLED` (`target_index`) / `STOP_FILLED` | ventes réelles ; une vente au marché hors stop Binance (stop franchi, fermeture manuelle) est rapportée en `STOP_FILLED` avec un `reason` explicite |
| `EXPIRED` / `CANCELLED` (`reason`) | entrées terminées sans aucun achat |
| `CLOSED` | position terminée après au moins un achat |

Les identifiants sont déterministes (`BSM-<SIGNAL_ID>-<TYPE>-<n>`) et enregistrés dans
`data/signal_feedback.sqlite3` après l'écriture de la ligne : au pire, une reprise
réécrit la même ligne avec le même identifiant, que le producteur ignore
(`import-feedback` dédoublonne sur `event_id`). Chaque ligne est validée contre les
règles du modèle producteur avant écriture ; une ligne non conforme est journalisée
et jamais écrite. Le retour est désactivé en `DRY_RUN` (aucun ordre réel). Limites :
les remplissages d'entrée LIMIT sont détectés par la réconciliation (toutes les
12 boucles) — `occurred_at` est alors l'instant de détection ; les frais ne sont
rapportés que lorsqu'un seul actif de commission est en jeu ; le SL est un
`STOP_LOSS_LIMIT` avec une limite 0,3 % sous le stop, son prix de vente peut donc
différer de `STOP_LOSS`.

### Réglages (Settings → Signaux)

- **Importer les signaux déposés** (`signal_drop_enabled`) : désactivé par défaut ;
  les fichiers attendent alors dans `incoming/` sans être lus. Le désactiver retire
  aussi l'exécution automatique des dépôts.
- **Exécuter aussi les signaux ML/dépôt direct** (`signal_drop_auto_enabled`),
  dans **Exécution automatique** : exige l'interrupteur général
  (`signal_auto_execute_enabled`) et une autorisation explicite séparée. Seuls les
  fichiers reçus après cette autorisation (`signal_drop_auto_enabled_since`) et après
  l'autorisation générale sont éligibles. L'exécution automatique Telegram reste
  réglée exactement comme avant et n'active jamais les dépôts.
- **SL après TP (signaux)** (`signal_sl_after_tp`) : aucun changement (défaut),
  break-even, break-even frais inclus ou TP précédent, appliqué après chaque TP
  sauf le dernier, pour les prochains signaux manuels et automatiques de toute
  origine. Les règles demandant une valeur saisie ne sont pas proposées.

### Sécurité

Le texte passe par le même parseur strict que Telegram : un format ambigu ou hors
Binance Spot est conservé avec son motif, sans ordre. `created_at` doit rester dans
la fenêtre de fraîcheur de l'exécution automatique (5 minutes par défaut) ; un
signal v2 obéit à sa propre fenêtre `VALID_FROM` / `EXPIRES_AT`. La
déduplication du signal et la clé de commande stable empêchent un second envoi.
Le worker revalide ensuite prix, soldes, réserve, frais et risque avant toute
écriture, uniquement sur Binance Demo. Sans exécution automatique, un signal
déposé se revoit et se confirme dans la page **Signaux** comme un message Telegram.

## Non activé dans cette version

Suivi des clôtures de bougie, remappage Bitget/forex et apprentissage libre de
formats. Le mode automatique actuel reste limité aux conversations Telegram
autorisées, au dépôt direct local et à Binance Demo Spot.

Tests hors réseau : `make test TESTS="tests/test_signal_sizing.py tests/test_signals.py tests/test_signals_ui.py tests/test_signal_drop.py tests/test_signal_v2.py tests/test_commands.py tests/test_automation.py"`.
