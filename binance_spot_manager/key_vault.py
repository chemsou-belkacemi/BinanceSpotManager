"""Coffre des secrets de l'instance : clés API Binance, secrets TOTP.

Principe (location, une instance par client) :
    le client saisit SES clés dans SA propre instance ; elles sont chiffrées sur place et ne
    transitent jamais par le propriétaire du logiciel.

Chiffrement :
    AES-256-GCM (bibliothèque `cryptography`), nonce aléatoire de 96 bits par écriture, données
    associées (AAD) = nom de l'entrée + métadonnées : une entrée ne peut être ni déplacée sous un
    autre nom, ni modifiée (indice, date) sans être refusée au déchiffrement.

Clé maîtresse :
    32 octets aléatoires dans un fichier SÉPARÉ des données (`BSM_MASTER_KEY_FILE`, par défaut
    `~/.config/binance-spot-manager/master.key`), créé au premier usage avec les droits 0600.
    Refusée si elle se trouve dans le dépôt ou dans `data/` (jamais dans Git ni dans une
    sauvegarde), ou si elle est lisible par d'autres comptes (POSIX).

Le coffre lui-même (`data/key_vault.json`) ne contient que des données chiffrées, l'empreinte
de la clé maîtresse (pour distinguer « mauvaise clé » de « fichier altéré ») et, pour les clés
Binance, les 4 derniers caractères de la clé API (jamais du secret).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import DATA_DIR, PROJECT_ROOT
from .file_mutex import FileMutex
from .models import utcnow
from .position_store import atomic_write_text

logger = logging.getLogger("bsm.vault")

MASTER_KEY_ENV = "BSM_MASTER_KEY_FILE"
DEFAULT_MASTER_KEY_FILE = Path.home() / ".config" / "binance-spot-manager" / "master.key"
VAULT_FILE = DATA_DIR / "key_vault.json"

MASTER_KEY_PREFIX = "bsm-master-key-v1:"
SCHEMA = 1
BINANCE_ENTRY = "binance"
#: Clés HMAC Binance : 64 caractères alphanumériques. Bornes larges, contenu strict.
KEY_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


class VaultError(RuntimeError):
    """Coffre inutilisable : message sans aucun secret."""


class WrongMasterKey(VaultError):
    """Le coffre a été chiffré avec une autre clé maîtresse."""


class TamperedVault(VaultError):
    """Une entrée a été modifiée ou est illisible."""


@dataclass(frozen=True)
class SecretStatus:
    """Ce que l'interface a le droit de montrer : jamais le secret."""

    defined: bool
    hint: str = ""
    updated_at: str = ""


