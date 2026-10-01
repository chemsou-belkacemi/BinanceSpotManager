"""Connexion à l'interface : comptes locaux, mot de passe + code TOTP, blocage, sessions.

Bibliothèque standard uniquement :
- mot de passe haché par scrypt (`hashlib.scrypt`), sel aléatoire de 16 octets, paramètres
  N=2^17, r=8, p=1 (recommandation OWASP), enregistrés avec le hachage ;
- second facteur TOTP (RFC 6238, HMAC-SHA1, 6 chiffres, pas de 30 s), fenêtre de ±1 pas, un code
  déjà utilisé est refusé (rejeu) ; le secret TOTP est chiffré dans le coffre (`key_vault`) ;
- 5 échecs consécutifs bloquent le compte 15 minutes ;
- comparaisons à temps constant (`hmac.compare_digest`) ; un identifiant inconnu coûte le même
  calcul scrypt qu'un compte existant.

Les comptes sont dans `data/accounts.json` (droits 0600), sans aucun secret en clair.
La session Streamlit expire après une inactivité (`BSM_SESSION_IDLE_MINUTES`, 30 par défaut).

`BSM_AUTH_REQUIRED` : `true` / `false`. Non défini : connexion exigée dès qu'un compte existe ;
une installation sans compte (celle du propriétaire aujourd'hui) reste utilisable telle quelle.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, MutableMapping, Optional

from .config import DATA_DIR
from .file_mutex import FileMutex
from .key_vault import KeyVault
from .models import utcnow
from .position_store import atomic_write_text

ACCOUNTS_FILE = DATA_DIR / "accounts.json"
ISSUER = "BinanceSpotManager"

SCRYPT_LOG2_N = 17
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{2,32}")

MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1

DEFAULT_IDLE_MINUTES = 30
SESSION_KEY = "bsm_auth_session"

GENERIC_FAILURE = "Identifiant, mot de passe ou code incorrect"


# --------------------------------------------------------------------------
# Mots de passe
# --------------------------------------------------------------------------


def _scrypt(password: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    n = 1 << log2_n
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        maxmem=128 * r * n * p + 32 * 1024 * 1024, dklen=HASH_BYTES,
    )


def validate_password(password: str) -> str:
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"Mot de passe : {MIN_PASSWORD_LENGTH} caractères minimum")
    return password


def hash_password(password: str, *, log2_n: int = SCRYPT_LOG2_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    validate_password(password)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, log2_n, r, p)
    return "$".join(["scrypt", str(log2_n), str(r), str(p),
                     base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, log2_n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt" or not 14 <= int(log2_n) <= 20:
            return False
        expected = base64.b64decode(digest, validate=True)
        actual = _scrypt(str(password)[:MAX_PASSWORD_LENGTH], base64.b64decode(salt, validate=True),
                         int(log2_n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY_HASH: Optional[str] = None


def _dummy_hash() -> str:
    """Hachage de référence : un identifiant inconnu coûte autant qu'un compte réel."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(24))
    return _DUMMY_HASH


# --------------------------------------------------------------------------
# TOTP (RFC 6238, sur HOTP RFC 4226)
# --------------------------------------------------------------------------


def generate_totp_secret() -> bytes:
    return secrets.token_bytes(20)  # 160 bits, taille recommandée par la RFC 4226 pour SHA-1


def totp_secret_base32(secret: bytes) -> str:
    return base64.b32encode(secret).decode("ascii").rstrip("=")


