# Audit de fiabilité — BinanceSpotManager Demo

Date : 28 septembre 2026. Périmètre : code local, interface Streamlit,
worker, stockage, exécution, rapprochement Binance, dépendances et tests.
Ni `.env` ni `.venv` n'ont été modifiés. Aucune activation du Live.

## Verdict

Le bot dispose d'une base fonctionnelle et de protections utiles, mais **n'est
pas encore un produit prêt à commercialiser**. Cet audit renforce les cas où
l'état Binance est inconnu, où deux processus travaillent simultanément et
où une vente n'est que partiellement exécutée. Ce n'est pas une certification
de sécurité ni une garantie d'exécution future.

L'OCO actuellement suivi reste un **OCO unique, avec un seul TP**. Le plan
multi-OCO visible dans l'interface est un aperçu : l'exécution de plusieurs
tranches avec déplacement coordonné des SL reste à construire et valider.

## Architecture actuelle

| Élément | Responsabilité |
| --- | --- |
| `app.py`, `pages/`, `ui_common.py` | Saisie, simulations, portefeuille, diagnostics, alertes |
| `dashboard_service.py` | Lectures et agrégations pour l'interface |
| `strategy_engine.py`, `risk_engine.py` | Calcul du plan et limites de risque |
| `position_engine.py`, `models.py` | État métier, quantités et résultats |
| `execution_engine.py`, `binance_client.py` | Contrôles d'exécution et transport REST Demo |
| `scripts/bot_worker.py`, `automation_engine.py` | Surveillance autonome, TP locaux et SL, lecture des branches OCO |
| `reconciliation_engine.py`, `exit_diagnostics.py` | Comparaison entre données locales et Binance |
| `market_price_stream.py` | Prix WebSocket avec repli REST si nécessaire |
| `position_store.py`, `file_mutex.py` | Fichiers JSON atomiques, versions et verrou système |
| `order_journal.py` | Réservation durable des identifiants avant création d'ordres |
| `event_store.py`, `notification_engine.py` | Journal et notifications |

L'interface et le worker sont deux processus séparés. Fermer le navigateur
n'arrête pas le worker. Les TP locaux ont besoin du worker ; les ordres déjà
acceptés par Binance restent gérés par Binance, même quand le worker s'arrête.
Un stop-limit peut toutefois se déclencher sans trouver d'acheteur à sa limite.

## Corrections réalisées pendant cet audit

