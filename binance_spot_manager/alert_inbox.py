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

    def counts(self):
        with closing(self.connect()) as db:
            row = db.execute("""SELECT COUNT(*) AS total,
                COUNT(CASE WHEN acknowledged_at IS NULL THEN 1 END) AS unread,
                COUNT(acknowledged_at) AS read FROM alerts""").fetchone()
            return dict(row)

    def recent(self, limit=100, *, unread=False, read=False):
        if unread and read:
            raise ValueError("Choisir les alertes lues ou non lues, pas les deux")
        condition = "WHERE acknowledged_at IS NULL " if unread else (
            "WHERE acknowledged_at IS NOT NULL " if read else ""
        )
        with closing(self.connect()) as db:
            return [dict(row) | {"record": json.loads(row["record"])} for row in db.execute(
                "SELECT * FROM alerts " + condition + "ORDER BY last_seen DESC, id LIMIT ?",
                (max(1, min(limit, 500)),),
            )]

    def versions(self):
        """Toutes les versions observees, sans limite de pagination."""
        with closing(self.connect()) as db:
            return {row["id"]: row["last_seen"] for row in db.execute("SELECT id, last_seen FROM alerts")}

    def acknowledge(self, alert_id, *, expected_last_seen=None):
        with closing(self.connect()) as db, db:
            return db.execute("""UPDATE alerts SET acknowledged_at=?, delivered=1
                WHERE id=? AND (? IS NULL OR last_seen=?)""",
                (time.time(), alert_id, expected_last_seen, expected_last_seen)).rowcount

    def acknowledge_all(self, *, expected_versions=None):
        with closing(self.connect()) as db, db:
            if expected_versions is not None:
                now = time.time()
                return db.executemany("""UPDATE alerts SET acknowledged_at=?, delivered=1
                    WHERE id=? AND last_seen=? AND acknowledged_at IS NULL""",
                    [(now, alert_id, version) for alert_id, version in expected_versions.items()]).rowcount
            return db.execute(
                "UPDATE alerts SET acknowledged_at=?, delivered=1 WHERE acknowledged_at IS NULL",
                (time.time(),),
            ).rowcount

    @staticmethod
    def _delete_version(db, alert_id, expected_last_seen):
        params = (alert_id, expected_last_seen, expected_last_seen)
        db.execute("""INSERT INTO deleted_alerts SELECT id, last_seen FROM alerts
            WHERE id=? AND (? IS NULL OR last_seen=?)
            ON CONFLICT(id) DO UPDATE SET last_seen=MAX(deleted_alerts.last_seen, excluded.last_seen)""", params)
        return db.execute("DELETE FROM alerts WHERE id=? AND (? IS NULL OR last_seen=?)", params).rowcount

    def delete(self, alert_id, *, expected_last_seen=None):
        """Retire l'alerte sans qu'un autre onglet puisse rejouer son ancien evenement."""
        with closing(self.connect()) as db, db:
            return self._delete_version(db, alert_id, expected_last_seen)

    def delete_all(self, *, expected_versions=None):
        with closing(self.connect()) as db, db:
            if expected_versions is not None:
                return sum(self._delete_version(db, alert_id, version)
                           for alert_id, version in expected_versions.items())
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
