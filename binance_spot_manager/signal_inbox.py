"""Durable, account-scoped signals and immutable execution confirmations."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .config import DATA_DIR
from .signal_parser import content_hash, first_line_is_csi, parse_signal

#: Origines pouvant alimenter l'exécution automatique : Telegram et dépôt direct (ML).
AUTO_SOURCES = frozenset({"telegram", "api"})
#: Préfixe de l'identifiant externe des signaux CSI : seul le dépôt TXT l'emploie.
CSI_EXTERNAL_PREFIX = "csi:"


class CsiChannelRefused(ValueError):
    """Texte CSI arrivé par un autre canal que le dépôt TXT : jamais enregistré.

    L'idempotence (IDEMPOTENCY_KEY, SIGNAL_ID) n'est garantie que par le dépôt :
    un même signal recopié dans Telegram, un JSON v1 ou un collage manuel
    créerait sinon une seconde ligne, donc une seconde position.
    """


class DuplicateSignal(ValueError):
    """Clé d'idempotence ou SIGNAL_ID CSI déjà enregistré pour ce compte.

    ``same_signal`` : le doublon porte le même SIGNAL_ID (rejeu d'un fichier
    déjà pris en compte) ; sinon il s'agit d'un autre identifiant réutilisant
    une clé logique connue, qui n'a jamais été accepté.
    """

    def __init__(self, message, *, signal_id="", same_signal=False):
        super().__init__(message)
        self.signal_id = signal_id
        self.same_signal = same_signal


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
                expires_at REAL NOT NULL DEFAULT 0,
                UNIQUE(scope, hash))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(signals)")}
            for name, definition in (
                ("source_timestamp", "REAL NOT NULL DEFAULT 0"),
                ("auto_state", "TEXT NOT NULL DEFAULT ''"),
                ("auto_detail", "TEXT NOT NULL DEFAULT ''"),
                # Contrat CSI : EXPIRES_AT (Unix, fin d'acceptation) ; 0 pour les formats historiques.
                ("expires_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE signals ADD COLUMN {name} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_offsets (
                bot TEXT PRIMARY KEY, offset INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS signal_origins (
                scope TEXT NOT NULL, external_id TEXT NOT NULL, signal_id TEXT NOT NULL,
                PRIMARY KEY(scope, external_id, signal_id))""")
            # Registre d'idempotence du contrat CSI : une clé logique et un
            # SIGNAL_ID ne sont acceptés qu'une seule fois par compte.
            db.execute("""CREATE TABLE IF NOT EXISTS signal_keys (
                scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, signal_id TEXT NOT NULL,
                row_id TEXT NOT NULL, registered REAL NOT NULL,
                PRIMARY KEY(scope, idempotency_key))""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS signal_keys_signal_id
                ON signal_keys(scope, signal_id)""")
            db.commit()
            yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        return dict(row) | {"parsed": json.loads(row["parsed"]),
                            "payload": json.loads(row["payload"]) if row["payload"] else None}

    @staticmethod
    def _csi_identity(parsed, source, external_id, idempotency_key, producer_signal_id):
        """Clé et SIGNAL_ID lus dans le texte CSI ; refus hors dépôt TXT ou si l'appelant diverge."""
        if source != "api" or not str(external_id).startswith(CSI_EXTERNAL_PREFIX):
            raise CsiChannelRefused(
                "Signal CSI accepté uniquement par le dépôt TXT (data/signal_drop/incoming/*.txt) : "
                "texte non enregistré, aucune exécution")
        if parsed["errors"] or not parsed.get("idempotency_key") or not parsed.get("signal_id"):
            raise ValueError("Contrat CSI invalide : " + " ; ".join(parsed["errors"] or ["identité absente"]))
        key, signal_id = parsed["idempotency_key"], parsed["signal_id"]
        if ((idempotency_key and idempotency_key != key) or (producer_signal_id and producer_signal_id != signal_id)
                or external_id != f"{CSI_EXTERNAL_PREFIX}{signal_id}"):
            raise ValueError("Identité CSI transmise différente de celle du texte : aucun enregistrement")
        return key, signal_id

    def signal_key(self, scope, signal_id):
        """Entrée du registre d'idempotence pour ce SIGNAL_ID, ou None."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM signal_keys WHERE scope=? AND signal_id=?",
                             (scope, signal_id)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _check_idempotency(db, scope, idempotency_key, producer_signal_id, digest):
        """Ligne existante si rejeu strict ; DuplicateSignal pour tout autre doublon ; None sinon."""
        known = db.execute("SELECT * FROM signal_keys WHERE scope=? AND idempotency_key=?",
                           (scope, idempotency_key)).fetchone()
        if known is not None:
            same = known["signal_id"] == producer_signal_id
            if same:
                original = db.execute("SELECT * FROM signals WHERE id=?", (known["row_id"],)).fetchone()
                if original is not None and original["hash"] == digest:
                    return original
                raise DuplicateSignal(
                    f"SIGNAL_ID {producer_signal_id} déjà enregistré avec un texte différent",
                    signal_id=producer_signal_id, same_signal=True,
                )
            raise DuplicateSignal(
                f"Clé d'idempotence déjà reçue pour le signal {known['signal_id']}",
                signal_id=known["signal_id"], same_signal=False,
            )
        by_id = db.execute("SELECT * FROM signal_keys WHERE scope=? AND signal_id=?",
                           (scope, producer_signal_id)).fetchone()
        if by_id is not None:
            raise DuplicateSignal(
                f"SIGNAL_ID {producer_signal_id} déjà reçu avec une autre clé d'idempotence",
                signal_id=producer_signal_id, same_signal=True,
            )
        return None

    def receive(self, scope, raw, *, template="auto", source="manual", external_id="",
                edited=False, source_timestamp=0.0, idempotency_key="", producer_signal_id=""):
        """Enregistre un texte ; les doublons retrouvent la même ligne.

        Un texte CSI (première ligne ``SIGNAL_VERSION=``) n'est accepté que du
        dépôt TXT (source ``api``, identifiant externe ``csi:<SIGNAL_ID>``) ;
        tout autre canal lève CsiChannelRefused. Sa clé d'idempotence et son
        SIGNAL_ID sont extraits ici du texte et inscrits dans ``signal_keys``
        dans la même transaction : une clé ou un SIGNAL_ID déjà connu lève
        DuplicateSignal, sauf rejeu strict du même fichier (même identifiant,
        même texte) qui rend simplement la ligne existante.
        """
        parsed = parse_signal(raw, template).to_dict()
        if first_line_is_csi(raw):
            idempotency_key, producer_signal_id = self._csi_identity(
                parsed, source, external_id, idempotency_key, producer_signal_id)
        elif idempotency_key or producer_signal_id:
            raise ValueError("Clé d'idempotence et SIGNAL_ID réservés aux signaux CSI du dépôt TXT")
        if edited:
            parsed["errors"].append("Message édité : vérifier manuellement via New Trade ; aucun ordre remplacé.")
        digest = content_hash(raw)
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                replay = self._check_idempotency(db, scope, idempotency_key, producer_signal_id, digest)
                if replay is not None:
                    return self.decode(replay)
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
                 source_timestamp, auto_state, auto_detail, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, '', '', ?)""",
                       (uuid.uuid4().hex, scope, digest, source, external_id, time.time(),
                        raw[:20000], json.dumps(parsed, allow_nan=False), float(source_timestamp or 0),
                        float(parsed.get("expires_at") or 0)))
            row = db.execute("SELECT * FROM signals WHERE scope=? AND hash=?", (scope, digest)).fetchone()
            if external_id:
                db.execute("INSERT OR IGNORE INTO signal_origins VALUES (?, ?, ?)", (scope, external_id, row["id"]))
            if idempotency_key:
                db.execute("INSERT INTO signal_keys VALUES (?, ?, ?, ?, ?)",
                           (scope, idempotency_key, producer_signal_id, row["id"], time.time()))
            # A previously reviewed/imported text may be sent again after automatic
            # execution is enabled. Refresh its Telegram (or drop) origin while no
            # payload was ever frozen. A fresh resend may retry a rejected preparation,
            # whereas confirmed/queued rows remain immutable and cannot create another
            # order. A drop file re-imported after a crash keeps its external_id and
            # therefore never resets a previous refusal.
            if (source in AUTO_SOURCES and external_id and source_timestamp
                    and (source == "telegram" or row["external_id"] != external_id)
                    and not edited and not revised and row["payload"] is None
                    and (row["auto_state"] or "") in {"", "REJECTED"}):
                db.execute("""UPDATE signals
                    SET source=?, external_id=?, received=?, source_timestamp=?,
                        auto_state='', auto_detail=''
                    WHERE scope=? AND id=? AND payload IS NULL
                      AND auto_state IN ('', 'REJECTED')""",
                    (source, external_id, time.time(), float(source_timestamp), scope, row["id"]))
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

    def get(self, scope, signal_id):
        with self.connect() as db:
            return self.decode(db.execute(
                "SELECT * FROM signals WHERE scope=? AND id=?", (scope, signal_id)).fetchone())

    def signal_keys(self, scope, limit=500):
        """Signaux CSI enregistrés (clé, SIGNAL_ID, ligne), du plus récent au plus ancien."""
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM signal_keys WHERE scope=? ORDER BY registered DESC, signal_id LIMIT ?",
                (scope, max(1, min(int(limit), 5000))))]

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
                        now, limit=10, sources=("telegram",)):
        """Fresh signals from `sources` eligible for automatic preparation.

        `sources` is an iterable of source names sharing `enabled_since`, or a
        mapping {source: enabled_since} when an origin was authorized separately
        (the effective start is then the latest of both values). Only Telegram
        and drop ("api") rows can ever be selected.
        PROCESSING rows are included for crash recovery even if their age window
        elapsed; an existing frozen payload or command decides their final state.
        Rows carrying a CSI EXPIRES_AT ignore the age window: the executor applies
        VALID_FROM <= now < EXPIRES_AT itself and records an explicit refusal.
        """
        if isinstance(sources, dict):
            windows = {str(name): max(float(enabled_since), float(since))
                       for name, since in sources.items()}
        else:
            windows = {str(name): float(enabled_since) for name in sources}
        windows = sorted((name, since) for name, since in windows.items() if name in AUTO_SOURCES)
        if not windows:
            return []
        clause = " OR ".join(["(source=? AND received>=?)"] * len(windows))
        params = [value for window in windows for value in window]
        with self.connect() as db:
            rows = db.execute("SELECT * FROM signals WHERE scope=? AND (" + clause + """)
                  AND auto_state IN ('', 'PROCESSING')
                  AND (auto_state='PROCESSING' OR expires_at>0 OR
                       (source_timestamp>=? AND source_timestamp<=?))
                ORDER BY received, id LIMIT ?""",
                (scope, *params, float(oldest_source_timestamp),
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
