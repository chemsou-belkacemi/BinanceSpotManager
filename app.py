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

col1, col2 = st.columns(2)

with col1:
    st.subheader("Avant de commencer")
    st.markdown(
        """
1. `make init`, puis renseigner les clés Demo dans `.env`.
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
