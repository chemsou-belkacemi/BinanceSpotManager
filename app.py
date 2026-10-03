"""BinanceSpotManager — point d'entree Streamlit.

Lance (dans le conteneur `ui`) :
    make up

Le package doit etre importable : le projet est concu pour etre execute depuis
sa racine (les scripts ajoutent la racine au sys.path automatiquement).
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.config import get_settings  # noqa: E402
from binance_spot_manager.event_store import configure_logging  # noqa: E402
from ui_common import banner, page_header, sidebar_status  # noqa: E402

from ui_common import require_login  # noqa: E402

# Connexion exigee avant tout affichage (comptes : scripts/creer_compte.py).
require_login()

configure_logging()

settings = get_settings()

st.set_page_config(
    page_title="BinanceSpotManager",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

page_header(
    "BinanceSpotManager",
    "Gestionnaire intelligent de positions Spot — mode Demo",
)

banner(settings)
sidebar_status(settings)

st.markdown(
    """
### Par où commencer

1. **CSI** : colle un signal et clique sur *Vérifier* pour avoir l'avis de CSI en une phrase.
   Tu y vois aussi les signaux que CSI a trouvés lui-même, avec un bouton *Tester en Demo*.
2. **Signaux** : pour exécuter un signal sur Binance Demo (budget, simulation, confirmation).
3. **Dashboard** et **Positions** : suivre ce qui tourne.

Les autres pages : **New Trade** (position construite à la main), **Investissement** (achat simple),
**History** (positions terminées), **Operations** (demandes envoyées au worker), **Settings** (réglages).
"""
)

with st.expander("Comment ça marche (2 minutes de lecture)"):
    st.markdown(
        """
**Deux programmes travaillent ensemble.**

- **CSI** (le cerveau) analyse les marchés toutes les 15 minutes et vérifie les signaux qu'on lui donne.
  Il ne passe **jamais** d'ordre.
- **Ce bot** (les mains) passe les ordres sur **Binance Demo** uniquement : entrée, stop, objectifs,
  stop remonté après un objectif.

**Vérifier un signal Telegram** : page **CSI** → coller → *Vérifier*. Réponses possibles :
*Ne pas prendre* (signal invalide ou dépassé), *Déconseillé* (ce type de trade a perdu en moyenne),
*Pas d'avis clair* (le passé ne permet pas de conclure, ce n'est pas du 50/50), *Plutôt bon*
(ce type de trade a gagné en moyenne, sans garantie). Une paire inconnue de CSI est ajoutée :
redemande l'avis quelques minutes plus tard.

**Signaux reçus automatiquement par Telegram** : si l'exécution automatique est activée
(Settings → Signaux), CSI donne son avis avant l'ordre. Un avis défavorable **retient** le signal :
il attend ta confirmation sur la page **Signaux**. CSI ne peut jamais envoyer un ordre tout seul.

**Signaux trouvés par CSI** : affichés sur la page CSI avec le bouton *Tester en Demo*. Les stratégies
de CSI ne sont **pas validées** (elles perdent en moyenne sur 5 ans) : c'est pour observer et tester.

**Chaque groupe Telegram** est suivi : au bout de 20 signaux résolus, CSI dit s'il fait mieux que le
hasard (page CSI → Détails).

**Démarrer tout d'un coup** (PowerShell, dossier CryptoSignalIntelligence) :
`.\\scripts\\demarrer.ps1`. Si le PC se met en veille, tout s'arrête ; tout repart au réveil.
"""
    )

col1, col2 = st.columns(2)

with col1:
    st.subheader("Avant de commencer")
    st.markdown(
        """
1. `make init`, puis saisir les clés Demo dans **Settings → Sécurité** (chiffrées sur place)
   ou dans `.env`.
2. Vérifier la connexion : `make check`
3. Lancer l'interface et le worker : `make up`
4. Suivre le worker depuis **Dashboard** (`make logs` pour les journaux).
"""
    )

with col2:
    st.subheader("Garde-fous actifs")
    st.markdown(
        f"""
- Écritures limitées à **{settings.base_url}**
- Mode opérationnel : **{settings.run_mode.value}**
- Mode Live **désactivé** dans cette version
- Toute écriture hors Demo est refusée par le code
"""
    )

st.divider()
st.caption(
    f"Version {__import__('binance_spot_manager').__version__} — "
    f"environnement {settings.environment.value}"
)
