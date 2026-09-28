"""Intentions d'ordres durables : un identifiant n'est jamais renvoye apres envoi.

La presence d'une intention ne prouve pas une execution. Apres un arret entre
l'enregistrement et le POST, seule une lecture Binance peut trancher.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone


class OrderJournal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def claim(self, namespace: str, symbol: str, client_id: str, payload: dict) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as db:
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
