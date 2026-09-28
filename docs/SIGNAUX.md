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
2. Choisir le budget en devise de cotation (USDT ou USDC). Pas de budget implicite.
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
**Signaux → Relever les messages Telegram** importe à la demande, sans achat.

Le bot doit avoir accès aux messages du groupe/canal, ou recevoir un transfert
dans une conversation autorisée. Ce n'est pas un accès aux abonnements personnels
Telegram. Un seul consommateur `getUpdates` par bot ; un webhook actif empêche
ce mode. L'offset est conservé après stockage ; les messages hors liste sont
ignorés. Les éditions invalident l'ancienne version locale, sans modifier ni
annuler les ordres déjà transmis. Elles demandent une vérification manuelle.

Références : [Bot API getUpdates](https://core.telegram.org/bots/api#getupdates),
[messages accessibles à un bot](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

## Non activé dans cette version

Réception en tâche de fond, achats automatiques à la réception, suivi des clôtures
de bougie, remappage Bitget/forex et apprentissage libre de formats. L'automatisation
sans confirmation nécessite d'abord des politiques par source (budget, durée de
validité, allocations, interprétation SL et gestion des modifications).

Tests hors réseau : `python -m pytest tests/test_signals.py tests/test_signals_ui.py tests/test_commands.py tests/test_automation.py`.
