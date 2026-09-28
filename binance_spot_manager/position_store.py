"""Persistance locale des positions — ecritures atomiques, JSON UTF-8.

Arborescence :
    data/positions/<position_id>.json
    data/bot_runtime.json
    data/settings.json
    data/presets.json

Palier de migration prevu vers SQLite/PostgreSQL : toute la logique d'acces
passe par cette classe, aucun appel direct au systeme de fichiers ailleurs.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import (
    BOT_RUNTIME_FILE,
    POSITIONS_DIR,
    PRESETS_FILE,
    SETTINGS_FILE,
    ensure_directories,
)
from .models import BotRuntime, Position, PositionStatus, utcnow
from .file_mutex import FileMutex

logger = logging.getLogger("bsm.store")


class ConcurrentPositionUpdate(RuntimeError):
    """Une version plus recente existe : recharger, ne jamais ecraser."""


def atomic_write_text(path: Path, content: str) -> None:
    """Ecrit via fichier temporaire + remplacement atomique.

    Ne laisse jamais un JSON tronque derriere lui, meme si le process meurt
    pendant l'ecriture (section 10 du cahier des charges).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str, allow_nan=False))


def read_json(path: Path) -> Optional[Any]:
    """Lecture tolerante : retourne None si le fichier est absent ou corrompu."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("JSON illisible %s : %s", path.name, exc)
        return None


class PositionStore:
    """CRUD des strategies identifiees ; plusieurs positions peuvent partager une paire."""

    def __init__(self, directory: Optional[Path] = None) -> None:
        ensure_directories()
        self.directory = Path(directory) if directory else POSITIONS_DIR
        self.read_errors: list[str] = []

    # -- chemins --------------------------------------------------------

    def path_for(self, position_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", position_id):
            raise ValueError("Identifiant de position invalide")
        return self.directory / f"{position_id}.json"

    def exists(self, position_id: str) -> bool:
        return self.path_for(position_id).exists()

    # -- ecriture -------------------------------------------------------

    def save(self, position: Position) -> Path:
        path = self.path_for(position.position_id)
        Position.model_validate(position.model_dump(mode="python"))
        with FileMutex(self.directory / ".write.lock"):
            old = read_json(path)
            if old is None and path.exists():
                raise RuntimeError(f"Fichier illisible conserve : {path.name}")
            if old is not None:
                previous = Position.model_validate(old)
                if previous.position_id != position.position_id:
                    raise RuntimeError(f"Identifiant incoherent dans {path.name}")
                if previous.revision != position.revision:
                    raise ConcurrentPositionUpdate(
                        f"{position.symbol} : position modifiee par un autre processus. "
                        "Recharger et verifier Binance avant de reprendre l'action."
                    )
                backup = self.directory / ".backups" / path.name
                if not backup.exists() or time.time() - backup.stat().st_mtime >= 60:
                    atomic_write_json(backup, old)
            if position.is_open and (old is None or not previous.is_open):
                existing = self.list_all()
                if self.read_errors:
                    raise RuntimeError("Creation refusee : positions locales illisibles")
            position.touch()
            payload = position.model_dump(mode="json")
            payload["revision"] = position.revision + 1
            atomic_write_json(path, payload)
            position.revision = payload["revision"]
        return path

    def delete(self, position_id: str) -> bool:
        path = self.path_for(position_id)
        with FileMutex(self.directory / ".write.lock"):
            if path.exists():
                path.unlink()
                return True
        return False

    # -- lecture --------------------------------------------------------

    def load(self, position_id: str) -> Optional[Position]:
        raw = read_json(self.path_for(position_id))
        if raw is None:
            return None
        try:
            position = Position.model_validate(raw)
            if position.position_id != position_id or not position.symbol:
                raise ValueError("Identifiant ou symbole incoherent")
            return position
        except Exception as exc:
            logger.error("Position illisible %s : %s", position_id, exc)
            return None

    def list_all(self) -> list[Position]:
        """Toutes les positions, plus recentes d'abord."""
        positions: list[Position] = []
        self.read_errors = []
        for path in sorted(self.directory.glob("*.json")):
            raw = read_json(path)
            if raw is None:
                self.read_errors.append(f"{path.name} : fichier vide, illisible ou JSON invalide")
                continue
            try:
                position = Position.model_validate(raw)
                if position.position_id != path.stem or not position.symbol:
                    raise ValueError("Identifiant ou symbole incoherent")
                positions.append(position)
            except Exception as exc:
                self.read_errors.append(f"{path.name} : modele de position invalide")
                logger.error("Position ignoree (%s) : %s", path.name, exc)
        positions.sort(key=lambda p: p.created_at, reverse=True)
        return positions

    def list_open(self) -> list[Position]:
        return [p for p in self.list_all() if p.status.is_open]

    def list_closed(self) -> list[Position]:
        return [p for p in self.list_all() if not p.status.is_open]

    # -- recherche par paire ; les mutations utilisent position_id ------

    def list_active_by_symbol(self, symbol: str) -> list[Position]:
        target = symbol.strip().upper()
        return [p for p in self.list_open() if p.symbol.upper() == target]

    def find_active_by_symbol(self, symbol: str) -> Optional[Position]:
        """Compatibilite pour une paire non ambigue ; sinon exiger un ID."""
        matches = self.list_active_by_symbol(symbol)
        if len(matches) > 1:
            raise ValueError(f"Plusieurs positions sur {symbol.upper()} : selectionner un position_id")
        return matches[0] if matches else None

    def active_symbols(self) -> set[str]:
        return {p.symbol.upper() for p in self.list_open()}

    def count_active(self) -> int:
        return len(self.list_open())

    # -- idempotence ----------------------------------------------------

    def find_entry_by_client_order_id(self, client_order_id: str) -> Optional[tuple[Position, str]]:
        """(position, entry_id) portant ce clientOrderId, ou None.

        Sert avant tout retry : on ne recree jamais un ordre sans verifier
        qu'il n'existe pas deja localement (section 80).
        """
        for position in self.list_all():
            for entry in position.entries:
                if entry.client_order_id == client_order_id:
                    return position, entry.entry_id
        return None

    def find_by_order_id(self, order_id: int) -> Optional[tuple[Position, str]]:
        for position in self.list_all():
            for entry in position.entries:
                if entry.order_id == order_id:
                    return position, entry.entry_id
            if position.stop_loss.order_id == order_id:
                return position, position.stop_loss.client_order_id or "SL"
            for tp in position.take_profits:
                if tp.order_id == order_id:
                    return position, tp.tp_id
        return None