def master_key_path() -> Path:
    raw = (os.getenv(MASTER_KEY_ENV) or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_MASTER_KEY_FILE


def _forbidden_location(path: Path, data_dir: Path) -> Optional[str]:
    resolved = path.resolve()
    if resolved.is_relative_to(Path(data_dir).resolve()):
        return "dans le dossier des données (il serait copié dans les sauvegardes)"
    if resolved.is_relative_to(PROJECT_ROOT.resolve()):
        return "dans le dossier du projet (risque de commit ou de copie dans l'image)"
    return None


def _encode_master_key(key: bytes) -> str:
    return MASTER_KEY_PREFIX + base64.b64encode(key).decode("ascii") + "\n"


def _decode_master_key(text: str) -> bytes:
    text = text.strip()
    if not text.startswith(MASTER_KEY_PREFIX):
        raise VaultError("Fichier de clé maîtresse au format inconnu")
    try:
        key = base64.b64decode(text[len(MASTER_KEY_PREFIX):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VaultError("Fichier de clé maîtresse illisible") from exc
    if len(key) != 32:
        raise VaultError("Clé maîtresse de taille invalide (32 octets attendus)")
    return key


def _check_permissions(path: Path) -> None:
    if os.name == "nt":
        return  # droits POSIX sans objet : protéger le dossier par les ACL Windows
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise VaultError(
            f"Clé maîtresse accessible à d'autres comptes (droits {stat.S_IMODE(mode):o}) : "
            f"chmod 600 {path}"
        )


def load_master_key(path: Optional[Path] = None, *, create: bool = False,
                    data_dir: Path = DATA_DIR) -> bytes:
    """Lit la clé maîtresse ; la crée (0600, écriture exclusive) si `create` et absente."""
    path = Path(path) if path is not None else master_key_path()
    problem = _forbidden_location(path, data_dir)
    if problem:
        raise VaultError(f"Clé maîtresse refusée : {problem}. Choisir un autre {MASTER_KEY_ENV}.")
    if not path.exists():
        if not create:
            raise VaultError(f"Clé maîtresse absente ({path}) : aucune clé enregistrée ne peut être lue")
        _create_master_key(path)
    if path.is_symlink() or not path.is_file():
        raise VaultError("La clé maîtresse doit être un fichier ordinaire")
    _check_permissions(path)
    return _decode_master_key(path.read_text(encoding="ascii"))


def _create_master_key(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return  # créée au même instant par un autre processus : la relire
    except OSError as exc:
        raise VaultError(
            f"Impossible de créer la clé maîtresse ({path}) : {exc.strerror or type(exc).__name__}. "
            "Sous Docker : make master-key"
        ) from exc
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        os.write(fd, _encode_master_key(secrets.token_bytes(32)).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    logger.warning("Clé maîtresse créée : %s (à conserver hors des sauvegardes de données)", path)


def _key_id(key: bytes) -> str:
    return hashlib.sha256(b"bsm-vault-key-id:" + key).hexdigest()[:16]


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def _aad(name: str, hint: str, updated_at: str) -> bytes:
    return json.dumps(["bsm-vault-v1", name, hint, updated_at], separators=(",", ":")).encode()


def validate_api_credential(value: str, label: str) -> str:
    value = (value or "").strip()
    if not 16 <= len(value) <= 128 or not set(value) <= KEY_ALPHABET:
        raise ValueError(
            f"{label} invalide : 16 à 128 caractères alphanumériques attendus "
            "(clé HMAC Binance, sans espace)"
        )
    return value


class KeyVault:
    """Coffre chiffré d'une instance. Thread et multi-processus sûrs (verrou de fichier)."""

    def __init__(self, path: Optional[Path] = None, master_key_file: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else VAULT_FILE
        self._master_key_file = Path(master_key_file) if master_key_file is not None else None

    def __repr__(self) -> str:  # jamais de clé ni de secret
        return f"KeyVault(path={self.path.name!r})"

    # -- stockage --------------------------------------------------------

    @property
    def master_key_file(self) -> Path:
        return self._master_key_file or master_key_path()

    def exists(self) -> bool:
        return self.path.exists()

    def _master_key(self, *, create: bool) -> bytes:
        return load_master_key(self.master_key_file, create=create, data_dir=self.path.parent)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema": SCHEMA, "key_id": "", "entries": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TamperedVault("Coffre illisible : fichier altéré ou tronqué") from exc
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("entries"), dict):
            raise TamperedVault("Coffre au format inconnu")
        return raw

    def _write(self, document: dict) -> None:
        # mkstemp crée le fichier temporaire en 0600 ; remplacement atomique après fsync.
        atomic_write_text(self.path, json.dumps(document, indent=2, sort_keys=True))
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def _lock(self) -> FileMutex:
        return FileMutex(self.path.with_name(self.path.name + ".lock"))

    # -- API générique ---------------------------------------------------

    def put(self, name: str, plaintext: bytes, *, hint: str = "") -> None:
        key = self._master_key(create=True)
        with self._lock():
            document = self._read()
            if document["entries"] and document.get("key_id") not in ("", _key_id(key)):
                raise WrongMasterKey(
                    "Le coffre a été chiffré avec une autre clé maîtresse : "
                    "restaurer la bonne clé, ou supprimer le coffre et ressaisir les clés"
                )
            updated_at = utcnow().isoformat()
            nonce = secrets.token_bytes(12)
            ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(name, hint, updated_at))
            document["key_id"] = _key_id(key)
            document["entries"][name] = {
                "nonce": _b64(nonce), "ciphertext": _b64(ciphertext),
                "hint": hint, "updated_at": updated_at,
            }
            self._write(document)

    def get(self, name: str) -> Optional[bytes]:
        document = self._read()
        entry = document["entries"].get(name)
        if entry is None:
            return None
        key = self._master_key(create=False)
        if document.get("key_id") != _key_id(key):
            raise WrongMasterKey(
                "Clé maîtresse différente de celle qui a chiffré le coffre : clés illisibles"
            )
        try:
            nonce, ciphertext = _unb64(entry["nonce"]), _unb64(entry["ciphertext"])
            aad = _aad(name, str(entry.get("hint", "")), str(entry.get("updated_at", "")))
            return AESGCM(key).decrypt(nonce, ciphertext, aad)
        except (InvalidTag, KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise TamperedVault(f"Entrée « {name} » du coffre altérée : refusée") from exc

    def delete(self, name: str) -> bool:
        if not self.path.exists():
            return False
        with self._lock():
            document = self._read()
            removed = document["entries"].pop(name, None) is not None
            if removed:
                self._write(document)
            return removed

    def status(self, name: str) -> SecretStatus:
        """Métadonnées seulement : ne lit pas la clé maîtresse, ne déchiffre rien."""
        try:
            entry = self._read()["entries"].get(name)
        except VaultError:
            return SecretStatus(defined=False, hint="coffre illisible")
        if entry is None:
            return SecretStatus(defined=False)
        return SecretStatus(True, str(entry.get("hint", "")), str(entry.get("updated_at", "")))

    # -- clés Binance ----------------------------------------------------

    def save_binance_keys(self, api_key: str, api_secret: str) -> SecretStatus:
        api_key = validate_api_credential(api_key, "Clé API")
        api_secret = validate_api_credential(api_secret, "Secret API")
        if api_key == api_secret:
            raise ValueError("La clé API et le secret doivent être différents")
        payload = json.dumps({"api_key": api_key, "api_secret": api_secret}).encode()
        self.put(BINANCE_ENTRY, payload, hint="…" + api_key[-4:])
        return self.status(BINANCE_ENTRY)

    def load_binance_keys(self) -> Optional[tuple[str, str]]:
        """Déchiffre les clés. Réservé au code qui signe les requêtes Binance."""
        raw = self.get(BINANCE_ENTRY)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return str(data["api_key"]), str(data["api_secret"])
        except (ValueError, KeyError, TypeError) as exc:
            raise TamperedVault("Clés Binance du coffre illisibles") from exc

    def delete_binance_keys(self) -> bool:
        return self.delete(BINANCE_ENTRY)

    def binance_status(self) -> SecretStatus:
        return self.status(BINANCE_ENTRY)
