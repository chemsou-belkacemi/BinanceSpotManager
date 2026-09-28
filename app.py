"""BinanceSpotManager — point d'entree Streamlit.

Lance :
    streamlit run app.py

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
Ouvre une page dans le menu de gauche.

**New Trade** construit une position complète (Entries, TP, SL) avec simulation
avant lancement. **Investissement** permet un achat simple sans sortie ou un
achat avec TP seul / SL seul. **Dashboard** suit le worker, le portefeuille
multidevise et les ordres réels. **Positions** détaille chaque position,
**History** conserve les positions terminées, **Settings** regroupe les réglages.
**Signaux** analyse un texte collé ou importé de Telegram, puis prépare un plan
Demo à confirmer avant transmission au worker.
"""
)

col1, col2 = st.columns(2)

with col1:
    st.subheader("Avant de commencer")
    st.markdown(
        """
1. Copier `.env.example` en `.env` et renseigner les clés Demo.
2. Vérifier la connexion : `python scripts/check_connection.py`
3. Lancer cette interface : `streamlit run app.py`
4. Démarrer le worker depuis **Dashboard** (ou `python scripts/bot_worker.py`).
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