def hotp(secret: bytes, counter: int, *, digits: int = TOTP_DIGITS, digest: str = "sha1") -> str:
    mac = hmac.new(secret, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def totp(secret: bytes, timestamp: float, *, digits: int = TOTP_DIGITS, step: int = TOTP_STEP,
         digest: str = "sha1") -> str:
    return hotp(secret, int(timestamp // step), digits=digits, digest=digest)


def verify_totp(secret: bytes, code: str, timestamp: float, *, window: int = TOTP_WINDOW,
                last_step: Optional[int] = None) -> Optional[int]:
    """Pas de temps accepté (pour bloquer le rejeu), ou None. Toutes les fenêtres sont
    comparées, à temps constant, même après une correspondance."""
    code = (code or "").strip().replace(" ", "")
    if not re.fullmatch(r"\d{%d}" % TOTP_DIGITS, code):
        return None
    current = int(timestamp // TOTP_STEP)
    matched = None
    for step in range(current - window, current + window + 1):
        if hmac.compare_digest(hotp(secret, step), code) and matched is None:
            matched = step
    if matched is None or (last_step is not None and matched <= last_step):
        return None
    return matched


def otpauth_uri(secret: bytes, username: str, issuer: str = ISSUER) -> str:
    label = urllib.parse.quote(f"{issuer}:{username}")
    query = urllib.parse.urlencode({
        "secret": totp_secret_base32(secret), "issuer": issuer,
        "algorithm": "SHA1", "digits": TOTP_DIGITS, "period": TOTP_STEP,
    })
    return f"otpauth://totp/{label}?{query}"


# --------------------------------------------------------------------------
# Comptes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    message: str = ""
    username: str = ""
    session_version: int = 0


@dataclass(frozen=True)
class NewAccount:
    """Retour de la création : le secret TOTP n'est montré qu'une fois, puis oublié."""

    username: str
    totp_secret: bytes

    def __repr__(self) -> str:
        return f"NewAccount(username={self.username!r})"

    @property
    def totp_base32(self) -> str:
        return totp_secret_base32(self.totp_secret)

    @property
    def uri(self) -> str:
        return otpauth_uri(self.totp_secret, self.username)


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Identifiant : 2 à 32 caractères parmi lettres, chiffres, . _ -")
    return username


class AccountStore:
    def __init__(self, path: Optional[Path] = None, vault: Optional[KeyVault] = None, *,
                 log2_n: int = SCRYPT_LOG2_N) -> None:
        self.path = Path(path) if path is not None else ACCOUNTS_FILE
        self.vault = vault if vault is not None else KeyVault()
        self.log2_n = log2_n

    def _lock(self) -> FileMutex:
        return FileMutex(self.path.with_name(self.path.name + ".lock"))

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema": 1, "accounts": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Échec sûr : un fichier de comptes illisible ne doit jamais ouvrir l'interface.
            raise RuntimeError("Fichier des comptes illisible : connexion impossible") from exc
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            raise RuntimeError("Fichier des comptes au format inconnu : connexion impossible")
        return data

    def _write(self, data: dict) -> None:
        atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True))
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def has_accounts(self) -> bool:
        if not self.path.exists():
            return False
        try:
            return bool(self._read()["accounts"])
        except RuntimeError:
            return True  # illisible : on exige la connexion (qui échouera), jamais l'inverse

    def usernames(self) -> list[str]:
        return sorted(self._read()["accounts"])

    def create(self, username: str, password: str, *, replace: bool = False) -> NewAccount:
        username = validate_username(username)
        password_hash = hash_password(password, log2_n=self.log2_n)
        secret = generate_totp_secret()
        with self._lock():
            data = self._read()
            previous = data["accounts"].get(username)
            if previous is not None and not replace:
                raise ValueError(f"Le compte « {username} » existe déjà (--remplacer pour le réinitialiser)")
            self.vault.put(f"totp:{username}", secret, hint="TOTP")
            data["accounts"][username] = {
                "password": password_hash,
                "created_at": (previous or {}).get("created_at") or utcnow().isoformat(),
                "updated_at": utcnow().isoformat(),
                "failures": 0,
                "locked_until": 0,
                "last_totp_step": None,
                # Toute réinitialisation ferme les sessions ouvertes avec l'ancien mot de passe.
                "session_version": int((previous or {}).get("session_version", 0)) + 1,
            }
            self._write(data)
        return NewAccount(username, secret)

    def delete(self, username: str) -> bool:
        with self._lock():
            data = self._read()
            removed = data["accounts"].pop(username, None) is not None
            if removed:
                self._write(data)
        if removed:
            self.vault.delete(f"totp:{username}")
        return removed

    def session_version(self, username: str) -> Optional[int]:
        try:
            account = self._read()["accounts"].get(username)
        except RuntimeError:
            return None
        return None if account is None else int(account.get("session_version", 0))

    def authenticate(self, username: str, password: str, code: str,
                     now: Optional[float] = None) -> AuthResult:
        now = time.time() if now is None else now
        username = (username or "").strip()
        with self._lock():
            data = self._read()
            account = data["accounts"].get(username)
            if account is None:
                verify_password(password, _dummy_hash())
                return AuthResult(False, GENERIC_FAILURE)
            locked_until = float(account.get("locked_until") or 0)
            if locked_until > now:
                minutes = max(1, int((locked_until - now + 59) // 60))
                return AuthResult(False, f"Compte bloqué après {MAX_FAILURES} échecs : réessayer dans {minutes} min")
            password_ok = verify_password(password, account["password"])
            step = None
            if password_ok:
                secret = self.vault.get(f"totp:{username}")
                if secret is None:
                    return AuthResult(False, "Second facteur absent du coffre : recréer le compte (scripts/creer_compte.py --remplacer)")
                step = verify_totp(secret, code, now, last_step=account.get("last_totp_step"))
            if not password_ok or step is None:
                failures = int(account.get("failures", 0)) + 1
                account["failures"] = failures
                if failures >= MAX_FAILURES:
                    account["failures"] = 0
                    account["locked_until"] = now + LOCK_SECONDS
                self._write(data)
                if failures >= MAX_FAILURES:
                    return AuthResult(False, f"Trop d'échecs : compte bloqué {LOCK_SECONDS // 60} minutes")
                return AuthResult(False, GENERIC_FAILURE)
            account.update(failures=0, locked_until=0, last_totp_step=step,
                           last_login_at=utcnow().isoformat())
            self._write(data)
            return AuthResult(True, "", username, int(account.get("session_version", 0)))


# --------------------------------------------------------------------------
# Politique et sessions (sur un dictionnaire : st.session_state en production)
# --------------------------------------------------------------------------


def auth_required(store: AccountStore) -> bool:
    raw = (os.getenv("BSM_AUTH_REQUIRED") or "").strip().lower()
    if raw in {"1", "true", "yes", "oui", "vrai"}:
        return True
    if raw in {"0", "false", "no", "non", "faux"}:
        return False
    return store.has_accounts()


def idle_timeout_seconds() -> int:
    raw = (os.getenv("BSM_SESSION_IDLE_MINUTES") or "").strip()
    try:
        minutes = int(raw) if raw else DEFAULT_IDLE_MINUTES
    except ValueError:
        minutes = DEFAULT_IDLE_MINUTES
    return max(5, min(minutes, 8 * 60)) * 60


def open_session(state: MutableMapping[str, Any], result: AuthResult, now: Optional[float] = None) -> None:
    if not result.ok:
        raise ValueError("Session refusée : authentification non réussie")
    now = time.time() if now is None else now
    state[SESSION_KEY] = {"username": result.username, "version": result.session_version,
                          "started_at": now, "last_seen": now}


def close_session(state: MutableMapping[str, Any]) -> None:
    state.pop(SESSION_KEY, None)


def current_user(state: MutableMapping[str, Any], store: AccountStore, *,
                 now: Optional[float] = None, idle_seconds: Optional[int] = None,
                 touch: bool = True) -> Optional[str]:
    """Utilisateur connecté, ou None (session absente, expirée, compte supprimé ou réinitialisé).

    `touch` : une action de l'utilisateur prolonge la session ; un rafraîchissement automatique
    (fragment toutes les secondes) ne doit pas la prolonger.
    """
    session = state.get(SESSION_KEY)
    if not isinstance(session, dict):
        return None
    now = time.time() if now is None else now
    idle_seconds = idle_timeout_seconds() if idle_seconds is None else idle_seconds
    if now - float(session.get("last_seen", 0)) > idle_seconds:
        close_session(state)
        return None
    username = str(session.get("username", ""))
    if store.session_version(username) != session.get("version"):
        close_session(state)
        return None
    if touch:
        session["last_seen"] = now
    return username
