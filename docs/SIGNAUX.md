# Signaux texte — première version

La page **Signaux** conserve les messages dans `data/signals.sqlite3`, séparés
par compte/mode Demo. Aucun message n'est un ordre à lui seul. Le parseur est
déterministe : pas de modèle IA ni d'envoi du texte à un service tiers.
Chaque signal confirmé crée une stratégie avec son propre `position_id`, même
si d'autres stratégies suivent la même paire. Les entrées d'un même signal
restent regroupées dans cette stratégie. Les limites de risque cumulent les
positions de la paire ; les ordres existants ne sont pas transférés.
Ce `position_id` est dérivé du compte Demo (scope, mode compris) et du signal :
`IDEMPOTENCY_KEY` pour un signal CSI au contrat V3, sinon l'empreinte du texte
normalisé. Deux installations BSM sur la même clé qui reçoivent le même signal
calculent donc les mêmes `clientOrderId` : Binance n'accepte qu'un achat. Les
positions manuelles (New Trade, Investissement) gardent un identifiant aléatoire.

## Formats

Le parseur ne dépend pas d'un fournisseur : il lit les **étiquettes**, quel que soit l'habillage
(emojis, lignes de séparation, numérotation `1)`/`1️⃣`, pourcentages, flèches, `|`, plusieurs
étiquettes sur une seule ligne).

