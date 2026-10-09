# Contribuer à BinanceSpotManager

Merci de votre intérêt. Avant tout, sachez ce qu'est ce projet :

- un **gestionnaire de positions Spot** (entrées, objectifs, stop, signaux Telegram ou de CryptoSignalIntelligence)
  avec une interface Streamlit et un worker séparé ;
- **verrouillé sur Binance Demo** (testnet) : toute écriture vers une URL hors de la liste blanche Demo est refusée,
  et le mode Live n'est pas implémenté (`BSM_RUN_MODE=LIVE` est ramené à `DRY_RUN`). Aucun passage en réel n'est
  prévu dans ce dépôt sans décision explicite de son propriétaire ;
- un outil d'exécution et de gestion du risque, **pas une stratégie** : rien ici ne démontre ni ne promet un gain.

Le projet est écrit **en français** : interface, commentaires, documentation, issues et demandes de fusion.
Merci d'écrire en français vous aussi.

À lire avant une contribution importante : le [README](README.md) (sections 7 à 9 : modes, architecture, choix
techniques), [docs/SIGNAUX.md](docs/SIGNAUX.md), [docs/AUDIT_SECURITE_2026-09-28.md](docs/AUDIT_SECURITE_2026-09-28.md)
et [docs/OPERATIONS_V2.md](docs/OPERATIONS_V2.md).

## Installer pour développer

### Sans Docker (Linux)

Python **3.14** ou **3.12** (les deux sont testés par la CI ; l'image Docker utilise 3.12). Sous Ubuntu, le paquet
`python3.14-venv` fournit le module `venv`.

```bash
git clone <url-du-dépôt> BinanceSpotManager
cd BinanceSpotManager
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Inutile d'activer le venv : on appelle `.venv/bin/python`, `.venv/bin/streamlit`… Aucune clé n'est nécessaire pour
développer ni pour lancer les tests hors intégration. Sans `.env`, la configuration vaut `BSM_ENV=DEMO` et
`BSM_RUN_MODE=DRY_RUN`.

### Avec Docker

`make test` lance les tests hors ligne dans un conteneur jetable, sans volume ni secret (voir la section 10 du
README). `make help` liste toutes les commandes.

## Tests et contrôles

```bash
.venv/bin/python -m pytest -m "not integration"                       # tests hors ligne (comme la CI)
.venv/bin/python -m pytest -m "not integration" tests/test_engines.py # un fichier
make integration                                                       # Binance Demo, lecture seule, clés .env
```

- `tests/conftest.py` **bloque tout accès réseau** hors des tests marqués `integration` (requests, urllib, SMTP) :
  aucun test ordinaire ne doit parler à Binance, à Telegram ni à un serveur de courriel. Un test qui en a besoin
  simule le client.
- La CI GitHub Actions (`.github/workflows/tests.yml`) lance `pytest -m "not integration"` sous Ubuntu et Windows,
  Python 3.12 et 3.14, puis `pip-audit` sur `requirements.txt`, puis la construction et les tests de l'image Docker.
- **Pas de ruff ni de mypy dans ce dépôt** : aucune configuration, rien dans la CI. Ne reformatez pas des fichiers
  entiers ; gardez le style du fichier que vous modifiez.

## Style

- **Français** pour l'interface, les messages, les commentaires et la documentation.
- Prix et quantités en `Decimal`, arrondis par `tickSize` et `stepSize` lus chez Binance, jamais codés en dur ;
  quantités toujours arrondies vers le bas.
- Le Strategy Engine et le Position Engine ne touchent jamais au réseau ; seul l'Execution Engine envoie des ordres,
  et toute écriture passe par `settings.assert_write_allowed()` dans le client Binance.
- **Tests obligatoires** pour tout calcul de prix, de quantité, d'arrondi, de frais, de risque, de taille ou de
  résultat (PnL), et pour toute modification du routage des signaux ou des garde-fous.
- Tout nouveau lien automatique avec CryptoSignalIntelligence est **désactivé par défaut**
  (`tests/test_csi_desactive_par_defaut.py`).

## Règles qui ne se négocient pas

1. **Binance Demo uniquement.** La liste blanche `ALLOWED_DEMO_BASE_URLS` (`binance_spot_manager/config.py`) ne
   s'élargit pas ; `assert_write_allowed()` reste appelé avant toute écriture ; `LIVE` reste ramené à `DRY_RUN`.
   Aucune contribution n'ajoute un mode réel, une URL de production pour les écritures ou un moyen de contourner
   ce verrou.
2. **Aucun secret en dur** : ni clé API, ni jeton Telegram, ni mot de passe, ni clé de licence dans le code, les
   tests, la configuration ou les scripts. Les secrets vivent dans `.env` (jamais versionné, seul `.env.example`
   l'est) ou dans le coffre local chiffré ; les tests utilisent des valeurs factices.
3. **Aucun test ne passe d'ordre** ni n'envoie de vrai message ; les tests d'intégration restent en lecture seule.
4. **Idempotence et prudence** : une écriture dont le résultat est incertain n'est jamais renvoyée automatiquement.

## Proposer une modification

1. Ouvrez d'abord une issue (modèle « Idée » ou « Bug ») pour tout changement de comportement du worker, du routage
   ou des garde-fous : on en discute avant le code.
2. Créez une branche depuis `main` : `feat/…`, `fix/…`, `docs/…`, `test/…`.
3. Commits courts, en français, au format `type(portée): description`.
4. Avant d'ouvrir la demande de fusion : `.venv/bin/python -m pytest -m "not integration"` vert.
5. Décrivez dans la demande de fusion ce qui change, pourquoi, comment c'est testé, et cochez la liste du modèle.
   Si l'interface change, joignez une capture **sans clé, solde réel ni identifiant**.
6. Rien n'est fusionné sans la relecture du propriétaire du dépôt.

## Ce qui sera refusé

- Tout passage en réel : mode Live, URL de production pour les écritures, élargissement de la liste blanche Demo,
  contournement de `assert_write_allowed()`.
- Un secret, un jeton ou un fichier `.env` dans le dépôt.
- Toute phrase, tout chiffre ou toute capture qui promet un gain, ou des réglages présentés comme « optimisés »
  sur l'historique.
- Un calcul de prix, de quantité, de frais ou de risque sans test.
- Un test qui touche le réseau hors du marqueur `integration`.

## Licence

En contribuant, vous acceptez que votre contribution soit publiée sous la licence du projet,
GNU AGPL-3.0-or-later ([LICENSE](LICENSE)).
