"""Migration du schema des signaux sure en concurrence (UI, worker et threads Telegram).

L'ancienne migration lisait les colonnes puis faisait ALTER TABLE hors transaction : une autre
connexion pouvait ajouter la meme colonne entre les deux (« duplicate column name »), de facon
intermittente dans test_deduplication_and_immutable_confirmation. Ici la course est provoquee
a coup sur.
"""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

from binance_spot_manager import signal_inbox
from binance_spot_manager.signal_inbox import SignalInbox

SIMPLE = "BTCUSDT\nEntry: 84000\nSL: 80000\nT1: 90000"

OLD_SCHEMA = """CREATE TABLE signals (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, hash TEXT NOT NULL,
    source TEXT NOT NULL, external_id TEXT NOT NULL, received REAL NOT NULL,
    raw TEXT NOT NULL, parsed TEXT NOT NULL, payload TEXT,
    source_timestamp REAL NOT NULL DEFAULT 0,
    auto_state TEXT NOT NULL DEFAULT '',
    auto_detail TEXT NOT NULL DEFAULT '',
    UNIQUE(scope, hash))"""


def old_database(path):
    with sqlite3.connect(path) as db:
        db.execute(OLD_SCHEMA)
        db.execute("CREATE TABLE telegram_offsets (bot TEXT PRIMARY KEY, offset INTEGER NOT NULL)")
        db.execute("""CREATE TABLE signal_origins (
            scope TEXT NOT NULL, external_id TEXT NOT NULL, signal_id TEXT NOT NULL,
            PRIMARY KEY(scope, external_id, signal_id))""")
        # Tables complètes, colonnes anciennes : la course porte sur les COLONNES (une table manquante,
        # comme signal_keys du contrat CSI, fait passer directement sous verrou, sans course possible).
        db.execute("""CREATE TABLE signal_keys (
            scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, signal_id TEXT NOT NULL,
            row_id TEXT NOT NULL, registered REAL NOT NULL, PRIMARY KEY(scope, idempotency_key))""")
    return path


def columns(path):
    with sqlite3.connect(path) as db:
        return [row[1] for row in db.execute("PRAGMA table_info(signals)")]


def test_concurrent_migration_between_schema_read_and_alter_is_safe(tmp_path, monkeypatch):
    """Une autre connexion ajoute une colonne manquante juste APRES notre lecture du schema."""
    path = old_database(tmp_path / "signals.db")
    real_connect = sqlite3.connect
    interference = []

    def other_process_migrates():
        other = real_connect(path, timeout=0)
        try:
            present = {row[1] for row in other.execute("PRAGMA table_info(signals)")}
            missing = next(name for name in ("csi_verdict", "csi_detail") if name not in present)
            other.execute(f"ALTER TABLE signals ADD COLUMN {missing} TEXT NOT NULL DEFAULT ''")
            other.commit()
            interference.append("ajoutee")
        except sqlite3.OperationalError as exc:  # verrou tenu, ou colonne deja la
            interference.append(str(exc))
        finally:
            other.close()

    class RacingConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.strip().startswith("PRAGMA table_info(signals)"):
                rows = super().execute(sql, *args).fetchall()
                other_process_migrates()  # la lecture ci-dessus est desormais perimee
                return rows
            return super().execute(sql, *args)

    def connect(*args, **kwargs):
        return real_connect(*args, factory=RacingConnection, **kwargs)

    monkeypatch.setattr(signal_inbox.sqlite3, "connect", connect)
    inbox = SignalInbox(path)
    with inbox.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    # 1re lecture (sans verrou) : l'autre connexion ajoute la colonne ; 2e lecture, sous
    # BEGIN IMMEDIATE : l'autre connexion ne peut plus rien changer.
    assert interference == ["ajoutee", "database is locked"]
    monkeypatch.undo()
    names = columns(path)
    assert len(names) == len(set(names))
    assert {"csi_verdict", "csi_detail", "csi_evaluated_at"} <= set(names)


def test_parallel_first_use_of_an_old_database(tmp_path):
    path = old_database(tmp_path / "signals.db")
    inbox = SignalInbox(path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: inbox.receive("demo", SIMPLE)["id"], range(16)))
    assert len(set(ids)) == 1
    assert columns(path).count("csi_verdict") == 1


def test_up_to_date_schema_takes_no_write_lock(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    with inbox.connect():
        pass
    # Un autre processus tient le verrou d'ecriture : une simple lecture doit passer.
    holder = sqlite3.connect(inbox.path, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        inbox_fast = SignalInbox(inbox.path)
        with inbox_fast.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    finally:
        holder.rollback()
        holder.close()
