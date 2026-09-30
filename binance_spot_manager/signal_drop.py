"""Dépôt direct de signaux par fichiers (générateur ML local ou CryptoSignalIntelligence).

Répertoire ``DATA_DIR / "signal_drop"`` avec ``incoming/``, ``processed/`` et
``rejected/`` ; le producteur écrit uniquement dans ``incoming/``, atomiquement
(fichier temporaire puis renommage). Deux contrats coexistent :

* **v1 (JSON)** : ``*.json`` = ``{"version": 1, "id": "ml-<hex>", "producer": "...",
  "created_at": <unix>, "text": "<signal structuré>", "meta": {...}}`` ; le texte
  passe par les modèles historiques du parseur ;
* **v2 (TXT)** : ``*.txt`` = contrat ``SIGNAL_VERSION=3`` de CryptoSignalIntelligence,
  sans enveloppe JSON (``*.txt.tmp`` en cours d'écriture : ignoré). Identifiant
  externe ``csi:<SIGNAL_ID>``, date de référence ``CREATED_AT``. Un fichier expiré
  à la réception (``EXPIRES_AT`` atteint), mal formé (dont ``SIGNAL_VERSION=2``,
  retirée), à politique de sortie non exécutée ou dont la clé d'idempotence /
  le SIGNAL_ID est déjà connu part dans ``rejected/`` avec ``<nom>.reason.txt``.

Les autres extensions sont ignorées, un fichier > 64 Ko est rejeté. Un fichier
ne crée jamais d'ordre à lui seul : il est stocké dans SignalInbox (source ``api``)
puis passe par la même fenêtre de validité et les mêmes contrôles du worker.
Le déplacement vers ``processed/`` a lieu APRÈS l'enregistrement : un arrêt entre
les deux provoque une réimportation dédupliquée, sans second signal. Le fichier
de retour d'exécution (``outgoing/execution_events.jsonl``) est tenu par
:mod:`signal_feedback`.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time

from .config import DATA_DIR
from .signal_inbox import DuplicateSignal
from .signal_parser import parse_csi_signal

logger = logging.getLogger("bsm.signal_drop")

DROP_DIR = DATA_DIR / "signal_drop"
MAX_FILE_BYTES = 64 * 1024
SCHEMA_VERSION = 1
ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
SUBDIRECTORIES = ("incoming", "processed", "rejected", "outgoing")
#: Préfixe de l'identifiant externe des signaux du contrat CSI.
V2_EXTERNAL_PREFIX = "csi:"


class DropFileRejected(ValueError):
    """Fichier invalide : il est déplacé dans rejected/ avec son motif."""


def ensure_drop_directories(directory=DROP_DIR) -> Path:
    """Crée l'arborescence du dépôt (propriétaire : l'utilisateur du worker)."""
    root = Path(directory)
    for name in SUBDIRECTORIES:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def parse_drop_document(data: bytes) -> dict:
    """Valide un fichier du contrat v1 ; lève DropFileRejected sinon."""
    if len(data) > MAX_FILE_BYTES:
        raise DropFileRejected(f"Fichier supérieur à {MAX_FILE_BYTES // 1024} Ko")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DropFileRejected("JSON invalide") from None
    if not isinstance(document, dict):
        raise DropFileRejected("Objet JSON attendu")
    version = document.get("version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise DropFileRejected("Version de schéma non prise en charge (1 attendu)")
    signal_id = document.get("id")
    if not isinstance(signal_id, str) or not ID_PATTERN.fullmatch(signal_id):
        raise DropFileRejected("Identifiant absent ou invalide")
    created_at = document.get("created_at")
    if (isinstance(created_at, bool) or not isinstance(created_at, (int, float))
            or not math.isfinite(created_at) or created_at <= 0):
        raise DropFileRejected("created_at doit être un horodatage Unix fini")
    text = document.get("text")
    if not isinstance(text, str) or not text.strip():
        raise DropFileRejected("Texte du signal absent")
    return {"id": signal_id, "created_at": float(created_at), "text": text}


def _lenient_v2_value(text: str, key: str) -> str:
    """Valeur brute d'une clé CSI dans un texte refusé (retour d'exécution uniquement)."""
    match = re.search(rf"^{key}=(\S+)\s*$", text, re.M)
    return match.group(1) if match else ""


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SignalDropImporter:
    """Relève incoming/ à chaque cycle du worker ; n'écrit que dans SignalInbox."""

    def __init__(self, inbox, scope, preferences_loader, *, directory=DROP_DIR,
                 clock=time.time, feedback=None):
        self.inbox = inbox
        self.scope = scope
        self.preferences_loader = preferences_loader
        self.directory = Path(directory)
        self.clock = clock
        #: Retour d'exécution (signal_feedback.SignalFeedbackWriter) ; facultatif.
        self.feedback = feedback
        self._lock = threading.Lock()
        self._diagnostics = {
            "state": "DISABLED",
            "last_import_at": None,
            "imported_total": 0,
            "rejected_total": 0,
            "last_error": "",
        }
        # Créé dès le démarrage du worker (uid bsm) : le producteur peut déposer
        # avant l'activation, les fichiers attendent simplement dans incoming/.
        try:
            ensure_drop_directories(self.directory)
        except OSError:
            logger.warning("Dépôt de signaux : création de %s impossible", self.directory)

    @property
    def incoming(self) -> Path:
        return self.directory / "incoming"

    def _update(self, **values):
        with self._lock:
            self._diagnostics.update(values)

    def snapshot(self):
        with self._lock:
            return dict(self._diagnostics)

    def import_pending(self, limit=20):
        """Importe au plus `limit` fichiers ; ne lève jamais (le worker continue)."""
        try:
            return self._import_pending(limit)
        except Exception:  # noqa: BLE001 - un dépôt défaillant ne bloque pas les TP/SL
            logger.exception("Dépôt de signaux : erreur inattendue")
            self._update(state="ERROR", last_error="Erreur inattendue du dépôt de signaux.")
            return []

    @staticmethod
    def _accepts(name: str) -> bool:
        # ``*.txt.tmp`` et ``*.tmp`` sont des écritures en cours : jamais lus.
        return name.endswith(".json") or name.endswith(".txt")

    def _import_pending(self, limit):
        preferences = self.preferences_loader()
        preferences = preferences if isinstance(preferences, dict) else {}
        if not preferences.get("signal_drop_enabled", False):
            self._update(state="DISABLED", last_error="")
            return []
        ensure_drop_directories(self.directory)
        names = sorted(
            name for name in os.listdir(self.incoming)
            if self._accepts(name) and (self.incoming / name).is_file()
        )[:max(1, min(int(limit), 200))]
        received = []
        last_error = ""
        for name in names:
            path = self.incoming / name
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise DropFileRejected(f"Fichier supérieur à {MAX_FILE_BYTES // 1024} Ko")
                data = path.read_bytes()
            except DropFileRejected as exc:
                self._reject(path, str(exc))
                last_error = f"{name} rejeté : {exc}"
                continue
            except OSError:
                # Fichier retiré ou verrouillé entre listdir et lecture : réessai au cycle suivant.
                continue
            try:
                if name.endswith(".txt"):
                    row = self._receive_csi(data)
                else:
                    document = parse_drop_document(data)
                    row = self.inbox.receive(
                        self.scope, document["text"], source="api",
                        external_id=f"drop:{document['id']}",
                        source_timestamp=document["created_at"],
                    )
            except DropFileRejected as exc:
                self._reject(path, str(exc))
                last_error = f"{name} rejeté : {exc}"
                continue
            received.append(row)
            # Déplacement APRÈS l'enregistrement : une reprise reste dédupliquée.
            self._move(path, self.directory / "processed" / name)
        update = {"state": "ACTIVE", "last_error": last_error}
        if received:
            update["last_import_at"] = self.clock()
            update["imported_total"] = self.snapshot()["imported_total"] + len(received)
        self._update(**update)
        if received:
            logger.info("%s signal(aux) déposé(s) importé(s)", len(received))
        return received

    def _receive_csi(self, data: bytes):
        """Contrat TXT V3 : contrôle strict, expiration à la réception, idempotence."""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise DropFileRejected("UTF-8 invalide") from None
        parsed = parse_csi_signal(text)
        if parsed.errors:
            reason = "Contrat CSI refusé : " + " ; ".join(parsed.errors)
            self._feedback_rejection(text, parsed, reason)
            raise DropFileRejected(reason)
        now = self.clock()
        if now >= parsed.expires_at:
            reason = (f"Signal expiré à la réception (EXPIRES_AT {_iso_utc(parsed.expires_at)}"
                      f" <= {_iso_utc(now)})")
            self._feedback_rejection(text, parsed, reason)
            raise DropFileRejected(reason)
        created_at = datetime.fromisoformat(parsed.published_at).timestamp()
        try:
            row = self.inbox.receive(
                self.scope, text, source="api",
                external_id=f"{V2_EXTERNAL_PREFIX}{parsed.signal_id}",
                source_timestamp=created_at,
                idempotency_key=parsed.idempotency_key,
                producer_signal_id=parsed.signal_id,
            )
        except DuplicateSignal as exc:
            reason = f"Doublon : {exc}"
            if not exc.same_signal:
                # Un nouvel identifiant réutilisant une clé connue n'est jamais accepté.
                self._feedback_rejection(text, parsed, reason)
            raise DropFileRejected(reason) from None
        if self.feedback is not None:
            self.feedback.register(parsed.signal_id, row["id"], parsed.symbol)
        return row

    def _feedback_rejection(self, text: str, parsed, reason: str) -> None:
        if self.feedback is None:
            return
        signal_id = parsed.signal_id or _lenient_v2_value(text, "SIGNAL_ID")
        symbol = parsed.symbol or _lenient_v2_value(text, "SYMBOL")
        self.feedback.record_rejection(signal_id, symbol, reason, occurred_at=self.clock())

    def _reject(self, path: Path, reason: str) -> None:
        target = self._move(path, self.directory / "rejected" / path.name)
        try:
            target.with_name(target.name + ".reason.txt").write_text(reason + "\n", encoding="utf-8")
        except OSError:
            pass
        self._update(rejected_total=self.snapshot()["rejected_total"] + 1)
        logger.warning("Signal déposé rejeté (%s) : %s", path.name, reason)

    @staticmethod
    def _move(source: Path, target: Path) -> Path:
        # Un nom déjà présent (fichier rejoué) est suffixé : aucun écrasement.
        if target.exists():
            target = target.with_name(f"{target.stem}.{time.time_ns()}{target.suffix}")
        os.replace(source, target)
        return target
