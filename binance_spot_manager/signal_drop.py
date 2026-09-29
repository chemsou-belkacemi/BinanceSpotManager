"""Dépôt direct de signaux par fichiers JSON (générateur ML local, sans Telegram).

Contrat v1 (fixe, partagé avec le projet externe « MLSignalGenerator ») :

* répertoire ``DATA_DIR / "signal_drop"`` avec ``incoming/``, ``processed/`` et
  ``rejected/`` ; le producteur écrit uniquement dans ``incoming/`` ;
* un fichier par signal, écrit atomiquement (``*.tmp`` puis renommage en
  ``*.json``) ; les autres extensions sont ignorées, un fichier > 64 Ko est rejeté ;
* schéma : ``{"version": 1, "id": "ml-<hex>", "producer": "...",
  "created_at": <unix>, "text": "<signal structuré>", "meta": {...}}``.

Le fichier ne crée jamais d'ordre à lui seul : il est stocké dans SignalInbox
(source ``api``) exactement comme un message Telegram, puis passe par le même
parseur strict, la même fenêtre de fraîcheur et les mêmes contrôles du worker.
Le déplacement vers ``processed/`` a lieu APRÈS l'enregistrement : un arrêt
entre les deux provoque une réimportation dédupliquée, sans second signal.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time

from .config import DATA_DIR

logger = logging.getLogger("bsm.signal_drop")

DROP_DIR = DATA_DIR / "signal_drop"
MAX_FILE_BYTES = 64 * 1024
SCHEMA_VERSION = 1
ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
SUBDIRECTORIES = ("incoming", "processed", "rejected")


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


class SignalDropImporter:
    """Relève incoming/ à chaque cycle du worker ; n'écrit que dans SignalInbox."""

    def __init__(self, inbox, scope, preferences_loader, *, directory=DROP_DIR,
                 clock=time.time):
        self.inbox = inbox
        self.scope = scope
        self.preferences_loader = preferences_loader
        self.directory = Path(directory)
        self.clock = clock
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

    def _import_pending(self, limit):
        preferences = self.preferences_loader()
        preferences = preferences if isinstance(preferences, dict) else {}
        if not preferences.get("signal_drop_enabled", False):
            self._update(state="DISABLED", last_error="")
            return []
        ensure_drop_directories(self.directory)
        names = sorted(
            name for name in os.listdir(self.incoming)
            if name.endswith(".json") and (self.incoming / name).is_file()
        )[:max(1, min(int(limit), 200))]
        received = []
        last_error = ""
        for name in names:
            path = self.incoming / name
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise DropFileRejected(f"Fichier supérieur à {MAX_FILE_BYTES // 1024} Ko")
                document = parse_drop_document(path.read_bytes())
            except DropFileRejected as exc:
                self._reject(path, str(exc))
                last_error = f"{name} rejeté : {exc}"
                continue
            except OSError:
                # Fichier retiré ou verrouillé entre listdir et lecture : réessai au cycle suivant.
                continue
            row = self.inbox.receive(
                self.scope, document["text"], source="api",
                external_id=f"drop:{document['id']}",
                source_timestamp=document["created_at"],
            )
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