| Risque constaté | Correction appliquée |
| --- | --- |
| Deux workers pouvant franchir simultanément le contrôle PID | Verrou interprocessus détenu par le système, libéré à la mort du processus |
| Réémission d'un ordre après timeout ou redémarrage | Identifiant réservé dans SQLite avant le POST ; un identifiant déjà réservé n'est pas renvoyé |
| Annulation inconnue assimilée à une annulation réussie | Confirmation d'un état terminal sans exécution obligatoire avant remplacement |
| SL partiellement vendu traité comme une clôture totale | Enregistrement cumulatif du vendu et des frais ; conservation du reliquat ; blocage des TP concurrents |
| Écrasement d'un état récent par une vieille copie Streamlit/worker | Révision de position, contrôle avant sauvegarde et verrou d'écriture |
| Perte du fichier précédent | Instantané local de la version précédente, renouvelé au plus une fois par minute |
| Création malgré un fichier de position illisible | Refus de nouvelle position et du calcul de risque incomplet |
| Diagnostic « lecture seule » modifiant l'objet local | Copie de travail et absence de journalisation métier avec `apply=False` |
| SL indépendant ou nouveaux achats sur une position OCO sans redimensionnement sûr | Refus explicite de ces chemins d'action |
| Reprise d'un achat sans ses commissions | Récupération des exécutions via l'historique avant calcul de quantité nette |
| Récupération des frais incomplète | Blocage du calcul plutôt qu'invention d'une quantité disponible |
| Fenêtre de crash avant sauvegarde des entrées | Identifiants et état de soumission enregistrés avant envoi ; sauvegarde après chaque réponse |
| New Trade lancé sans worker frais | Blocage des envois Demo tant que le worker n'est pas actif et à jour |
| Écritures directes possibles en DRY_RUN | Refus au niveau du transport, pas seulement dans le moteur d'exécution |
| Requêtes signées hors Demo et redirections HTTP | Refus hors liste blanche ; redirections désactivées |
| URL signée dans une exception réseau | Journalisation du type d'erreur, sans URL ni signature |
| Secrets dans une représentation/sérialisation de Settings | Champs sensibles exclus des représentations et exports du modèle |
| Prix non finis et données numériques invalides | Validation renforcée des modèles, arrondis et prix reçus |
| Capital global nul accepté par le contrôle de risque | Refus au lieu d'un simple avertissement |
| Erreur d'automatisation masquée par un état worker normal | État sauvegardé, erreur remontée au suivi du worker |
| Relecture de tout le journal à chaque affichage | Lecture de la fin du fichier ; rotation de `bot.log` |
| Exposition réseau de l'interface par défaut | Configuration Streamlit sur `127.0.0.1`, CORS et protection XSRF activés |
| Tests pouvant accidentellement utiliser REST | Transport HTTP bloqué dans les tests hors intégration ; test d'interface isolé du portefeuille réel |
| Régressions futures non contrôlées automatiquement | Workflow GitHub Windows/Linux, Python 3.12/3.14 et audit des dépendances |
| Ancienne version de pytest vulnérable sur Unix | Version minimale déclarée relevée à 9.0.3 ; tests exécutés avec pytest corrigé en répertoire temporaire |

### Limites importantes des nouveaux garde-fous

- Le journal d'intentions prouve une **tentative réservée**, pas la réception
  par Binance. Un crash avant le POST peut donc bloquer une action jamais envoyée.
  C'est volontairement conservateur : ne pas supprimer le journal pour réessayer.
  Même après un refus définitif, le même identifiant n'est pas réutilisé.
- La révision empêche les sauvegardes périmées ; elle ne rend pas atomiques une
  requête réseau et un fichier local. Un conflit impose relecture et rapprochement.
- Les verrous couvrent le même répertoire local. Deux copies de l'application
  utilisant le même compte, ou deux machines, ne sont pas coordonnées.
- Les sauvegardes locales ne remplacent pas une sauvegarde externe. Il n'y a
  pas de restauration automatique : un ancien état ne doit pas réémettre un ordre.
- Le rapprochement historique n'essaie pas de réparer un OCO : il renvoie vers
  le diagnostic dédié et le suivi des deux branches.
- La récupération des commissions est actuellement limitée à 1 000 exécutions
  par ordre. Un historique incomplet bloque la reprise ; la pagination reste à faire.
- Une vente SL partielle encore ouverte continue d'être surveillée ; une vente
  partielle terminale met l'automatisation en pause pour contrôler le reliquat.

## Vérifications effectuées

- Suite finale sur Windows / Python 3.14 / pytest 9.1.1 : **262 tests réussis,
  1 ignoré**. Le test ignoré exige l'activation explicite de `/order/test`.
  Les 249 tests hors intégration n'utilisent pas le transport REST réel.
- Tests hors intégration avec transport REST interdit : arrondis, risque,
  fees, timeout, reprise d'identifiant, annulation ambiguë, persistance,
  conflit de révisions, lecture seule, SL partiel puis complet, interface.
- Tests de verrou sur deux objets et dans un processus Python distinct.
- Tests d'intégration Demo en lecture seule : aucune création d'ordre de test.
- Audit des dépendances résolues depuis `requirements.txt` : aucune
  vulnérabilité connue signalée par pip-audit au moment du contrôle.
