# Améliorations opérationnelles V2 — état d'avancement

## Réalisé

1. **Exécution UI centralisée** : achats New Trade et Investissement, achat
   simple, annulations, déplacement de SL et fermeture locale utilisent une
   file SQLite consommée par le worker. Les scripts de maintenance existants
   restent des outils séparés et explicites.
2. **Reprise conservatrice** : identifiants persistés avant POST, états de
   demande persistants, interruption détectée au démarrage, aucun rejeu d'une
   commande incertaine. Lecture des intentions Binance depuis Operations.
3. **Protection lisible** : état local distinct d'une observation Binance
   récente ; contrôle renforcé des types, quantités et niveaux TP/SL, y compris
   le prix limite du stop. Pas de certification à partir du seul heartbeat.
4. **Frais/PnL** : coût d'achat incluant les frais en cotation, quantités nettes
   après commissions en base, répartition réalisé/non réalisé. Frais tiers non
   valorisés affichés et PnL marqué incomplet.
5. **Alertes persistantes** : historique SQLite, regroupement des erreurs
   répétées, acquittement, avertissement visible des alertes critiques non lues,
   déduplication des demandes de notification entre onglets.
6. **Sauvegardes vérifiables** : export ZIP à liste blanche, instantané SQLite
   via son API, empreintes SHA-256, validation des modèles et de l'intégrité des
   bases, rejet des chemins inattendus et archives trop volumineuses.
7. **Page Operations** : suivi actualisé des commandes ; consultations réseau
   seulement à la demande pour les intentions et le contrôle des protections.

Les pages conservent leur organisation actuelle. Les fragments Streamlit
isolent le suivi de commandes des formulaires ; les confirmations reposent sur
un identifiant stable, pas sur la durée d'un clic. La boîte de commandes est
durable ; la session Streamlit seule ne constitue pas une garantie de livraison.

## Ce qui n'est pas terminé

- **Multi-OCO exécuté et SL coordonnés** : toujours un aperçu. La création,
  l'annulation partielle, les courses avec un fill et le remplacement des autres
  tranches nécessitent un moteur dédié et des tests de panne avant activation.
  L'OCO unique existant n'est pas migré automatiquement.
- **Stockage métier totalement transactionnel** : les positions restent en
  JSON avec verrou/version. La file est transactionnelle, pas l'ensemble
  « Binance + JSON + commande ». Un conflit ou une réponse perdue se bloque
  pour vérification au lieu de garantir artificiellement un résultat.
- **Restauration automatique** : non implémentée. Une archive ancienne ne doit
  pas effacer les nouvelles intentions d'ordres ni relancer un ancien trade.
- **Frais tiers exacts** : taux historiques et pagination étendue des fills
  restent à compléter ; aucune conversion BNB/EUR historique n'est inventée.
- **Produit commercial multiutilisateur** : non implémenté. Le cache de service
  est partagé et le stockage de positions est monocompte. Il faut définir
  l'hébergement, l'identité, l'isolation des données et un coffre de secrets,
  puis faire une revue indépendante. L'écoute reste locale.
- **Planification et supervision** : une commande comportant plusieurs entrées
  peut prolonger un cycle du worker. Le stop-limit n'est pas une garantie de
  prix/exécution, et aucune cadence d'une seconde n'est garantie sous charge.
- **Sauvegarde distante/chiffrement/signature** : hors de cette implémentation.
  L'archive ne contient pas les alertes ni les journaux et doit rester privée.

## Vérification et remise en service

Validation finale du 28 septembre 2026 : **290 tests réussis, 1 test optionnel
ignoré**, avec Python 3.14 et pytest 9.1.1. Une commande `CHECK_CONNECTION` a
également traversé la file et le vrai worker jusqu'à `SUCCEEDED`, sans ordre.
Après redémarrage final, le worker était en `MONITORING` sans erreur et les
deux branches de l'OCO BTCUSDT existant étaient toujours `NEW` et cohérentes
au contrôle de 02:12 UTC. Ce résultat est un instantané.

Les tests couvrent : concurrence de soumission et de prise de commande,
expiration, retrait avant exécution, compte/mode distincts, revalidation du
prix/risque/solde, réponse d'achat perdue, annulation incertaine, fermeture avec
TP inconnu, frais, reprise des alertes, archives altérées et parcours de la
nouvelle page depuis le point d'entrée Streamlit.

Relancer : `make test`.
Les tests Demo en lecture seule sont séparés ; aucun ordre de test n'est envoyé
automatiquement. Redémarrer le worker après mise à jour, puis vérifier les
sorties existantes dans Operations. Ne pas modifier les clés de compte tout
en conservant des positions actives dans le même répertoire.

La prochaine étape technique est le moteur multi-OCO sur cette base,
pas l'activation du Live. `.env` et `.venv` restent inchangés.