class RuntimeStore:
    """Etat du worker : heartbeat, PID, compteur de boucle."""

    def __init__(self, path: Optional[Path] = None) -> None:
        ensure_directories()
        self.path = Path(path) if path else BOT_RUNTIME_FILE

    def load(self) -> BotRuntime:
        raw = read_json(self.path)
        if raw is None:
            return BotRuntime()
        try:
            return BotRuntime.model_validate(raw)
        except Exception as exc:
            logger.error("bot_runtime.json illisible : %s", exc)
            return BotRuntime()

    def save(self, runtime: BotRuntime) -> None:
        atomic_write_json(self.path, runtime.model_dump(mode="json"))

    def heartbeat(self, **fields: Any) -> BotRuntime:
        runtime = self.load()
        for key, value in fields.items():
            if hasattr(runtime, key):
                setattr(runtime, key, value)
        runtime.heartbeat_at = utcnow()
        runtime.loop_count += 1
        self.save(runtime)
        return runtime


class JsonFileStore:
    """Petit store cle/valeur JSON (settings applicatifs, presets)."""

    def __init__(self, path: Path, default: Any = None) -> None:
        ensure_directories()
        self.path = Path(path)
        self.default = default if default is not None else {}

    def load(self) -> Any:
        raw = read_json(self.path)
        if raw is None:
            return json.loads(json.dumps(self.default))
        return raw

    def save(self, payload: Any) -> None:
        atomic_write_json(self.path, payload)

    def update(self, values: dict[str, Any]) -> Any:
        current = self.load()
        if not isinstance(current, dict):
            current = {}
        current.update(values)
        self.save(current)
        return current


def get_settings_store() -> JsonFileStore:
    return JsonFileStore(SETTINGS_FILE, {})


def get_presets_store() -> JsonFileStore:
    return JsonFileStore(PRESETS_FILE, {})


def summarize(positions: Iterable[Position]) -> dict[str, Any]:
    """Agregat leger pour le Dashboard (section 51)."""
    positions = list(positions)
    open_positions = [p for p in positions if p.status.is_open]
    return {
        "total": len(positions),
        "open": len(open_positions),
        "closed": len(positions) - len(open_positions),
        "symbols": [p.symbol for p in open_positions],
        "capital_committed": round(sum(p.metrics.capital_committed for p in open_positions), 8),
        "capital_pending": round(sum(p.metrics.capital_pending for p in open_positions), 8),
        "realized_pnl": round(sum(p.pnl.realized for p in positions), 8),
        "unrealized_pnl": round(sum(p.pnl.unrealized for p in open_positions), 8),
        "error_count": sum(1 for p in positions if p.status is PositionStatus.ERROR),
        "desync_count": sum(1 for p in open_positions if p.sync_status.name != "SYNCED"),
    }
