import sqlite3
import threading
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_local = threading.local()


def init_db(db_path):
    """
    Creates the schema if it doesn't exist yet. Safe to call every time the
    process boots (serve.py) since every statement is CREATE ... IF NOT EXISTS.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()


def get_connection(db_path):
    """
    Returns a connection scoped to the current thread.

    sqlite3 connections aren't safe to share across threads, and both the
    supervisor's worker threads and FastAPI's threadpool need to read/write
    concurrently, so each thread gets and keeps its own connection instead
    of a single shared one.
    """
    db_path = str(db_path)
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "db_path", None) != db_path:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _local.db_path = db_path
    return conn
