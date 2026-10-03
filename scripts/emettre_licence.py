"""Outil du PROPRIÉTAIRE : paire de clés de licence et émission de licences signées (Ed25519).

Ne s'exécute que chez le propriétaire. La clé PRIVÉE ne doit jamais être dans le dépôt, sur un
serveur client, ni dans une sauvegarde partagée : le script refuse un chemin dans le projet.

Usage :
    # une fois : crée la paire (clé privée chiffrée par phrase de passe, droits 0600)
    python scripts/emettre_licence.py generer --cle-privee ~/licences/bsm_licence.pem
    # pour chaque client : signe une licence
    python scripts/emettre_licence.py emettre --cle-privee ~/licences/bsm_licence.pem \\
        --client "Client SARL" --offre mensuelle --fin 2026-12-31 --sortie licence-client.json

La clé publique affichée par `generer` se donne au client (variable BSM_LICENCE_PUBLIC_KEY),
avec le fichier de licence (Settings → Sécurité → Licence, ou data/licence.json).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from binance_spot_manager.licence import public_key_b64, sign_licence  # noqa: E402


def _outside_project(path: Path) -> Path:
    path = Path(path).expanduser()
    if path.resolve().is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError("La clé privée ne doit jamais être dans le dossier du projet (risque de commit)")
    return path


def generate_keypair(private_path: Path, passphrase: bytes) -> str:
    private_path = _outside_project(private_path)
    if private_path.exists():
        raise ValueError(f"{private_path} existe déjà : jamais écrasée")
    if len(passphrase) < 12:
        raise ValueError("Phrase de passe : 12 caractères minimum")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase),
    )
    private_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    return public_key_b64(key)


def load_private_key(private_path: Path, passphrase: bytes) -> Ed25519PrivateKey:
    private_path = _outside_project(private_path)
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=passphrase or None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("La clé privée n'est pas une clé Ed25519")
    return key


def issue(private_path: Path, passphrase: bytes, *, client: str, offre: str, fin: str,
          licence_id: str | None = None) -> dict:
    key = load_private_key(private_path, passphrase)
    return sign_licence(key, licence_id=licence_id or uuid.uuid4().hex, client=client, offre=offre, fin=fin)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Licences de location BinanceSpotManager (propriétaire)")
    sub = parser.add_subparsers(dest="commande", required=True)
    gen = sub.add_parser("generer", help="Crée la paire de clés de licence")
    gen.add_argument("--cle-privee", required=True, type=Path)
    emit = sub.add_parser("emettre", help="Signe une licence pour un client")
    emit.add_argument("--cle-privee", required=True, type=Path)
    emit.add_argument("--client", required=True)
    emit.add_argument("--offre", required=True)
    emit.add_argument("--fin", required=True, help="Dernier jour inclus, AAAA-MM-JJ (UTC)")
    emit.add_argument("--sortie", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.commande == "generer":
            passphrase = getpass.getpass("Phrase de passe de la clé privée (12 caractères minimum) : ").encode()
            if getpass.getpass("La retaper : ").encode() != passphrase:
                raise ValueError("Les deux saisies diffèrent")
            public = generate_keypair(args.cle_privee, passphrase)
            print(f"Clé privée : {args.cle_privee} (droits 0600, à sauvegarder hors ligne)")
            print(f"Clé publique (BSM_LICENCE_PUBLIC_KEY des clients) : {public}")
            return 0
        passphrase = getpass.getpass("Phrase de passe de la clé privée : ").encode()
        document = issue(args.cle_privee, passphrase, client=args.client, offre=args.offre, fin=args.fin)
        if args.sortie.exists():
            raise ValueError(f"{args.sortie} existe déjà : jamais écrasé")
        args.sortie.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Licence {document['licence']['id']} écrite : {args.sortie} (fin {document['licence']['fin']})")
        return 0
    except (ValueError, TypeError, OSError) as exc:
        print(f"Refusé : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
