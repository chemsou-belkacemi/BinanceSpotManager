"""Exports controles, sans secrets, et verification avant toute restauration."""

from contextlib import closing
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import zipfile

from .file_mutex import FileMutex
from .models import Position, utcnow


MAX_ARCHIVE_BYTES = 100_000_000


def allowed_member(name):
    path = PurePosixPath(name)
    if "\\" in name or ":" in name or ".." in path.parts or path.is_absolute():
        return False
    return (name in {"data/settings.json", "data/presets.json", "data/commands.sqlite3", "data/order_intents.sqlite3"}
            or len(path.parts) == 3 and path.parts[:2] == ("data", "positions") and path.suffix == ".json")


def build_backup(data_dir: Path, *, worker_running: bool) -> bytes:
    if worker_running:
        raise ValueError("Arrete proprement le worker avant de creer une sauvegarde coherente")
    data_dir = Path(data_dir)
    members = {}
    # Empeche aussi un redemarrage du worker pendant la capture.
    with FileMutex(data_dir / "bot_worker.lock.lease"), FileMutex(data_dir / "positions" / ".write.lock"):
        files = list((data_dir / "positions").glob("*.json"))
        files += [data_dir / name for name in ("settings.json", "presets.json", "commands.sqlite3", "order_intents.sqlite3")]
        for path in files:
            if not path.exists():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(data_dir.resolve()):
                raise ValueError("Lien de fichier refuse dans la sauvegarde")
            name = "data/" + path.relative_to(data_dir).as_posix()
            if path.suffix == ".sqlite3":
                with tempfile.TemporaryDirectory(prefix="bsm-backup-") as tmp:
                    target = Path(tmp) / "snapshot.db"
                    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(target)) as dest:
                        source.backup(dest)
                        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError("Base SQLite endommagee")
                    members[name] = target.read_bytes()
            else:
                raw = path.read_bytes()
                if path.parent.name == "positions":
                    position = Position.model_validate_json(raw)
                    if position.position_id != path.stem:
                        raise ValueError("Identifiant de position incoherent")
                else:
                    json.loads(raw)
                members[name] = raw
            if sum(map(len, members.values())) > MAX_ARCHIVE_BYTES:
                raise ValueError("Sauvegarde trop volumineuse pour l'export en memoire")
    manifest = {"schema": 1, "environment": "DEMO", "created_at": utcnow().isoformat(),
                "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in members.items()}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, raw in members.items():
            archive.writestr(name, raw)
    return output.getvalue()


def verify_backup(raw: bytes) -> dict:
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError("Archive trop volumineuse")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or len(names) > 10000 or sum(e.file_size for e in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive dupliquee ou trop volumineuse")
        if "manifest.json" not in names or any(not allowed_member(n) for n in names if n != "manifest.json"):
            raise ValueError("Contenu non autorise dans l'archive")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != 1 or manifest.get("environment") != "DEMO":
            raise ValueError("Format ou environnement de sauvegarde incompatible")
        if set(manifest["files"]) != set(names) - {"manifest.json"}:
            raise ValueError("Inventaire incomplet")
        for name, digest in manifest["files"].items():
            content = archive.read(name)
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("Empreinte invalide : " + name)
            if name.startswith("data/positions/"):
                p = Position.model_validate_json(content)
                if p.position_id != PurePosixPath(name).stem:
                    raise ValueError("Identifiant incoherent")
            elif name.endswith(".json"):
                json.loads(content)
            elif name.endswith(".sqlite3"):
                with tempfile.TemporaryDirectory(prefix="bsm-verify-") as tmp:
                    path = Path(tmp) / "check.db"
                    path.write_bytes(content)
                    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError("Base SQLite invalide")
        return manifest
