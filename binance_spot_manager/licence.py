"""Licence de location : fichier JSON signé Ed25519, vérifié hors ligne.

Format (`data/licence.json`, ou `BSM_LICENCE_FILE`) :
    {"licence": {"id": "...", "client": "...", "offre": "...", "fin": "AAAA-MM-JJ",
                 "emise_le": "..."},
     "signature": "<base64 de la signature Ed25519>"}
La signature porte sur `bsm-licence-v1\\n` + le JSON canonique de "licence" (clés triées, sans
espaces, UTF-8). La licence est valable jusqu'à la fin du jour "fin" (UTC) inclus.

Clé publique du propriétaire : `BSM_LICENCE_PUBLIC_KEY` (32 octets bruts, en base64). La clé
PRIVÉE ne vit que chez le propriétaire (scripts/emettre_licence.py), jamais dans le dépôt.

`BSM_LICENCE_REQUIRED=true` : sans licence valide, AUCUNE NOUVELLE ENTRÉE (exécution automatique
des signaux et nouvelles positions refusées), mais les positions ouvertes restent gérées
(stops, TP, clôtures) : une position n'est jamais abandonnée. Non défini : pas d'exigence
(installation du propriétaire).

Limite assumée : sur une machine que le client contrôle (modèle auto-hébergé), il peut modifier
le code ou la configuration. La licence matérialise le contrat ; ce n'est pas une protection
technique contre un client de mauvaise foi (voir docs/LOCATION.md).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .config import DATA_DIR

LICENCE_FILE = DATA_DIR / "licence.json"
DOMAIN = b"bsm-licence-v1\n"
REQUIRED_FIELDS = ("id", "client", "offre", "fin")
MAX_LICENCE_BYTES = 16_384
EXPIRY_WARNING_DAYS = 7


@dataclass(frozen=True)
class LicenceStatus:
    valid: bool
    reason: str = ""
    client: str = ""
    offre: str = ""
    fin: str = ""
    days_left: Optional[int] = None

    @property
    def expiring_soon(self) -> bool:
        return self.valid and self.days_left is not None and self.days_left <= EXPIRY_WARNING_DAYS


def canonical_bytes(fields: dict) -> bytes:
    return DOMAIN + json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def public_key_from_b64(text: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode((text or "").strip(), validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Clé publique de licence invalide (32 octets Ed25519 en base64 attendus)") from exc


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _end_of(fin: str) -> datetime:
    day = date.fromisoformat(fin)
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)


def sign_licence(private_key: Ed25519PrivateKey, *, licence_id: str, client: str, offre: str,
                 fin: str, emise_le: Optional[str] = None) -> dict:
    fields = {"id": licence_id.strip(), "client": client.strip(), "offre": offre.strip(),
              "fin": date.fromisoformat(fin).isoformat(),
              "emise_le": emise_le or datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    if not all(fields[name] for name in REQUIRED_FIELDS):
        raise ValueError("Licence incomplète : id, client, offre et fin sont obligatoires")
    signature = private_key.sign(canonical_bytes(fields))
    return {"licence": fields, "signature": base64.b64encode(signature).decode("ascii")}


def verify_licence(document: Any, public_key_text: str, *, now: Optional[float] = None) -> LicenceStatus:
    """Vérifie signature et échéance. Ne lève pas : toute anomalie donne une licence invalide."""
    if not (public_key_text or "").strip():
        return LicenceStatus(False, "Clé publique de licence absente (BSM_LICENCE_PUBLIC_KEY)")
    try:
        public_key = public_key_from_b64(public_key_text)
    except ValueError as exc:
        return LicenceStatus(False, str(exc))
    if not isinstance(document, dict) or not isinstance(document.get("licence"), dict):
        return LicenceStatus(False, "Fichier de licence au format inconnu")
    fields = document["licence"]
    if not all(isinstance(fields.get(name), str) and fields.get(name) for name in REQUIRED_FIELDS):
        return LicenceStatus(False, "Licence incomplète")
    try:
        signature = base64.b64decode(str(document.get("signature", "")), validate=True)
        public_key.verify(signature, canonical_bytes(fields))
    except (binascii.Error, ValueError, InvalidSignature):
        return LicenceStatus(False, "Signature de licence invalide (fichier modifié ou autre émetteur)")
    try:
        end = _end_of(fields["fin"])
    except ValueError:
        return LicenceStatus(False, "Date de fin de licence illisible")
    now = time.time() if now is None else now
    remaining = end.timestamp() - now
    common = dict(client=fields["client"], offre=fields["offre"], fin=fields["fin"])
    if remaining <= 0:
        return LicenceStatus(False, f"Licence expirée depuis le {fields['fin']}", **common)
    return LicenceStatus(True, "", days_left=int(remaining // 86400), **common)


def licence_required() -> bool:
    return (os.getenv("BSM_LICENCE_REQUIRED") or "").strip().lower() in {"1", "true", "yes", "oui", "vrai"}


def licence_path() -> Path:
    raw = (os.getenv("BSM_LICENCE_FILE") or "").strip()
    return Path(raw).expanduser() if raw else LICENCE_FILE


def read_licence_file(path: Path) -> Any:
    path = Path(path)
    if not path.exists():
        return None
    raw = path.read_bytes()
    if len(raw) > MAX_LICENCE_BYTES:
        raise ValueError("Fichier de licence trop volumineux")
    return json.loads(raw.decode("utf-8"))


def current_status(*, path: Optional[Path] = None, public_key_text: Optional[str] = None,
                   now: Optional[float] = None) -> LicenceStatus:
    path = licence_path() if path is None else Path(path)
    key = os.getenv("BSM_LICENCE_PUBLIC_KEY", "") if public_key_text is None else public_key_text
    try:
        document = read_licence_file(path)
    except (OSError, ValueError):
        return LicenceStatus(False, "Fichier de licence illisible")
    if document is None:
        return LicenceStatus(False, f"Aucune licence installée ({path.name})")
    return verify_licence(document, key, now=now)


def install_licence(raw: bytes, *, path: Optional[Path] = None,
                    public_key_text: Optional[str] = None, now: Optional[float] = None) -> LicenceStatus:
    """Installe un fichier de licence reçu, seulement s'il est valide maintenant."""
    from .position_store import atomic_write_text

    if len(raw) > MAX_LICENCE_BYTES:
        return LicenceStatus(False, "Fichier de licence trop volumineux")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return LicenceStatus(False, "Fichier de licence illisible")
    key = os.getenv("BSM_LICENCE_PUBLIC_KEY", "") if public_key_text is None else public_key_text
    status = verify_licence(document, key, now=now)
    if status.valid:
        atomic_write_text(licence_path() if path is None else Path(path),
                          json.dumps(document, ensure_ascii=False, indent=2))
    return status


class LicenceGate:
    """Autorisation des NOUVELLES entrées. Les sorties ne passent jamais par ici.

    `refusal()` renvoie "" (autorisé) ou la raison du refus. Résultat gardé `ttl` secondes : le
    worker l'interroge à chaque tour sans relire le fichier à chaque fois.
    """

    def __init__(self, *, required: Optional[bool] = None,
                 status_loader: Optional[Callable[[], LicenceStatus]] = None,
                 clock: Callable[[], float] = time.monotonic, ttl: float = 60.0) -> None:
        self.required = licence_required() if required is None else required
        self.status_loader = status_loader or current_status
        self.clock = clock
        self.ttl = ttl
        self._cached: Optional[tuple[float, LicenceStatus]] = None

    def status(self) -> LicenceStatus:
        now = self.clock()
        if self._cached is None or now - self._cached[0] >= self.ttl:
            self._cached = (now, self.status_loader())
        return self._cached[1]

    def refusal(self) -> str:
        if not self.required:
            return ""
        status = self.status()
        if status.valid:
            return ""
        return (f"Licence requise : {status.reason}. Aucune nouvelle entrée ; "
                "les positions ouvertes restent suivies (stops, objectifs, clôtures).")
