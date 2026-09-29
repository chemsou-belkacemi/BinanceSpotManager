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

## Dépôt direct (générateur ML)

Un projet local séparé (« MLSignalGenerator », dans son propre conteneur sur le
même hôte) peut remettre ses signaux au bot sans passer par Telegram. Il monte le
volume Docker `binance-spot-manager_bsm-data` et tourne avec l'uid `10001` (bsm).

### Contrat v1

- Répertoire `data/signal_drop/` avec `incoming/`, `processed/` et `rejected/`,
  créé par le worker au démarrage. Le producteur écrit **uniquement** dans
  `incoming/`.
- Un fichier par signal, écrit atomiquement : `*.tmp` puis renommage en `*.json`.
  Tout fichier ne se terminant pas par `.json` est ignoré ; un fichier de plus de
  64 Ko est déplacé dans `rejected/`.
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
la fenêtre de fraîcheur de l'exécution automatique (5 minutes par défaut). La
déduplication du signal et la clé de commande stable empêchent un second envoi.
Le worker revalide ensuite prix, soldes, réserve, frais et risque avant toute
écriture, uniquement sur Binance Demo. Sans exécution automatique, un signal
déposé se revoit et se confirme dans la page **Signaux** comme un message Telegram.

## Non activé dans cette version

Suivi des clôtures de bougie, remappage Bitget/forex et apprentissage libre de
formats. Le mode automatique actuel reste limité aux conversations Telegram
autorisées, au dépôt direct local et à Binance Demo Spot.

Tests hors réseau : `make test TESTS="tests/test_signal_sizing.py tests/test_signals.py tests/test_signals_ui.py tests/test_commands.py tests/test_automation.py"`.