- Contrôle des branches OCO existantes, puis redémarrage du worker.
  Sur le contrôle de 01:33 UTC, les ordres BTCUSDT `68319602936` (TP) et
  `68319602935` (SL) étaient `NEW`, à `0.00034 BTC`, et cohérents avec le local.
  Le worker a repris en `MONITORING`, sans erreur à cet instant.
  Un second contrôle après plus de 160 boucles a confirmé les mêmes ordres,
  un état local `SYNCED` et aucune erreur de stockage ou du worker.

Les résultats de marché ci-dessus sont des instantanés, pas une surveillance
permanente ni une garantie de vente à un prix donné. Aucun nouvel ordre n'a
été volontairement créé pour cet audit. Les workflows CI sont ajoutés au
projet mais n'ont pas encore été exécutés par GitHub.

## Feuille de route priorisée

### P1 — À terminer avant d'élargir les scénarios de trading

1. **Base transactionnelle pour positions et commandes.** Migrer le stockage
   métier vers SQLite, versionner le schéma, garder un journal de transitions
   et une file de commandes unique UI → worker. Objectif : un seul acteur
   exécute les décisions et réserve les fonds, même avec plusieurs onglets.
2. **Reprise des intentions incertaines.** Écran listant les intentions,
   rapprochement REST, décision explicite de reprise après preuve, jamais un
   bouton supprimant aveuglément les identifiants de sécurité.
3. **Multi-TP OCO réel.** Une tranche par OCO, quantités nettes arrondies,
   validation des minima par tranche et des quotas d'ordres. Après TP1,
   recalculer puis annuler/confirmer/remplacer les SL des tranches restantes.
   Tester les fills intervenant pendant une annulation et chaque point de crash.
4. **Cycle de protection plus robuste.** Alerte durable de protection absente,
   reprise d'un SL partiellement rempli, gestion explicite du stop déclenché
   mais non exécuté. Un fallback Market éventuel doit être un choix utilisateur.
5. **Réservation du capital et inventaire.** Éviter que deux stratégies ou achats
   simples utilisent les mêmes fonds ; séparer quantités propres au bot,
   achats externes et quantités verrouillées par les ordres.
6. **Validation juste avant envoi.** Rafraîchir prix, budget, filtres et
   disponibilité ; appliquer un âge maximal du plan et une tolérance de prix
   configurables. Bloquer un plan périmé plutôt que l'exécuter silencieusement.
7. **Comptabilité complète des frais.** Frais en actif de base, cotation et BNB,
   coût d'acquisition réparti entre vendu et restant, historique paginé,
   conversion EUR horodatée et résultats réalisés/non réalisés explicites.
8. **Flux privé des exécutions.** Ajouter les événements d'ordres utilisateur
   avec reconnexion et rattrapage REST ; conserver REST comme contrôle, pas
   comme unique polling rapide pour toutes les positions.
9. **Budget API partagé.** Limites communes UI/worker, prise en compte des
   en-têtes de consommation, backoff avec jitter, cache des filtres et soldes,
   priorité aux actions de protection pendant une limitation.

### P2 — Exploitation locale fiable et expérience utilisateur

10. **Tableau de santé.** Distinguer worker vivant, connexion disponible,
    prix frais, position protégée et état d'exécution connu. Ne pas afficher
    « connecté » comme preuve d'une protection globale.
11. **Notifications fiables.** Déduplication persistante par événement,
    niveaux d'urgence, acquittement, historique, reprise après reconnexion.
    Les notifications navigateur dépendent toujours des permissions et du système.
12. **Sauvegarde/restauration vérifiée.** Rotation, export externe chiffré,
    contrôle d'intégrité et exercice de restauration avec rapprochement Demo.
    *Suivi du 5 octobre 2026 : rotation et sauvegarde chiffrée (`make backup-chiffre`),
    rapatriement sur le PC avec contrôle de déchiffrement (`scripts/recuperer_sauvegardes.sh`),
    restauration pas à pas avec reprise en veille ([SECURITE_VPS.md](SECURITE_VPS.md)). Reste :
    restauration outillée, signature des sauvegardes.*
