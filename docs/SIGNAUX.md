# Signaux texte — première version

La page **Signaux** conserve les messages dans `data/signals.sqlite3`, séparés
par compte/mode Demo. Aucun message n'est un ordre à lui seul. Le parseur est
déterministe : pas de modèle IA ni d'envoi du texte à un service tiers.
Chaque signal confirmé crée une stratégie avec son propre `position_id`, même
si d'autres stratégies suivent la même paire. Les entrées d'un même signal
restent regroupées dans cette stratégie. Les limites de risque cumulent les
positions de la paire ; les ordres existants ne sont pas transférés.

## Formats

Le parseur ne dépend pas d'un fournisseur : il lit les **étiquettes**, quel que soit l'habillage
(emojis, lignes de séparation, numérotation `1)`/`1️⃣`, pourcentages, flèches, `|`, plusieurs
étiquettes sur une seule ligne).

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

**Refusé plutôt que deviné** : deux prix pour un TP ou un SL, `or market`, `above`/`breakout`,
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
Les messages reçus avant l'activation, édités, anciens,
ambigus, hors Binance Spot ou incompatibles avec les règles sont conservés avec un
motif de refus et ne créent aucune commande.

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
- **Settings → Signaux → Avis CSI avant exécution automatique** : le worker demande l'avis de CSI
  avant de mettre en file un signal Telegram automatique. REFUSE et DEFAVORABLE sont **retenus**
  (état `REJECTED`, motif « Avis CSI … exécution automatique retenue ») : la confirmation manuelle
  reste possible sur la page Signaux. INDETERMINE peut aussi être retenu (option). CSI injoignable :
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
autorisées et à Binance Demo Spot.

Tests hors réseau : `make test TESTS="tests/test_signal_sizing.py tests/test_signals.py tests/test_signals_ui.py tests/test_commands.py tests/test_automation.py tests/test_candle_stop.py"`.
