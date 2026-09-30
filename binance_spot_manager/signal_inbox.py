"""Durable, account-scoped signals and immutable execution confirmations."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .config import DATA_DIR
from .signal_parser import (
    UNVERIFIABLE_SOURCE_DATE_WARNING,
    content_hash,
    parse_signal,
)


class SignalInbox:
    def __init__(self, path=DATA_DIR / "signals.sqlite3"):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, hash TEXT NOT NULL,
                source TEXT NOT NULL, external_id TEXT NOT NULL, received REAL NOT NULL,
                raw TEXT NOT NULL, parsed TEXT NOT NULL, payload TEXT,
                source_timestamp REAL NOT NULL DEFAULT 0,
                auto_state TEXT NOT NULL DEFAULT '',
                auto_detail TEXT NOT NULL DEFAULT '',
                UNIQUE(scope, hash))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(signals)")}
            for name, definition in (
                ("source_timestamp", "REAL NOT NULL DEFAULT 0"),
                ("auto_state", "TEXT NOT NULL DEFAULT ''"),
                ("auto_detail", "TEXT NOT NULL DEFAULT ''"),
                # Avis de CryptoSignalIntelligence : information affichée, jamais un ordre.
                ("csi_verdict", "TEXT NOT NULL DEFAULT ''"),
                ("csi_detail", "TEXT NOT NULL DEFAULT ''"),
                ("csi_evaluated_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE signals ADD COLUMN {name} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_offsets (
                bot TEXT PRIMARY KEY, offset INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS signal_origins (
                scope TEXT NOT NULL, external_id TEXT NOT NULL, signal_id TEXT NOT NULL,
                PRIMARY KEY(scope, external_id, signal_id))""")
            db.commit()
            yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        parsed = json.loads(row["parsed"])
        # Telegram provides its own server timestamp. It is the authoritative
        # freshness source, so an absent date inside the message is not a warning.
        if row["source"] == "telegram":
            parsed["warnings"] = [
                warning for warning in parsed.get("warnings", [])
                if warning != UNVERIFIABLE_SOURCE_DATE_WARNING
            ]
        return dict(row) | {"parsed": parsed,
                            "payload": json.loads(row["payload"]) if row["payload"] else None}

    def receive(self, scope, raw, *, template="auto", source="manual", external_id="",
                edited=False, source_timestamp=0.0):
        parsed = parse_signal(raw, template).to_dict()
        if source == "telegram":
            parsed["warnings"] = [
                warning for warning in parsed["warnings"]
                if warning != UNVERIFIABLE_SOURCE_DATE_WARNING
            ]
        if edited:
            parsed["errors"].append("Message édité : vérifier manuellement via New Trade ; aucun ordre remplacé.")
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            originals = db.execute("""SELECT signals.* FROM signals JOIN signal_origins
                ON signals.id=signal_origins.signal_id WHERE signal_origins.scope=? AND signal_origins.external_id=?""",
                (scope, external_id)).fetchall() if external_id else []
            revised = any(original["hash"] != content_hash(raw) for original in originals)
            if revised:
                parsed["errors"].append("Révision d'un message déjà reçu : aucune seconde exécution automatique.")
            if edited or revised:
                for original in originals:
                    previous = json.loads(original["parsed"])
                    error = "Message source édité : ancienne version invalidée. Si déjà transmise, vérifier la position dans Opérations."
                    if error not in previous["errors"]:
                        previous["errors"].append(error)
                    db.execute("UPDATE signals SET parsed=? WHERE id=?", (json.dumps(previous), original["id"]))
            db.execute("""INSERT OR IGNORE INTO signals
                (id, scope, hash, source, external_id, received, raw, parsed, payload,
                 source_timestamp, auto_state, auto_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, '', '')""",
                       (uuid.uuid4().hex, scope, content_hash(raw), source, external_id, time.time(),
                        raw[:20000], json.dumps(parsed, allow_nan=False), float(source_timestamp or 0)))
            row = db.execute("SELECT * FROM signals WHERE scope=? AND hash=?", (scope, content_hash(raw))).fetchone()
            if external_id:
                db.execute("INSERT OR IGNORE INTO signal_origins VALUES (?, ?, ?)", (scope, external_id, row["id"]))
            # A previously reviewed/imported text may be sent again after automatic
            # execution is enabled. Refresh its Telegram origin while no payload was
            # ever frozen. A fresh resend may retry a rejected preparation, whereas
            # confirmed/queued rows remain immutable and cannot create another order.
            if (source == "telegram" and external_id and source_timestamp
                    and not edited and not revised and row["payload"] is None
                    and (row["auto_state"] or "") in {"", "REJECTED"}):
                db.execute("""UPDATE signals
                    SET source='telegram', external_id=?, received=?, source_timestamp=?,
                        auto_state='', auto_detail=''
                    WHERE scope=? AND id=? AND payload IS NULL
                      AND auto_state IN ('', 'REJECTED')""",
                    (external_id, time.time(), float(source_timestamp), scope, row["id"]))
                row = db.execute(
                    "SELECT * FROM signals WHERE scope=? AND id=?", (scope, row["id"]),
                ).fetchone()
            if edited or revised:
                previous = json.loads(row["parsed"])
                error = "Message édité : vérifier manuellement via New Trade, aucune seconde exécution."
                if error not in previous["errors"]:
                    previous["errors"].append(error)
                    db.execute("UPDATE signals SET parsed=? WHERE id=?", (json.dumps(previous), row["id"]))
                    row = db.execute("SELECT * FROM signals WHERE id=?", (row["id"],)).fetchone()
            return self.decode(row)

    def recent(self, scope):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute(
                "SELECT * FROM signals WHERE scope=? ORDER BY received DESC LIMIT 100", (scope,))]

    def reanalyse(self, scope, signal_id, *, template="auto"):
        """Refresh parsing after a format upgrade, retaining edit blocks and confirmations."""
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM signals WHERE scope=? AND id=?", (scope, signal_id)).fetchone()
            if row is None or row["payload"] is not None:
                raise ValueError("Signal absent ou déjà confirmé : réanalyse impossible.")
            previous = json.loads(row["parsed"])
            if any(error.startswith(("Message édité", "Message source édité", "Révision d'un message"))
                   for error in previous["errors"]):
                raise ValueError("Message source édité : réanalyse bloquée, vérifier manuellement via New Trade.")
            parsed = parse_signal(row["raw"], template).to_dict()
            db.execute("UPDATE signals SET parsed=? WHERE scope=? AND id=?",
                       (json.dumps(parsed, allow_nan=False), scope, signal_id))
            return self.decode(db.execute("SELECT * FROM signals WHERE scope=? AND id=?", (scope, signal_id)).fetchone())

    def freeze(self, scope, signal_id, payload):
        """First confirmation wins, including random IDs; never overwrite a sent/uncertain plan."""
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM signals WHERE scope=? AND id=?", (scope, signal_id)).fetchone()
            if row is None or json.loads(row["parsed"])["errors"]:
                raise ValueError("Signal absent ou bloqué")
            if row["payload"]:
                if row["payload"] != encoded:
                    raise ValueError("Signal déjà confirmé : consulter la commande existante dans Opérations.")
                return json.loads(row["payload"])
            db.execute("UPDATE signals SET payload=? WHERE scope=? AND id=?", (encoded, scope, signal_id))
            return payload

    def offset(self, bot):
        with self.connect() as db:
            row = db.execute("SELECT offset FROM telegram_offsets WHERE bot=?", (bot,)).fetchone()
            return row[0] if row else 0

    def advance(self, bot, offset):
        with self.connect() as db, db:
            db.execute("INSERT INTO telegram_offsets VALUES (?, ?) ON CONFLICT(bot) DO UPDATE SET offset=MAX(offset, excluded.offset)",
                       (bot, offset))

    def auto_candidates(self, scope, *, enabled_since, oldest_source_timestamp,
                        now, limit=10):
        """Fresh Telegram signals eligible for automatic preparation.

        PROCESSING rows are included for crash recovery even if their age window
        elapsed; an existing frozen payload or command decides their final state.
        """
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM signals
                WHERE scope=? AND source='telegram' AND received>=?
                  AND auto_state IN ('', 'PROCESSING')
                  AND (auto_state='PROCESSING' OR
                       (source_timestamp>=? AND source_timestamp<=?))
                ORDER BY received, id LIMIT ?""",
                (scope, float(enabled_since), float(oldest_source_timestamp),
                 float(now) + 60, max(1, min(int(limit), 50)))).fetchall()
            return [self.decode(row) for row in rows]

    def claim_auto(self, scope, signal_id):
        with self.connect() as db, db:
            changed = db.execute("""UPDATE signals
                SET auto_state='PROCESSING', auto_detail=''
                WHERE scope=? AND id=? AND auto_state=''""",
                (scope, signal_id)).rowcount
            if changed:
                return True
            row = db.execute(
                "SELECT auto_state FROM signals WHERE scope=? AND id=?",
                (scope, signal_id),
            ).fetchone()
            return bool(row and row[0] == "PROCESSING")

    def set_auto_state(self, scope, signal_id, state, detail=""):
        if state not in {"QUEUED", "REJECTED", "PROCESSING"}:
            raise ValueError("État automatique invalide")
        with self.connect() as db, db:
            changed = db.execute("""UPDATE signals SET auto_state=?, auto_detail=?
                WHERE scope=? AND id=?""",
                (state, str(detail)[:1000], scope, signal_id)).rowcount
            if changed != 1:
                raise ValueError("Signal automatique absent")

    def set_csi_opinion(self, scope, signal_id, verdict, detail="", evaluated_at=""):
        """Dernier avis de CryptoSignalIntelligence sur ce signal (affiché, jamais exécuté)."""
        with self.connect() as db, db:
            changed = db.execute("""UPDATE signals SET csi_verdict=?, csi_detail=?, csi_evaluated_at=?
                WHERE scope=? AND id=?""",
                (str(verdict)[:32], str(detail)[:2000], str(evaluated_at)[:64], scope, signal_id)).rowcount
            if changed != 1:
                raise ValueError("Signal absent")
