import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import recordings_repo, settings_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from supervisor import backups, originals  # noqa: E402
from supervisor.housekeeping import Housekeeping  # noqa: E402

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _converted(conn, tmp_path, name, kept_days_ago, size=10):
    raw = tmp_path / f"{name}.ts"
    raw.write_bytes(b"x" * size)
    rid = recordings_repo.start(conn, channel_id=None, username="c", file_path=str(raw))
    recordings_repo.mark_completed(conn, rid, file_path=str(raw), format="ts")
    recordings_repo.replace_with_converted(
        conn,
        rid,
        file_path=str(raw.with_suffix(".mp4")),
        file_size_bytes=1,
        duration_seconds=1,
        thumbnail_path=None,
    )
    recordings_repo.keep_original(conn, rid, str(raw))
    kept = (NOW - timedelta(days=kept_days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE recordings SET original_kept_at = ? WHERE id = ?", (kept, rid))
    conn.commit()
    return rid, raw


def _db(tmp_path):
    db_path = tmp_path / "hub.db"
    init_db(db_path)
    return db_path, get_connection(db_path)


def test_cleanup_deletes_expired_originals_only(tmp_path):
    _, conn = _db(tmp_path)
    old_id, old = _converted(conn, tmp_path, "old", kept_days_ago=8)
    new_id, new = _converted(conn, tmp_path, "new", kept_days_ago=2)

    assert originals.cleanup(conn, tmp_path, now=NOW, free_bytes=10**15) == 1

    assert not old.exists() and new.exists()
    assert recordings_repo.get(conn, old_id)["original_path"] is None
    assert recordings_repo.get(conn, new_id)["original_path"] == str(new)

    # Fewer days in Settings also applies to originals already kept.
    settings_repo.set(conn, originals.KEEP_DAYS_KEY, "1")
    assert originals.cleanup(conn, tmp_path, now=NOW, free_bytes=10**15) == 1
    assert not new.exists()


def test_cleanup_frees_oldest_first_when_disk_is_low(tmp_path, monkeypatch):
    _, conn = _db(tmp_path)
    monkeypatch.setenv("HUB_ORIGINALS_MIN_FREE_GB", "0.000001")  # ~1074 bytes
    _, a = _converted(conn, tmp_path, "a", kept_days_ago=3, size=600)
    _, b = _converted(conn, tmp_path, "b", kept_days_ago=2, size=600)
    _, c = _converted(conn, tmp_path, "c", kept_days_ago=1, size=600)

    assert originals.cleanup(conn, tmp_path, now=NOW, free_bytes=0) == 2

    assert not a.exists() and not b.exists() and c.exists()


def test_cleanup_forgets_originals_deleted_by_hand(tmp_path):
    _, conn = _db(tmp_path)
    rid, raw = _converted(conn, tmp_path, "gone", kept_days_ago=1)
    raw.unlink()

    assert originals.cleanup(conn, tmp_path, now=NOW, free_bytes=10**15) == 0
    assert recordings_repo.get(conn, rid)["original_path"] is None


def test_daily_backup_is_consistent_and_pruned(tmp_path):
    db_path, conn = _db(tmp_path)
    settings_repo.set(conn, "marker", "hello")
    folder = tmp_path / "backups"
    for day in range(1, backups.KEEP + 3):
        (folder).mkdir(exist_ok=True)
        (folder / f"hub-2026-10-{day:02d}.db").write_bytes(b"old")

    target = backups.run_if_due(db_path, folder, today="2026-11-20")

    assert backups.run_if_due(db_path, folder, today="2026-11-20") is None
    copy = sqlite3.connect(target)
    try:
        value = copy.execute(
            "SELECT value FROM settings WHERE key = 'marker'"
        ).fetchone()[0]
    finally:
        copy.close()
    assert value == "hello"
    kept = backups.existing(folder)
    assert len(kept) == backups.KEEP and kept[-1] == target
    assert not list(folder.glob("*.part"))


def test_housekeeping_survives_failures(tmp_path, monkeypatch):
    db_path, conn = _db(tmp_path)
    monkeypatch.setenv("HUB_BACKUP_DIR", str(tmp_path / "bk"))

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(originals, "cleanup", boom)
    Housekeeping(str(db_path), get_connection, str(tmp_path)).run_once()

    assert len(backups.existing(tmp_path / "bk")) == 1
