# Politique de sécurité

## Signaler une vulnérabilité

**Ne publiez jamais une vulnérabilité dans une issue publique**, une discussion ou une demande de fusion.

Passez par les avis de sécurité privés de GitHub :

1. onglet **Security** du dépôt ;
2. bouton **Report a vulnerability** ;
3. décrivez le problème, la version (commit) concernée, les étapes pour le reproduire et l'effet constaté.

Le rapport n'est visible que du mainteneur et de vous. Si le bouton n'apparaît pas, ouvrez une issue publique
**sans aucun détail technique** qui demande seulement un moyen de contact privé.

N'incluez jamais de vraie clé, de vrai jeton, de mot de passe ni de capture de solde dans un rapport : une valeur
factice suffit.

## Ce qui est concerné

BinanceSpotManager manipule des clés API (Binance Demo), des jetons Telegram et des comptes de connexion. Sont
notamment des vulnérabilités :

- un **contournement du verrou Demo** : écriture (ordre, annulation, protection, vente) acceptée vers une URL hors
  de `ALLOWED_DEMO_BASE_URLS`, mode `LIVE` qui ne retombe pas en `DRY_RUN`, appel qui échappe à
  `assert_write_allowed()` ;
- une **fuite de secret** : clé ou secret API, jeton Telegram, mot de passe SMTP, clé maîtresse du coffre, clé
  privée de licence, visible dans l'interface, un journal, une notification, une sauvegarde ou l'image Docker ;
- une **injection par un signal** : message Telegram, fichier déposé dans `data/signal_drop/` ou signal TXT de
  CryptoSignalIntelligence qui déclenche un ordre non voulu, contourne le routage « À confirmer », les limites de
  risque ou la déduplication, ou fait exécuter du code ;
- les **commandes Telegram** (`/pause`, `/reprise`, `/statut`) acceptées depuis un autre compte que celui du
  propriétaire ;
- l'**interface** : accès sans connexion quand un compte existe, contournement du TOTP ou du blocage après échecs,
  session qui n'expire pas, XSRF, port publié ailleurs que sur `127.0.0.1` par défaut, proxy Caddy sans mot de passe ;
- le **coffre de clés** (AES-256-GCM) ou la **licence de location** (Ed25519) : déchiffrement ou licence forgée ;
- un **second worker** qui contourne le verrou et agit sur les mêmes données.

Ne sont pas des vulnérabilités : un trade perdant, un écart de prix entre Binance Demo et le marché réel, une
indisponibilité du testnet. Ce sont des sujets d'issue ordinaires.

## Délai de réponse

Le projet est maintenu par une seule personne, sur son temps libre. À titre indicatif et **sans engagement** :
accusé de réception sous une semaine environ, premier avis sous un mois. Merci de laisser un délai raisonnable
avant toute publication, le temps qu'un correctif soit disponible.

Seule la branche `main` reçoit des correctifs.

## Rappels

- **Aucune clé dans le code.** Les clés Demo vivent dans `.env` ou dans le coffre local chiffré
  (`data/key_vault.json`, clé maîtresse hors des données) ; elles ne sont jamais affichées par l'application.
- **Les fichiers `.env` ne sont jamais versionnés** (seul `.env.example`, sans valeur réelle, l'est) ni copiés dans
  l'image Docker.
- N'affichez jamais `docker compose config` sans `--quiet` : cette commande recopie le contenu de `.env` à l'écran.
- Une clé API Binance doit avoir les retraits désactivés et, si possible, une restriction d'adresse IP.
- Si une clé ou un jeton a pu fuiter, considérez-le comme compromis et renouvelez-le, même après correction.
- Durcissement d'un serveur : [docs/SECURITE_VPS.md](docs/SECURITE_VPS.md).
