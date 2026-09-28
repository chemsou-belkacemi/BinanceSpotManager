"""Boite d'alertes persistante : deduplication et acquittement local."""

from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path
import time

from .ui_alerts import ALERT_EVENTS


class AlertInbox:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY, record TEXT NOT NULL, first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL, occurrences INTEGER NOT NULL, acknowledged_at REAL,
            delivered INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS deleted_alerts (
            id TEXT PRIMARY KEY, last_seen TEXT NOT NULL)""")
        db.commit()
        return db

    @staticmethod
    def key(record):
        identity = {k: record.get(k) for k in ("event", "position_id", "symbol", "message", "data")}
        # Les erreurs identiques sont regroupees, pas chaque nouvelle occurrence.
        if record.get("level") not in {"ERROR", "CRITICAL"}:
            identity["ts"] = record.get("ts")
        return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()

    def ingest(self, record):
        if record.get("event") not in ALERT_EVENTS and record.get("level") not in {"ERROR", "CRITICAL"}:
            return
        key, stamp = self.key(record), str(record.get("ts", ""))
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO alerts
                SELECT ?, ?, ?, ?, 1, NULL, 0
                WHERE NOT EXISTS (SELECT 1 FROM deleted_alerts WHERE id=? AND last_seen>=?)
                ON CONFLICT(id) DO UPDATE SET record=excluded.record, last_seen=excluded.last_seen,
                occurrences=alerts.occurrences+1,
                delivered=CASE WHEN alerts.acknowledged_at IS NOT NULL THEN 0 ELSE alerts.delivered END,
                acknowledged_at=NULL WHERE excluded.last_seen>last_seen""",
                       (key, json.dumps(record, default=str), stamp, stamp, key, stamp))

    def recent(self, limit=100, *, unread=False):
        with closing(self.connect()) as db:
            return [dict(row) | {"record": json.loads(row["record"])} for row in db.execute(
                "SELECT * FROM alerts " + ("WHERE acknowledged_at IS NULL " if unread else "") + "ORDER BY last_seen DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            )]

    def acknowledge(self, alert_id):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE alerts SET acknowledged_at=?, delivered=1 WHERE id=?", (time.time(), alert_id))

    def acknowledge_all(self):
        with closing(self.connect()) as db, db:
            return db.execute(
                "UPDATE alerts SET acknowledged_at=?, delivered=1 WHERE acknowledged_at IS NULL",
                (time.time(),),
            ).rowcount

    def delete(self, alert_id):
        """Retire l'alerte sans qu'un autre onglet puisse rejouer son ancien evenement."""
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO deleted_alerts SELECT id, last_seen FROM alerts WHERE id=?
                ON CONFLICT(id) DO UPDATE SET last_seen=MAX(deleted_alerts.last_seen, excluded.last_seen)""",
                       (alert_id,))
            return db.execute("DELETE FROM alerts WHERE id=?", (alert_id,)).rowcount

    def delete_all(self):
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO deleted_alerts SELECT id, last_seen FROM alerts WHERE 1
                ON CONFLICT(id) DO UPDATE SET last_seen=MAX(deleted_alerts.last_seen, excluded.last_seen)""")
            return db.execute("DELETE FROM alerts").rowcount

    def claim_delivery(self, record):
        self.ingest(record)
        with closing(self.connect()) as db, db:
            return db.execute(
                "UPDATE alerts SET delivered=1 WHERE id=? AND delivered=0 AND acknowledged_at IS NULL AND last_seen=?",
                (self.key(record), str(record.get("ts", ""))),
            ).rowcount == 1
