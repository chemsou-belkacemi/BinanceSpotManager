"""Intentions d'ordres durables : un identifiant n'est jamais renvoye apres envoi.

La presence d'une intention ne prouve pas une execution. Apres un arret entre
l'enregistrement et le POST, seule une lecture Binance peut trancher.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone


class OrderJournal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def claim(self, namespace: str, symbol: str, client_id: str, payload: dict) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS order_intents (
                namespace TEXT NOT NULL, symbol TEXT NOT NULL, client_id TEXT NOT NULL,
                created_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(namespace, symbol, client_id))""")
            cursor = db.execute(
                "INSERT OR IGNORE INTO order_intents VALUES (?, ?, ?, ?, ?)",
                (namespace, symbol, client_id, datetime.now(timezone.utc).isoformat(),
                 json.dumps(payload, sort_keys=True)),
            )
            return cursor.rowcount == 1

    def created_at(self, namespace: str, symbol: str, client_id: str):
        """Date d'inscription d'une intention (datetime UTC), ou None si elle est inconnue."""
        if not self.path.exists():
            return None
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute(
                    "SELECT created_at FROM order_intents WHERE namespace=? AND symbol=? AND client_id=?",
                    (namespace, symbol, client_id),
                ).fetchone()
        except sqlite3.OperationalError:
            return None  # journal sans table : aucune intention inscrite
        return datetime.fromisoformat(row[0]) if row else None

    def recent(self, namespace, limit=100):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) | {"payload": json.loads(row["payload"])} for row in db.execute(
                "SELECT symbol, client_id, created_at, payload FROM order_intents WHERE namespace=? ORDER BY created_at DESC LIMIT ?",
                (namespace, min(max(limit, 1), 500)),
            )]
