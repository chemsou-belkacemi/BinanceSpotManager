"""Création d'un compte de connexion à l'interface (mot de passe + code TOTP).

Usage :
    python scripts/creer_compte.py <identifiant>
    python scripts/creer_compte.py <identifiant> --remplacer   # nouveau mot de passe + nouveau TOTP
    python scripts/creer_compte.py --supprimer <identifiant>
Docker :
    make compte NAME=<identifiant>

Le mot de passe est demandé sans écho (12 caractères minimum). Le secret TOTP est affiché UNE
SEULE FOIS (texte et URI otpauth://) : l'ajouter tout de suite dans une application
d'authentification (Aegis, 2FAS, Google Authenticator...). Il est ensuite chiffré dans le coffre
de l'instance et n'est plus jamais affiché. Dès qu'un compte existe, l'interface exige la
connexion (sauf BSM_AUTH_REQUIRED=false).
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.auth import AccountStore, validate_password, verify_totp  # noqa: E402
from binance_spot_manager.key_vault import VaultError  # noqa: E402


def ask_password() -> str:
    first = getpass.getpass("Mot de passe (12 caractères minimum) : ")
    validate_password(first)
    if getpass.getpass("Le retaper : ") != first:
        raise ValueError("Les deux saisies diffèrent")
    return first


def main(argv=None, *, store: AccountStore | None = None, password_prompt=ask_password,
         code_prompt=input, clock=None) -> int:
    parser = argparse.ArgumentParser(description="Compte de connexion à l'interface BinanceSpotManager")
    parser.add_argument("identifiant", nargs="?", help="2 à 32 caractères : lettres, chiffres, . _ -")
    parser.add_argument("--remplacer", action="store_true", help="Réinitialise mot de passe et TOTP (ferme les sessions)")
    parser.add_argument("--supprimer", metavar="IDENTIFIANT", help="Supprime ce compte")
    parser.add_argument("--liste", action="store_true", help="Liste les comptes")
    args = parser.parse_args(argv)
    store = store or AccountStore()
    try:
        if args.liste:
            print("\n".join(store.usernames()) or "Aucun compte")
            return 0
        if args.supprimer:
            print("Compte supprimé" if store.delete(args.supprimer) else "Compte inconnu")
            return 0
        if not args.identifiant:
            parser.error("identifiant manquant")
        password = password_prompt()
        account = store.create(args.identifiant, password, replace=args.remplacer)
    except (ValueError, VaultError, RuntimeError) as exc:
        print(f"Refusé : {exc}", file=sys.stderr)
        return 1
    print(f"Compte « {account.username} » enregistré.")
    print()
    print("Second facteur — à ajouter MAINTENANT dans l'application d'authentification,")
    print("il ne sera plus jamais affiché :")
    print(f"  secret : {account.totp_base32}")
    print(f"  URI    : {account.uri}")
    print()
    # Vérifie que l'application est bien configurée, sans consommer le code pour la connexion.
    now = (clock or time.time)()
    code = code_prompt("Code affiché par l'application (Entrée pour passer) : ").strip()
    if code:
        if verify_totp(account.totp_secret, code, now) is None:
            print("Code incorrect : vérifier l'heure du téléphone et le secret saisi.", file=sys.stderr)
            return 2
        print("Code correct : second facteur prêt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
