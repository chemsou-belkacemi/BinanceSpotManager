"""File locale durable : une demande incertaine n'est jamais rejouee."""

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .config import DATA_DIR, ALLOWED_DEMO_BASE_URLS


ACTIONS = {"SUBMIT_POSITION", "SIMPLE_BUY", "CANCEL_ORDER", "MOVE_SL", "CLOSE_LOCAL", "CLOSE_MARKET", "CHECK_CONNECTION"}
FINAL_STATES = {"SUCCEEDED", "FAILED", "UNCERTAIN", "EXPIRED", "CANCELED"}


def account_scope(settings):
    if not settings.is_demo or settings.base_url not in ALLOWED_DEMO_BASE_URLS:
        raise ValueError("Commandes reservees a Binance Demo")
    return settings.base_url + ":" + settings.run_mode.value + ":" + hashlib.sha256(settings.api_key.encode()).hexdigest()


class CommandStore:
    def __init__(self, path: Path = DATA_DIR / "commands.sqlite3"):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, request_key TEXT NOT NULL,
                action TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL,
                updated_at REAL NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                UNIQUE(scope, request_key))""")
            db.commit()
            yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        return dict(row) | {"payload": json.loads(row["payload"]), "result": json.loads(row["result"])}

    def enqueue(self, scope, action, payload, *, request_key, ttl=120):
        if action not in ACTIONS or not request_key or not 1 <= ttl <= 300:
            raise ValueError("Commande invalide")
        raw = json.dumps(payload, sort_keys=True, allow_nan=False)
        if len(raw) > 500_000:
            raise ValueError("Commande trop volumineuse")
        now, command_id = time.time(), uuid.uuid4().hex
        with self.connect() as db, db:
            db.execute("INSERT OR IGNORE INTO commands VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, '{}')",
                       (command_id, scope, request_key, action, raw, now, now + ttl, now))
            row = db.execute("SELECT * FROM commands WHERE scope=? AND request_key=?", (scope, request_key)).fetchone()
            # Meme confirmation, meme demande. Un changement ne remplace jamais une commande.
            if row["action"] != action or row["payload"] != raw:
                raise ValueError("Confirmation deja utilisee pour une autre demande")
            return self.decode(row)

    def claim(self, scope):
        now = time.time()
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE commands SET state='EXPIRED', updated_at=? WHERE scope=? AND state='PENDING' AND expires_at<=?", (now, scope, now))
            row = db.execute("SELECT * FROM commands WHERE scope=? AND state='PENDING' ORDER BY created_at, id LIMIT 1", (scope,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE commands SET state='RUNNING', updated_at=? WHERE id=?", (now, row["id"]))
            return self.decode(row) | {"state": "RUNNING"}

    def finish(self, scope, command_id, state, result):
        if state not in FINAL_STATES:
            raise ValueError("Etat terminal invalide")
        with self.connect() as db, db:
            changed = db.execute("UPDATE commands SET state=?, result=?, updated_at=? WHERE scope=? AND id=? AND state='RUNNING'",
                                (state, json.dumps(result, allow_nan=False), time.time(), scope, command_id)).rowcount
            if changed != 1:
                raise RuntimeError("Commande non detenue ou deja terminee")

    def recover_interrupted(self, scope):
        # Uniquement au demarrage, APRES acquisition du verrou worker.
        with self.connect() as db, db:
            return db.execute("UPDATE commands SET state='UNCERTAIN', updated_at=?, result=? WHERE scope=? AND state='RUNNING'",
                              (time.time(), json.dumps({"message": "Worker interrompu : verifier Binance, aucun rejeu automatique"}), scope)).rowcount

    def list_recent(self, scope, limit=100):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute("SELECT * FROM commands WHERE scope=? ORDER BY created_at DESC LIMIT ?", (scope, max(1, min(limit, 500))))]

    def cancel_pending(self, scope, command_id):
        with self.connect() as db, db:
            return db.execute("UPDATE commands SET state='CANCELED', updated_at=? WHERE scope=? AND id=? AND state='PENDING'", (time.time(), scope, command_id)).rowcount == 1