Exception : `SIGNAL_VERSION=3` en première ligne désigne le contrat TXT V3 de
CryptoSignalIntelligence (voir [Contrat CSI V3](#contrat-csi-v3-txt-cryptosignalintelligence)).
Ce texte n'est jamais lu par étiquettes, et un message par étiquettes n'est jamais lu comme V3.

| Élément | Étiquettes reconnues |
|---|---|
| Paire | `PAIR`, `COIN`, `SYMBOL`, ou n'importe où : `SAGA/USDT`, `#DOGE/USDT`, `LSKUSDT`, `ARB-USDT` |
| Entrées | `ENTRY`, `ENTRY 1`, `ENTRY ZONE`, `ENTRIES`, `BUY`, `BUY ZONE`, `ACHAT` — prix, plage `a - b` ou liste |
| Objectifs | `TP`, `TP1`, `T1`, `TARGET 1`, `TARGETS`, `TAKE PROFIT 1`, `OBJECTIF` |
| Stop | `SL`, `STOP`, `STOP LOSS`, `STOPLOSS`, `INVALIDATION` ; `(15m)`, `(4H close)`, `daily close` = mention de clôture |
| Plateforme | `PLATFORM`, `EXCHANGE` |

Un en-tête sans prix (`TARGETS:`, `ENTRY ZONE:`) peut être suivi de prix seuls, un par ligne.
Direction : achat, sauf `SHORT`, `SELL` en tête, levier, futures ou marge (refusés) ; un short non
annoncé est de toute façon refusé par la règle SL < entrées < TP.

**Refusé plutôt que deviné** : deux prix pour un TP ou un SL, `or market`, `(market)`/`(au marché)`, `above`/`breakout`,
`150K`, `160+`, virgule (`140,5`, `84,000`), prix négatif, deux paires, paire sans USDT/USDC,
plateforme autre que Binance, TP non strictement croissants (y compris publiés à l'envers),
numérotation discontinue. Le motif affiché cite la ligne en cause. XAU/USD, indices NIFTY et
shorts restent hors périmètre. Un message par import. Le choix d'un modèle précis reste possible ;
un nouveau cas non lu se corrige par une extension testée du parseur (`tests/test_signal_formats.py`).
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
3. Pour `SL (1h)` ou `(15min)`, choisir le déclenchement : **à la clôture de la bougie**
   (par défaut, comme le signal) ou stop au prix, ce qui **change la stratégie source**.
   Une clôture sans durée reconnue (« candle close ») n'autorise que le stop au prix.
4. Simuler avec les règles et soldes Binance Demo ; confirmer dans les 120 secondes.
5. Le worker revalide solde, risque, prix, frais et expiration avant les achats.
   Redémarrer un ancien worker : une capacité `signal_v1` est exigée.

Les entrées LIMIT partagent également le budget, expirent après 24 heures,
et peuvent se remplir immédiatement si leur limite est au-dessus du marché.
Les TP représentent des parts égales de la position : le pourcentage transmis
au moteur existant est converti en pourcentage du restant, dernier TP à 100 %.
Le premier TP confirmé demande l'annulation des entrées encore ouvertes.

**Suivi du stop loss** (Settings → Signaux, activé par défaut, signaux manuels et automatiques) :
après TP1 le SL passe à l'Entry 1, il reste à l'Entry 1 après TP2, puis à partir de TP3 il suit
deux objectifs en arrière (TP3 → TP1, TP4 → TP2…). Désactivé : le SL reste au prix du signal
pendant tout le trade, et un SL de clôture de bougie le reste. Activé, un SL de clôture déplacé
devient un stop au prix sur Binance. La règle est enregistrée dans chaque TP à la création du trade :
changer le réglage ne modifie que les nouveaux trades, les trades ouverts gardent leur comportement.

**SL à la clôture de bougie** (`SL: 0.02446 (15m)`, `(1h)`, `(4H close)`, `daily close`…) :
aucun ordre stop n'est placé sur Binance, car il se déclencherait au toucher. Le worker lit chaque
bougie clôturée de l'intervalle (15m, 30m, 1h, 4h, 1d, 1w…). Si une bougie clôturée **après l'achat**
termine au niveau du SL ou dessous, il vend au marché la quantité nette de cette position (clôture
au marché : intention enregistrée, aucun renvoi d'une vente incertaine). La clôture fait foi : un
rebond ensuite ne l'annule pas, et la vente peut se faire sous le SL. La bougie en cours n'est jamais
jugée. Les bougies clôturées pendant un arrêt du worker sont rattrapées au redémarrage, mais **la
position n'est pas protégée tant que le worker est arrêté**. Si Settings → *Vendre au marché si le
stop est déjà franchi* est désactivé, la position est mise en pause avec une alerte critique au
lieu d'être vendue. Un déplacement manuel du SL le transforme en stop au prix. Le worker doit
annoncer la capacité `candle_stop_v1` (le redémarrer après mise à jour).
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
La politique automatique peut limiter le signal aux premières entrées et aux
premiers TP. Par défaut, elle conserve Entry1 et TP1/TP2, puis vend 70 % au
premier objectif et le reliquat au second. Le mode égal reste disponible.
Les répartitions des entrées et des TP peuvent aussi être personnalisées dans
Settings. Il faut fournir une valeur positive par niveau retenu et un total de
100 %, par exemple `30;70` pour deux entrées ou `70;30` pour deux TP.
La date Telegram (ou la date d'origine Telegram d'un transfert) est la référence de
fraîcheur et doit rester dans la fenêtre réglée, 5 minutes par défaut. Elle est exprimée
en temps Unix et ne dépend pas du fuseau du conteneur. La date écrite dans le texte reste
informative car les fournisseurs n'utilisent pas tous correctement les fuseaux horaires.
Chaque message éligible est ensuite **routé** (voir [Routage](#routage--confirmation-manuelle-ou-exécution-automatique)) :
seul un signal sans motif de revue part sans confirmation. Les messages anciens reçus
après l'activation, ambigus, d'un groupe non déclaré ou à risque élevé passent « À
confirmer » avec leurs motifs ; les messages reçus avant l'activation ne sont jamais
routés ; les contrats violés sont refusés. Aucun ne crée de commande sans décision.

Un SL portant une mention `1h` ou `15min` attend la clôture de sa bougie (voir plus haut), sauf
si l'option **Interpréter les SL temporisés comme des stops au toucher** est activée. Une
clôture sans durée reconnue reste refusée en automatique. Le worker
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

**Désactivé par défaut** : rien n'est écrit tant que « Retour d'exécution vers CSI »
(`signal_csi_feedback_enabled`, Settings → Signaux → Liens avec CSI) n'est pas activé ;
jamais en DRY_RUN.

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
- **Achat quand le TP1 est touché avant** (`signal_cancel_entry_if_tp1_first`) :
  désactivé par défaut, l'ordre d'achat d'un signal texte reste ouvert si le prix
  atteint le TP1 avant son exécution ; la position est marquée `tp1_avant_achat` et
  History compare ces achats tardifs aux autres. Activé : l'achat est annulé (le
  signal est parti sans toi). Un signal CSI annule toujours (contrat).
- **Canal perdant** (`signal_channel_review_enabled`, désactivé par défaut ;
  `signal_channel_review_min_trades`, 30 par défaut, de 10 à 500) : un signal dont le
  trader ou canal d'origine a au moins ce nombre de positions terminées et un résultat
  net négatif (frais BNB valorisés comme dans History) passe « À confirmer » avec le
  motif `C_CHANNEL_LOSING`. Nom inconnu ou trop peu de positions : aucun jugement.

- **Stop de secours chez Binance** (`signal_candle_backup_percent`, 3 % par défaut, de 0 à 20 ; 0 = aucun) :
  pour un signal dont le SL est « à la clôture de bougie » (« SL: 0.43 (1h) »), un ordre stop est aussi posé chez
  Binance à ce pourcentage sous le niveau de clôture. La clôture reste surveillée par le worker ; le stop de
  secours protège quand le worker est arrêté. Il est annulé avant chaque vente TP puis reposé, annulé avant la
  sortie à la clôture, et remplacé par un stop au prix si le SL est déplacé.
- **Filtre de liquidité** (`signal_liquidity_enabled`, activé ; `signal_min_volume_usdt`, 500 000 ;
  `signal_max_spread_percent`, 0,5) : volume 24 h trop bas (`R_LIQUIDITY`) ou écart achat/vente trop large
  (`R_SPREAD`) → « À confirmer » ; statistiques illisibles → `D_LIQUIDITY`, « À confirmer » aussi.

### Trader ou canal d'origine

Chaque signal garde le nom de sa source, dans cet ordre :

1. le **nom du trader écrit en tête du message** (`trader_name.py`) : « 👑 HAMZAWY 👑 »,
   « Trader/ Suhaib AlMashhadani », « Al-Afify Harmonic Indicator Ultra », « ABK SIGNAL ALERT »… Il
   survit à un relais qui recopie le texte sans le transférer, et distingue les traders d'un même
   canal (LEGEND TRADING en publie une vingtaine). Seules les lignes placées avant la paire ou la
   première étiquette comptent ; les formules (« بسم الله … ») et les lignes d'événement
   (« Harmonic Pattern Detected », « TIME-BASED TRADE DETECTED ») sont ignorées ; les préfixes
   (« Trader/ », « Ph. ») et suffixes (« Harmonic Indicator Ultra », « SIGNAL ALERT ») sont retirés ;
   la dernière ligne restante est le nom. Aucun nom n'est inventé. Mesure du 2026-10-05 sur les
   exports du propriétaire : un nom pour 867 signaux lisibles sur 874 ; les 7 autres n'en portent pas ;
2. sinon, pour un message **transféré**, le canal, le groupe ou la personne d'origine ;
3. sinon la conversation elle-même, ou le nom déclaré pour l'identifiant du chat (« telegram <id> »
   à défaut).

Le nom suit la position (groupe de source). Les variantes d'écriture sont regroupées
(« Suhaib Al-Mashhadani » = « SUHAIB ALMASHHADANI » ; « TRADING », « CRYPTO » et « TRADER » ignorés ;
« VIP » reste distinct). History → **Par trader ou canal** donne positions, part de gagnantes, gain et
perte moyens, PnL net et profit factor, frais compris ; les positions sans aucun achat exécuté ne
comptent pas. Une position ouverte avant cette version retrouve son trader dans le texte de son signal
(boîte des signaux) ; sans nom écrit, elle reste en « Telegram (canal inconnu) ».

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
- Avis CSI demandé (réglage) et défavorable, refusé, injoignable ou indéterminé (option) :
  « À confirmer » (voir [Avis CSI](#avis-csi-cryptosignalintelligence)).
- Message trop ancien ou non daté, SL sur clôture d'une bougie inconnue sans autorisation
  « au toucher » (une bougie connue, 15m, 1h, 4h…, est surveillée par le worker), mode `DEMO_MANUAL` (honoré par défaut : tout signal est manuel) : « À
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
| `VOLATILITY_REGIME=HIGH` (CSI) | revue si activée (désactivée par défaut, Settings → Signaux → Liens avec CSI) |
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

## Avis CSI (CryptoSignalIntelligence)

CryptoSignalIntelligence (projet voisin, « le cerveau ») évalue un signal sans jamais passer
d'ordre : vetos déterministes (signal déjà mort, paire hors univers, stop absurde…), contexte de
marché, **taux de base historique** de la même géométrie dans le même régime (TP1 avant stop,
espérance nette en R avec son intervalle) et bilan du groupe qui a émis le signal. Verdicts :
REFUSE, DEFAVORABLE, INDETERMINE, FAVORABLE. Un taux de base n'est jamais la probabilité que ce
signal réussisse ; INDETERMINE signifie « pas assez d'éléments », pas « 50/50 ».

- **Page CSI** : bouton *Vérifier* (signal collé → avis en une phrase, détail sur demande) ; liste des
  **signaux trouvés par CSI** (stratégies non validées) avec un bouton *Tester en Demo* qui les ajoute à la
  page Signaux, où le budget se choisit et se confirme à la main ; en détail : bilan des groupes Telegram,
  verdicts des stratégies de CSI, dernières évaluations, état de la surveillance.
- **Page Signaux** : bouton « Demander l'avis de CSI » sur chaque signal ; l'avis est conservé
  avec le signal (`csi_verdict`, `csi_detail`).
- **Paires hors de l'univers de CSI** : un signal **collé à la main** (page CSI, ou page Signaux
  avec un texte collé) vaut validation de sa paire par le propriétaire : CSI l'ajoute définitivement,
  répond `EN_ATTENTE` le temps de télécharger l'historique (quelques minutes), puis l'avis normal
  arrive au clic suivant. Un signal **reçu par Telegram** n'ajoute jamais de paire (`REFUSE`, retenu) :
  le coller sur la page CSI pour valider la paire.
- **Settings → Signaux → Liens avec CSI → Avis de CSI** (**désactivé par défaut** : aucun appel, aucun
  signal retenu à cause de CSI) : une fois activé, le worker demande l'avis de CSI
  avant de mettre en file un signal texte automatique (jamais pour un signal CSI V3, déjà produit
  par CSI). REFUSE et DEFAVORABLE sont **retenus** : « À confirmer » (`REVIEW`, motif de confiance
  `C_CSI_OPINION`), demandé avant tout appel Binance ; la confirmation manuelle reste possible sur
  la page Signaux. INDETERMINE peut aussi être retenu (option). CSI injoignable :
  retenu par défaut, ou exécuté sans avis si le réglage le permet. L'avis ne rend **jamais** un
  signal automatique : il ne peut que retenir. Les noms des groupes (`identifiant=nom`) servent au
  bilan par source chez CSI ; sans nom, la source est « telegram <identifiant> ».

Technique : `binance_spot_manager/csi_client.py` (client HTTP, `GatePolicy`), API locale de CSI
(`docs/API.md` de CryptoSignalIntelligence). Variables d'environnement : `BSM_CSI_API_URL`
(défaut `http://csi-api:8503` sous Compose, via le réseau Docker partagé `csi-bridge` créé par
`make network`) et `CSI_API_TOKEN` (facultatif, identique côté CSI). Les tests hors ligne n'ont
pas besoin de CSI : une panne est simulée, jamais un appel réseau.

## Non activé dans cette version

Stop de secours sur Binance pendant un SL à la clôture, remappage Bitget/forex et apprentissage
libre de formats. Le mode automatique actuel reste limité aux conversations Telegram
autorisées, au dépôt direct local et à Binance Demo Spot.

Tests hors réseau : `make test TESTS="tests/test_signal_sizing.py tests/test_signals.py tests/test_signals_ui.py tests/test_signal_drop.py tests/test_signal_csi.py tests/test_signal_routing.py tests/test_commands.py tests/test_automation.py tests/test_candle_stop.py"`.
