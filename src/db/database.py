import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def init_db(db_path):
    """
    Creates the schema if it doesn't exist yet, and switches the database to
    WAL mode. Safe to call every time the process boots (serve.py) since
    every schema statement is CREATE ... IF NOT EXISTS.

    WAL mode is set here - once, sequentially, before any request/worker
    threads exist - because unlike `busy_timeout`/`foreign_keys` (per-
    connection session settings), journal_mode is persisted in the database
    file itself. Setting it from every new per-thread connection in
    `get_connection` used to race: many threads opening their first
    connection at once (e.g. a page loading a dozen thumbnails in parallel)
    would all try to flip the file's journal mode simultaneously and some
    would fail with "database is locked".
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA_PATH.read_text())
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS doesn't
# touch existing tables, so databases created before a column existed get
# it here (with its default) on the next boot.
_ADDED_COLUMNS = {
    "channels": [("smart_interval", "INTEGER NOT NULL DEFAULT 0")],
}


def _migrate(conn):
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def get_connection(db_path):
    """
    Opens a fresh connection. Callers are responsible for closing it
    (e.g. via `contextlib.closing`) once they're done.

    This used to cache one connection per thread, on the assumption that
    whoever calls `get_connection` keeps using it from that same thread.
    That's true for the supervisor's worker threads, but not for FastAPI:
    each sync `Depends()` callable (including the one that fetches this
    connection) is dispatched to the threadpool independently from the
    route body that actually uses it, so the "creating" and "using" thread
    can differ - which raised `sqlite3.ProgrammingError: ... created in a
    thread ... used in another thread` under concurrent requests.
    `check_same_thread=False` makes the connection safe to hand off across
    threads like this; it's fine here because it's never used by two
    threads *at once*, only sequentially, one request at a time.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
