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
- `SIGNAL_VERSION=3` en première ligne : contrat TXT V3 de CryptoSignalIntelligence
  (voir [Contrat CSI V3](#contrat-csi-v3-txt-cryptosignalintelligence)). Ce texte n'est jamais
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
Chaque message éligible est ensuite **routé** (voir [Routage](#routage--confirmation-manuelle-ou-exécution-automatique)) :
seul un signal sans motif de revue part sans confirmation. Les messages anciens reçus
après l'activation, ambigus, d'un groupe non déclaré ou à risque élevé passent « À
confirmer » avec leurs motifs ; les messages reçus avant l'activation ne sont jamais
routés ; les contrats violés sont refusés. Aucun ne crée de commande sans décision.

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
├── incoming/     écrit par le producteur uniquement (*.json v1, *.txt CSI V3)
├── processed/    fichiers enregistrés dans la boîte des signaux
├── rejected/     fichiers refusés + <nom>.reason.txt (motif)
└── outgoing/     execution_events.jsonl : retour d'exécution v2 des signaux CSI
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

### Contrat CSI V3 (TXT CryptoSignalIntelligence)

Le producteur publie chaque signal dans `incoming/<SIGNAL_ID>.txt` : écriture de
`<SIGNAL_ID>.txt.tmp`, flush + fsync, puis renommage atomique. Le worker ne lit que
les `*.txt` (jamais `*.txt.tmp`), sans enveloppe JSON. Le texte suit le contrat V3
du producteur (`signals/txt.py` pour l'ordre et le type des clés, `signals/schema.py`
pour la cohérence) : `SIGNAL_VERSION=3` en première ligne, une clé `CLE=VALEUR` par
ligne, clés en majuscules, **clé inconnue ou dupliquée = refus** (dont l'ancienne
`INTENDED_EXECUTION_ENVIRONMENT`, remplacée par `ENVIRONMENT`), `NONE` pour les
valeurs optionnelles absentes, décimales à point, horodatages `YYYY-MM-DDTHH:MM:SSZ`,
prose libre après `---ANALYSIS---` (ignorée, elle ne peut rien changer au contrat).
`SIGNAL_VERSION=2` (jamais consommée en production) est refusée avec le motif
« version 2 retirée » ; toute autre version aussi.

`parse_csi_signal` (module `signal_parser.py`, bibliothèque standard seulement : le
test de contrat du producteur `tests/test_bsm_contract.py` le charge par chemin)
reproduit les règles du modèle producteur et bloque tout écart :

- `ENVIRONMENT=DEMO`, `MARKET_TYPE=SPOT`, `ACTION=BUY`, `ENTRY_MODE=LIMIT`, paire
  USDT/USDC, `NEWS_STATUS` parmi `OFF`, `OBSERVE`, `GATE_CLEAR` ;
- `DATA_AS_OF ≤ DECISION_AT ≤ CREATED_AT ≤ VALID_FROM < EXPIRES_AT ≤ ENTRY_EXPIRES_AT` ;
- `ENTRY_COUNT` de 1 à 2, `ENTRY_2=NONE` si et seulement si `ENTRY_COUNT=1`,
  `STOP_LOSS < ENTRY_2 < ENTRY_1`, un poids `ENTRY_WEIGHTS` par entrée sommant à 1 ;
- `STOP_LOSS < ENTRY_1 < TP_1 < …` strictement croissants, `TP_COUNT` cohérent,
  `TP_WEIGHTS` strictement positifs sommant à 1 (tolérance 1e-9) ;
- `RR_REFERENCE` = `ENTRY_1` (obligatoire avec une seule entrée) ou `WEIGHTED_ENTRY`
  (prix moyen prévu : moyenne pondérée en `BASE_QUANTITY`, harmonique en
  `QUOTE_BUDGET`) ; chaque `RR_TPn_GROSS` est **recalculé** sur ce prix de référence
  (3 décimales), toute autre valeur est refusée ;
- `ML_PROBABILITY`, `MODEL_ID`, `ML_TARGET_ID`, `ML_HORIZON_MINUTES`,
  `ML_CALIBRATION_ID` tous renseignés (probabilité dans [0, 1]) ou tous `NONE` ;
- `VALIDATION_STATUS=SCHEMA_EXAMPLE_ONLY` (exemple de la spécification) est refusé ;
  les autres statuts sont lus (l'exécution automatique n'accepte que
  `DEMO_ELIGIBLE`, voir plus bas).

#### Politiques de sortie (identifiant ET empreinte)

Le registre du producteur est copié dans `binance_spot_manager/csi_exit_policies.json`
(un test le compare au fichier `config/exit_policies.json` de CSI quand le dossier
voisin est présent, et recalcule chaque empreinte : sha256 du JSON canonique, clés
triées, sans espaces, 16 premiers caractères). BSM n'accepte que les politiques qu'il
exécute réellement, avec l'empreinte exacte ; toute autre politique, une empreinte
différente, `ENTRY_COUNT=2` (`max_entries=1`) ou `MAX_HOLD_MINUTES` renseigné (aucune
sortie temporelle) produit une erreur contenant `EXIT_POLICY` :

| Politique | Empreinte | Règle de SL appliquée par BSM |
|---|---|---|
| `BSM_MARKET_TP_FIXED_SL_V2` | `0c49448ca40d2a8f` | aucun changement (`stop_rule=FIXED`) |
| `BSM_MARKET_TP_BREAK_EVEN_V2` | `b34dee623d3b61ed` | break-even au prix moyen d'achat réel après le premier TP effectivement rempli, y compris un TP issu d'un report (`stop_rule=BREAK_EVEN_AVG_FILL_AFTER_FIRST_TP`) |

Les V1 (`BSM_MARKET_TP_FIXED_SL_V1`, `BSM_MARKET_TP_BREAK_EVEN_V1`) restent dans le registre
du producteur mais sont **refusées** : elles ne décrivent pas exactement BSM (poids sur la
quantité brute, stop-limit sans vente au marché si franchi, break-even au prix d'entrée).

Règles communes vérifiées dans le code : TP vendu **au marché** dès que le dernier
prix atteint le niveau (`MARKET_ON_TRIGGER`), stop `STOP_LOSS_LIMIT` avec une limite
à 30 points de base sous le stop, vendu au marché si Binance refuse le stop parce que
le prix l'a déjà franchi (`STOP_LIMIT_MARKET_IF_CROSSED`), aucune sortie temporelle,
stop déplacé seulement après la confirmation du fill du TP, parts de TP appliquées à
la quantité **nette** achetée, frais payés en actif de base déduits
(`NET_FILLED_BASE_QUANTITY`),
dernier TP = tout le restant, **tranche sous les minimums Binance reportée sur le TP
suivant** (le TP suivant vend `1 − (1 − p)(1 − q)` du restant, soit exactement la
part cumulée), entrée annulée à `ENTRY_EXPIRES_AT` ou au premier TP, une seule entrée.
Le report des tranches n'est actif que pour les positions issues d'un signal CSI
(`automation.merge_below_minimum_tp`) ; les autres stratégies gardent leur
comportement.

#### Réception, deux expirations, exécution

**Canal unique.** Un texte CSI (première ligne `SIGNAL_VERSION=`) n'est accepté QUE
par le dépôt TXT : `SignalInbox.receive` le refuse s'il arrive par Telegram (message
ignoré, offset avancé), dans un JSON v1 (fichier rejeté) ou par collage dans la page
**Signaux** (message d'erreur). Pour tout texte CSI, la clé d'idempotence et le
`SIGNAL_ID` sont lus dans le texte lui-même et contrôlés dans `signal_keys`, quel que
soit l'appelant. En double sécurité, l'exécution automatique refuse une ligne CSI
dont l'identifiant externe ne commence pas par `csi:`.

Réception (chaque cycle du worker) :

- `SIGNAL_ID` déjà reçu (accepté ou refusé) : contrôlé AVANT tout autre refus. Rejeu
  strict du même fichier → ligne existante, `processed/` ; tout autre fichier portant
  cet identifiant (modifié, invalide, expiré) → `rejected/` avec le motif « déjà
  reçu », **sans aucun événement de retour** : le suivi du signal accepté continue ;
- fichier accepté → boîte des signaux, source `api`, identifiant externe
  `csi:<SIGNAL_ID>`, date de référence `CREATED_AT`, puis `processed/` ;
- `EXPIRES_AT` déjà atteint, texte non conforme (dont version 2, politique non
  exécutée), ou clé d'idempotence / `SIGNAL_ID` déjà connu → `rejected/` avec
  `<nom>.txt.reason.txt`. Le registre `signal_keys` (table de `data/signals.sqlite3`,
  écrite dans la même transaction que le signal) garantit qu'une `IDEMPOTENCY_KEY`
  n'est acceptée qu'une fois par compte, même sous un autre `SIGNAL_ID` ou un autre
  texte ; un `SIGNAL_ID` est lui aussi unique. Le rejeu strict d'un fichier déjà
  enregistré (même identifiant, même texte) retrouve la ligne existante.

Deux expirations distinctes :

- `EXPIRES_AT` borne l'**acceptation du message** : refus à la réception au-delà,
  refus de l'exécution automatique au-delà, et recontrôle par le worker juste avant
  l'envoi de l'ordre d'entrée (valeur gelée dans la commande, comme `VALID_FROM`, lui
  aussi recontrôlé) ;
- `ENTRY_EXPIRES_AT` borne l'**ordre d'entrée** : c'est l'expiration locale de
  l'entrée LIMIT, annulée sur Binance à cette heure si elle n'est pas remplie. Une
  position déjà ouverte continue de suivre sa politique (TP et stop) sans limite de
  durée.

Exécution automatique (mêmes interrupteurs que le contrat v1) :

- `VALIDATION_STATUS` autre que `DEMO_ELIGIBLE` → « À confirmer » (double sécurité : le
  producteur ne publie hors shadow que du `DEMO_ELIGIBLE`) ; la confirmation manuelle
  dans **Signaux** exige une case d'acquittement dédiée ; jamais automatique ;
- la fenêtre `VALID_FROM ≤ maintenant < EXPIRES_AT` remplace l'âge maximal de
  5 minutes : un signal pas encore valide attend sans être réclamé ;
- l'écart `|prix / ENTRY_1 − 1| × 10⁴` doit rester ≤ `MAX_ENTRY_DEVIATION_BPS`,
  sinon refus avec l'écart mesuré ;
- la politique de sortie impose la règle de SL (tableau ci-dessus), à la place du
  réglage `signal_sl_after_tp` ;
- `TP_WEIGHTS` fixe la part initiale de chaque TP ; le moteur reçoit la part du
  restant (`w_i / (1 − Σ_{j<i} w_j) × 100`, dernier TP à 100 %). Le contrôle de
  quantité minimale des tranches utilise le plus petit poids ;
- `EXPIRES_AT`, `ENTRY_EXPIRES_AT`, `MAX_ENTRY_DEVIATION_BPS`, `ENTRY_1` et la
  politique (identifiant, empreinte) sont gelés dans la commande ; expiration et
  écart sont **recontrôlés par le worker juste avant l'achat** (en plus du prix à
  ±1 %, des soldes, de la réserve, des frais et du risque).

Une entrée expirée sans aucun achat est annulée sur Binance puis la position est
terminée (`CANCELED_BEFORE_FILL`) : elle ne compte plus comme ouverte. Un TP atteint
avant tout achat n'est ni vendu, ni reporté, ni mis en échec : le premier TP atteint
annule les entrées encore ouvertes (`CANCEL_AT_ENTRY_EXPIRY_OR_FIRST_TP`), puis la
position sans achat est terminée.

### Retour d'exécution v2 (`outgoing/execution_events.jsonl`)

Pour chaque signal CSI, le worker ajoute une ligne JSON par événement, UTF-8, ajout
en fin de fichier (flush + fsync), au format du retour v2 du producteur
(`feedback/schema.py`) : `event_id`, `signal_id` (le `SIGNAL_ID`), `event_type`,
`occurred_at` (`…Z`, à la seconde), `environment` (`DEMO`), `producer`
(`BinanceSpotManager`), `symbol`, `quantity`, `price`, `quote_quantity`, `fee`,
`fee_asset`, `order_id`, `target_index`, `reason`, `exit_policy_hash` ; nombres en
chaînes décimales, `null` pour les champs absents, `fee` et `fee_asset` ensemble.

| Événement | Origine |
|---|---|
| `RECEIVED` (`exit_policy_hash`) | message accepté : commande `signal:<ligne>` mise en file ; porte l'empreinte de politique vérifiée par BSM |
| `REJECTED` (`reason`) | refus à la réception (expiré, non conforme, version 2, politique, doublon d'une autre clé), commande échouée/expirée/annulée, ou — sans commande (à confirmer non confirmé, refusé, jamais traité, confirmation gelée non transmise) — à `EXPIRES_AT` + 60 s |
| `ORDER_PLACED` | ordre d'entrée accepté par Binance : `order_id`, `quantity` commandée, `price` limite |
| `ENTRY_PARTIAL` / `ENTRY_FILLED` | achats réellement remplis, par **incrément** depuis le dernier cumul rapporté |
| `TP_FILLED` (`target_index`) / `STOP_FILLED` | ventes réelles par TP ou par le stop Binance |
| `MARKET_EXIT_FILLED` (`reason`) | vente au marché hors stop et hors TP : stop refusé car déjà franchi puis vente au marché, ou fermeture manuelle au marché |
| `EXPIRED` / `CANCELLED` (`reason`) | entrées terminées sans aucun achat |
| `CLOSED` | position terminée après au moins un achat |

Frais réels : pour chaque remplissage, le worker lit `GET /api/v3/myTrades?orderId=…`
(lecture seule, Binance Demo) ; `occurred_at` prend alors l'heure de la dernière
exécution Binance. Tant que les exécutions visibles ne couvrent pas la quantité
remplie, l'événement est retardé (30 s au plus), puis écrit sans frais. À défaut de
myTrades, les commissions déjà reçues avec l'ordre sont utilisées ; sinon `fee` et
`fee_asset` sont **omis** (jamais 0). Plusieurs devises de commission sur un même
remplissage : la devise la plus fréquente parmi les exécutions de l'ordre est écrite
(égalité : plus grand montant, puis ordre alphabétique) ; les autres sont journalisées
et omises, pour ne jamais écrire deux événements (donc deux quantités) pour un même
remplissage.

Les identifiants (`BSM-<SIGNAL_ID>-<TYPE>-<n>`) sont fixés avec la ligne complète,
notée comme **intention** dans `data/signal_feedback.sqlite3` AVANT l'écriture ;
après l'écriture, l'événement et le cumul rapporté du remplissage sont validés dans
UNE seule transaction. Un arrêt, ou un fichier verrouillé, laisse l'intention en
attente : elle est réécrite telle quelle (même identifiant, même contenu) au cycle
suivant, que le producteur dédoublonne (`import-feedback` sur `event_id`) ; un
incrément n'est donc jamais compté deux fois et un refus n'est jamais perdu. Une
dernière ligne tronquée n'est jamais prolongée (retour à la ligne ajouté d'abord).
Chaque signal est synchronisé isolément : une erreur sur l'un n'arrête pas les autres. Chaque ligne est validée contre les
règles du modèle producteur avant écriture ; une ligne non conforme est journalisée
et jamais écrite. Le retour est désactivé en `DRY_RUN` (aucun ordre réel).

Limites : un remplissage d'entrée LIMIT survenu après l'envoi n'est vu que par la
réconciliation (toutes les 12 boucles) ; le stop n'est posé qu'après cette
détection. Le SL est un `STOP_LOSS_LIMIT` (limite 0,3 % sous le stop) : son prix de
vente peut différer de `STOP_LOSS`, et un stop déjà franchi est vendu au marché
(`MARKET_EXIT_FILLED`). Un seul TP est traité par cycle.

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
signal CSI obéit à sa propre fenêtre `VALID_FROM` / `EXPIRES_AT`. La
déduplication du signal et la clé de commande stable empêchent un second envoi.
Le worker revalide ensuite prix, soldes, réserve, frais et risque avant toute
écriture, uniquement sur Binance Demo. Sans exécution automatique, un signal
déposé se revoit et se confirme dans la page **Signaux** comme un message Telegram.

## Routage : confirmation manuelle ou exécution automatique

Règle du propriétaire : la confirmation manuelle est **obligatoire** si le risque est
élevé **ou** si la confiance est faible ou inconnue. Un signal part automatiquement sur
Binance Demo seulement si l'exécution automatique est autorisée (Settings → Signaux) et
qu'il n'a **aucun** motif de revue. La confirmation manuelle n'est jamais retirée.

Le routage a lieu dans le worker (`signal_routing`, appelé par l'exécution automatique)
après la préparation du plan et avant tout gel de commande. Trois issues :

| Issue | Sens | Effet |
|---|---|---|
| **AUTO** | aucun motif | commande `SUBMIT_POSITION` avec `confirmation_mode=AUTO` et la décision gelée |
| **À confirmer** (`REVIEW`) | au moins un motif de confiance, de risque ou de données | aucun ordre ; motifs enregistrés et affichés ; alerte dans l'application ; push optionnel |
| **Refusé** (`REJECTED`) | contrat violé : personne ne peut l'exécuter | erreur d'analyse, version retirée, CSI hors dépôt TXT, `EXPIRES_AT` dépassé, écart CSI > `MAX_ENTRY_DEVIATION_BPS`, préparation gelée expirée |

**Confiance** (déclarée, jamais mesurée) :

- CSI : `VALIDATION_STATUS=DEMO_ELIGIBLE` (étiquette de la stratégie, posée à la main
  chez le producteur). Autre statut : « À confirmer » avec une case d'acquittement
  dédiée ; jamais bloqué, jamais automatique.
- Telegram : conversation déclarée « de confiance » (liste vide par défaut, sous-ensemble
  des conversations autorisées) **et** actif de base dans la liste validée. Liste
  pré-remplie avec les 16 actifs de base de CryptoSignalIntelligence (BTC ETH SOL XRP
  NEAR AVAX HBAR LINK XLM ADA TRX FIL ALGO DOT ATOM ETC), à valider (univers halal) ;
  **une liste vide n'autorise aucun actif**. Un actif hors liste passe « À confirmer ».
  Le CSI n'est pas contrôlé contre cette liste : son univers est filtré en amont.
- JSON v1 : toujours « À confirmer ». Collage manuel : jamais routé, confirmé à la main.
- Message trop ancien ou non daté, SL sur clôture de bougie sans autorisation « au
  toucher », mode `DEMO_MANUAL` (honoré par défaut : tout signal est manuel) : « À
  confirmer ». Les messages trop anciens reçus après l'autorisation ne restent plus
  muets : ils passent « À confirmer » avec leur motif.

**Risque** (données déjà lues, aucune requête supplémentaire) :

| Critère | Seuil par défaut |
|---|---|
| Perte au stop frais compris (achat, décalage du stop-limit 0,3 %, vente ; hors gap), % du portefeuille | > 0,5 % (plafonné à la limite dure de 1 %) |
| Limites dures du worker (miroir exact : 1 % / 5 % / 5 positions / 25 % / réserve) | tout refus : « le worker refusera à ce budget » |
| Avertissements du moteur de risque (dernière place, exposition > 50 %) | tout avertissement |
| Risque total projeté, entrées au repos et commandes en file comprises | > 80 % de 5 % (4 %) |
| Même actif déjà ouvert ou en file | ≥ 1 |
| Distance du stop | hors [1 % ; 10 %] |
| Entrée déjà dépassée (Telegram, JSON) | > 1 % sous l'entrée la plus basse |
| `VOLATILITY_REGIME=HIGH` (CSI) | revue |
| Coupe-circuits | ≥ 4 ordres automatiques sur 24 h ; résultat réalisé des signaux depuis 00:00 UTC ≤ −2 % ; 3 pertes automatiques consécutives (jusqu'à « Réarmer l'automatique ») |

Données non mesurables (Binance indisponible, actif sans cours, budget nul, plan refusé,
positions illisibles, erreur interne) : « À confirmer », jamais un refus définitif ni un
envoi automatique. Tous les seuils se règlent dans Settings → Signaux ; un changement qui
élargit l'automatique exige une autorisation explicite et chaque changement est
journalisé (`SIGNAL_ROUTING_CHANGED`). Ces seuils décident **qui confirme** ; ils ne sont
pas calibrés et ne disent rien du résultat attendu.

Une ligne « À confirmer » reste dans cet état : un nouveau réglage ou un renvoi du même
texte ne la re-route jamais. Une seule nouvelle commande automatique par cycle.

**Worker** : en plus de tous les contrôles existants (inchangés), une commande `AUTO`
est refusée si sa décision gelée n'est pas `AUTO`, si c'est un signal CSI non
`DEMO_ELIGIBLE`, ou en `DEMO_MANUAL` (tant que ce mode est honoré). Les confirmations
manuelles et les anciennes commandes sont traitées comme avant.

**Page Signaux** : filtre « Seulement à confirmer » avec compteur, état de chaque ligne,
motifs groupés (confiance, risque, données) avec valeurs et seuils, échéance
`EXPIRES_AT` pour un signal CSI (au-delà, plus confirmable). Après la simulation, un
panneau de risque identique au routage ; une case d'acquittement par catégorie de motif
(et une dédiée au CSI non `DEMO_ELIGIBLE`) ; « Transmettre » est désactivé si une limite
dure refuserait. La commande porte `confirmation_mode=MANUAL`,
`acknowledged_reason_codes` et le budget proposé ; l'événement
`SIGNAL_MANUAL_CONFIRMED` est journalisé. La barre latérale affiche « N signal(aux) à
confirmer » ; Opérations montre la date, le signal et le mode de chaque commande.

**Notification** : `SIGNAL_REVIEW_REQUIRED` entre dans les alertes de l'application.
L'envoi Telegram/e-mail « Signal à confirmer » est **désactivé par défaut** (réglage dans
Settings) ; son texte ne contient aucune ligne de prix (ni entrée, ni TP, ni SL).

**Retour CSI** : pour un signal CSI sans commande (à confirmer, refusé ou jamais traité),
le `REJECTED` n'est écrit qu'à `EXPIRES_AT + 60 s`, afin qu'une confirmation manuelle
dans la fenêtre soit rapportée correctement (`RECEIVED`, `ORDER_PLACED`…).

## Non activé dans cette version

Suivi des clôtures de bougie, remappage Bitget/forex et apprentissage libre de
formats. Le mode automatique actuel reste limité aux conversations Telegram
autorisées, au dépôt direct local et à Binance Demo Spot.

Tests hors réseau : `make test TESTS="tests/test_signal_sizing.py tests/test_signals.py tests/test_signals_ui.py tests/test_signal_drop.py tests/test_signal_csi.py tests/test_signal_routing.py tests/test_commands.py tests/test_automation.py"`.
