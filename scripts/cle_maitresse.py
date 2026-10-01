"""Crée la clé maîtresse du coffre si elle manque, et affiche son emplacement (jamais son contenu).

Usage :
    python scripts/cle_maitresse.py
Docker :
    make master-key    (seul ce service ponctuel peut écrire la clé ; l'interface et le worker
                        la montent en lecture seule)

La clé maîtresse chiffre les clés API Binance et les secrets TOTP de CETTE instance. Elle ne doit
être ni dans le dépôt, ni dans data/, ni dans une sauvegarde de données. La perdre rend le coffre
illisible : il suffit alors de ressaisir les clés API dans Settings et de recréer les comptes.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.key_vault import VaultError, load_master_key, master_key_path  # noqa: E402


def main() -> int:
    path = master_key_path()
    existed = path.exists()
    try:
        load_master_key(path, create=True)
    except VaultError as exc:
        print(f"Refusé : {exc}", file=sys.stderr)
        return 1
    print(f"Clé maîtresse {'déjà présente' if existed else 'créée'} : {path} (droits 0600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
