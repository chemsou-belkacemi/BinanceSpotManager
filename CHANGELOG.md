# Journal des modifications

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/), simplifié : Ajouté / Modifié / Corrigé.
Pas de numéro de version : le projet avance par étapes datées sur `main`. Tout fonctionne sur **Binance Demo**
uniquement ; aucune entrée de ce journal n'annonce ni ne démontre un gain. Détail : `git log`.

## Non publié

- Ajouté : `CONTRIBUTING.md`, `SECURITY.md`, ce journal, modèles GitHub, « Démarrage rapide » du README.

## 2026-10-08 — Licence, feu de protection CSI, liens CSI désactivés par défaut

### Ajouté
- Publication sous **GNU AGPL-3.0-or-later** (`LICENSE`).
- Garde-fou « Feu de protection CSI » (`GET /meteo` de CryptoSignalIntelligence), désactivé par défaut : au ROUGE,
  aucune nouvelle entrée automatique ; à l'ORANGE, taille réduite. Outil de gestion du risque, pas une stratégie.

### Modifié
- Tous les liens avec CryptoSignalIntelligence (avis, conseil de taille, revue « volatilité haute », retour
  d'exécution, feu) sont **désactivés par défaut**.

### Corrigé
- Feu CSI : écritures seulement sur changement, grâce de 15 minutes conservée après un redémarrage.

## 2026-10-06 — Taille selon le risque, trader perdant

### Ajouté
- Taille selon le risque (même perte au stop pour chaque signal, plafonnée), désactivée par défaut.
- Trader ou canal perdant : signaux « à confirmer » ou taille réduite au-delà d'un seuil choisi.
- Conseil de taille de CSI affiché (information, jamais appliqué).
- Interrupteurs « toutes les conversations autorisées de confiance » et « toutes les cryptos acceptées »,
  désactivés par défaut.

### Corrigé
- Parseur : cryptos d'une seule lettre (G, T, W…) lues comme dans CSI, jamais « w/ USDT ».
- Routage : risque au stop d'un SL à la clôture mesuré à son stop de secours.

## 2026-10-05 — Sécurité d'exploitation et suivi par trader

### Ajouté
- Alerte « bot muet » par un service de surveillance séparé (`watchdog`).
- Perte maximale du jour sur tout le portefeuille ; filtre de liquidité des signaux automatiques.
- Arrêt d'urgence par Telegram (`/pause`, `/reprise`, `/statut`) ; alertes de connexion à l'interface.
- Stop de secours chez Binance pour les SL à la clôture de bougie.
- Contrôle du VPS en lecture seule (`scripts/verifier_vps.sh`) et sauvegardes chiffrées hors du serveur.
- Résultats par trader ou canal (nom lu en tête du signal), rapport quotidien, protection en cas de chute de BTC,
  comparaison « BSM face au marché ».

### Corrigé
- Achat vu par la réconciliation → position ACTIVE ; reliquat sous les minimums Binance terminé proprement.
- Chemins d'échec du stop de secours, annulations en cours, arrêts courts (relecture indépendante).
- Aucun vrai message Telegram ni courriel depuis les tests (réseau bloqué hors intégration).

## 2026-10-01 au 2026-10-03 — Coffre, comptes, reprise sûre

### Ajouté
- Clés API chiffrées sur place (AES-256-GCM) avec une clé maîtresse séparée.
- Comptes de connexion (mot de passe scrypt et code TOTP) ; licence de location Ed25519 vérifiée hors ligne.
- Lecture générique des signaux par étiquettes ; stop loss à la clôture de bougie (15m, 1h, 4h…).
- Page d'accueil « Comment ça marche » ; progression de chaque groupe Telegram vers un avis de CSI.

### Corrigé
- Un seul achat par signal, même avec deux workers (`position_id` déterministe).
- Ordres `BSM-…` orphelins détectés : exécution automatique des signaux suspendue.
- SL incertain jamais arrivé chez Binance recréé une fois `recvWindow` passé ; reprise qui réconcilie les achats
  en attente.
- « Au marché » dans un signal change le sens d'un prix : le signal est refusé ; « Stop: 223. » accepté.
- TLS SMTP vérifié, proxys ignorés, conteneurs durcis.

## 2026-09-30 — Contrat CSI, routage, accès public

### Ajouté
- Consommateur du contrat TXT **V3** de CryptoSignalIntelligence (dépôt de fichiers), politiques de sortie à
  empreinte, retour d'exécution JSONL v2 (frais réels).
- Routage des signaux : exécution automatique ou « À confirmer » selon le risque et la confiance déclarée,
  coupe-circuits.
- Avis de CSI dans l'interface et, en option, avant l'exécution automatique.
- Accès public facultatif par un proxy Caddy (HTTPS, un identifiant par personne).
- Dépôt direct de signaux par fichiers pour un générateur local.

### Corrigé
- Positions dont toutes les entrées ont expiré sans achat terminées ; tranche de TP sous les minimums reportée
  sur le TP suivant.

## 2026-09-29 — Docker et signaux Telegram

### Ajouté
- Exécution complète sous Docker Compose avec un `Makefile` ; worker supervisé par son heartbeat.
- Exécution automatique des signaux Telegram (Demo) et politique de taille.

## 2026-09-27 et 2026-09-28 — Première version

### Ajouté
- Interface Streamlit (Dashboard, New Trade, Positions, History, Settings, Investissement, Operations) et worker
  séparé ; positions indépendantes, chacune avec son ID.
- Client Spot signé, filtres `tickSize` / `stepSize` / `minNotional`, prix en `Decimal`.
- Liste blanche Demo : toute écriture hors Binance Demo est refusée ; mode Live non implémenté.
- OCO Demo expérimental (un TP à 100 %), aperçu multi-OCO, investissement avec TP seul ou SL seul.
- File de commandes persistante, journal d'intentions SQLite, réconciliation avec Binance, alertes, frais BNB.
- Flux de prix `miniTicker` de l'environnement Demo, avec repli sur REST.

### Corrigé
- SL ou TP refusé de façon certaine retenté avec un nouvel identifiant ; notifications répétées limitées.
- Vente au marché quand Binance refuse un stop déjà franchi.