13. **Journal exploitable.** Identifiant de corrélation par action, recherche,
    export, rotation aussi des erreurs, espace disque minimum et alerte de saturation.
14. **Gestion des conflits dans l'UI.** Recharger proprement une position après
    conflit de révision, afficher l'état relu, demander une nouvelle confirmation
    d'action sans renvoyer automatiquement une commande.
15. **Déploiement reproductible.** Versions verrouillées et empreintes,
    dépendances développement séparées, mise à jour testée, migration réversible,
    packaging et démarrage fiable sans fenêtres de terminal.
16. **Tests de panne et d'endurance.** Réseau coupé, erreur 429/418, disque plein,
    fichier corrompu, crash à chaque transition, redémarrage Windows,
    plusieurs onglets et sessions, boucle prolongée, surveillance des ressources.
17. **Clarté des formulaires.** Indiquer explicitement TP en pourcentage du
    restant, différence TP local/OCO/GTC, SL optionnel, capital non protégé et
    poussières non vendables. Le mode achat simple n'est pas une position protégée.

### P3 — Prérequis à un produit commercial

18. Comptes utilisateurs, authentification forte, autorisations et isolation
    stricte de chaque compte et de ses données ; jamais une clé partagée.
19. Coffre de secrets, chiffrement, rotation, audit des accès, restrictions de
    clés API et absence de permission de retrait.
20. Hébergement HTTPS, supervision externe, sauvegardes hors machine,
    continuité d'exploitation, procédure d'incident et mises à jour signées.
    *Suivi du 5 octobre 2026 : contrôle du serveur en lecture seule
    (`scripts/verifier_vps.sh` : SSH, ufw, fail2ban, mises à jour, ports Docker, droits) et
    sauvegardes chiffrées hors machine. Reste : supervision externe, procédure d'incident,
    mises à jour signées.*
21. Revue de sécurité indépendante et tests de charge représentatifs.
22. Analyse juridique adaptée au pays et au service vendu, conditions,
    confidentialité, suppression/export des données et limites de responsabilité.
23. Seulement ensuite : réflexion séparée sur le Live avec nouvelles limites
    et critères d'acceptation. **Aucun déblocage du Live dans cet audit.**

Signaux Telegram/TradingView, backtests, statistiques avancées et nouvelles
stratégies peuvent venir après ces fondations. Ils n'améliorent pas à eux
seuls la fiabilité d'exécution.

## Utilisation après cette mise à jour

- Redémarrer Streamlit pour recharger tous les modules et la configuration
  d'écoute locale. Le worker a déjà été redémarré pendant l'audit.
- Ne pas supprimer les fichiers de verrou ou `order_intents.sqlite3` pendant
  l'exécution. Ne pas relancer une action incertaine avec un nouvel identifiant
  avant d'avoir vérifié ce que Binance a réellement reçu.
- Les outils de contrôle corrigés ont été installés uniquement dans un
  répertoire temporaire, hors `.venv`. La version globale de pytest n'a pas
  été mise à jour ; le manifeste du projet exige maintenant `pytest>=9.0.3`.
- Relancer les tests hors ligne : `python -m pytest -m "not integration"`.
- Vérification Demo en lecture seule : `python -m pytest tests/test_demo_integration.py`.
- Pour la position existante : Settings → Diagnostic → comparaison des sorties.

## Références techniques consultées

- [Erreurs Binance Spot](https://github.com/binance/binance-spot-api-docs/blob/master/errors.md) :
  `-1006` et `-1007` n'établissent pas que l'ordre a échoué.
- [API REST Binance Spot](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md) :
  ordres, exécutions et annulations.
- [Publication pytest 9.0.3](https://github.com/pytest-dev/pytest/releases/tag/9.0.3) :
  correctif de sécurité pour les répertoires temporaires sur Unix.
