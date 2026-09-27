"""Journal d'evenements JSONL (section 57).

Un evenement par ligne, append-only, lisible par `tail` ou pandas.
Les ecritures sont effectuees en mode append pour ne jamais reecrire
le journal complet.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import ERROR_LOG_FILE, EVENTS_FILE, ensure_directories
from .models import EventType, utcnow

logger = logging.getLogger("bsm.events")


class EventStore:
    """Append-only JSONL. Tolerant aux erreurs d'ecriture (ne casse pas le bot)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        ensure_directories()
        self.path = Path(path) if path else EVENTS_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        event_type: EventType | str,
        message: str = "",
        *,
        position_id: str = "",
        symbol: str = "",
        level: str = "INFO",
        **data: Any,
    ) -> dict[str, Any]:
        record = {
            "ts": utcnow().isoformat(),
            "level": level,
            "event": event_type.value if isinstance(event_type, EventType) else str(event_type),
            "position_id": position_id,
            "symbol": symbol,
            "message": message,
            "data": data or {},
        }
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # Le journal ne doit jamais faire tomber le bot.
            logger.error("Ecriture evenement impossible : %s", exc)
        return record

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        """Derniers evenements, du plus ancien au plus recent."""
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError as exc:
            logger.error("Lecture journal impossible : %s", exc)
            return []

        records: list[dict[str, Any]] = []
        for line in lines[-max(limit, 1):]:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def for_position(self, position_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return [r for r in self.tail(limit=5000) if r.get("position_id") == position_id][-limit:]

    def errors(self, limit: int = 100) -> list[dict[str, Any]]:
        return [r for r in self.tail(limit=5000) if r.get("level") in {"ERROR", "CRITICAL"}][-limit:]

    def clear(self) -> None:
        """Vide le journal (outil de maintenance, jamais appele automatiquement)."""
        try:
            self.path.write_text("", encoding="utf-8")
        except OSError as exc:
            logger.error("Purge journal impossible : %s", exc)


def log_error(message: str, *, context: str = "", **data: Any) -> None:
    """Erreur applicative : journal JSONL + fichier texte lisible."""
    EventStore().append(EventType.ERROR, message, level="ERROR", context=context, **data)
    try:
        with ERROR_LOG_FILE.open("a", encoding="utf-8", newline="\n") as handle:
            stamp = datetime.now(timezone.utc).isoformat()
            handle.write(f"{stamp} | {context} | {message}\n")
    except OSError:
        pass


def configure_logging(level: int = logging.INFO) -> None:
    """Configuration logging console + fichier, appelee par le worker et Streamlit."""
    ensure_directories()
    root = logging.getLogger("bsm")
    if root.handlers:
        return
    root.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_file = ERROR_LOG_FILE.parent / "bot.log"
    try:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass
