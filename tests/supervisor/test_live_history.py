import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import recordings_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from supervisor import backups, schedules  # noqa: E402


def _live(conn, username, day):
    rid = recordings_repo.start(
        conn, channel_id=None, username=username, file_path=f"/x/{day}.ts"
    )
    recordings_repo.mark_completed(
        conn, rid, file_path=f"/x/{day}.ts", format="ts", duration_seconds=3600
    )
    conn.execute(
        "UPDATE recordings SET started_at = ?, ended_at = ? WHERE id = ?",
        (f"2026-11-{day:02d} 02:00:00", f"2026-11-{day:02d} 03:00:00", rid),
    )
    conn.commit()
    return rid


def _lives(conn, username):
    return schedules.get(
        conn, username, now=datetime(2026, 11, 21, tzinfo=timezone.utc), use_cache=False
    )["lives"]


def test_deleting_recordings_keeps_the_learned_schedule(tmp_path):
    db_path = tmp_path / "hub.db"
    init_db(db_path)
    conn = get_connection(db_path)
    ids = [_live(conn, "ana", day) for day in (10, 11, 12, 13, 14)]
    assert _lives(conn, "ana") == 5

    for rid in ids[:3]:
        recordings_repo.delete(conn, rid)  # "streams que no me sirven"

    assert _lives(conn, "ana") == 5
    row = conn.execute(
        "SELECT * FROM live_history WHERE recording_id = ?", (ids[0],)
    ).fetchone()
    assert row["ended_at"] == "2026-11-10 03:00:00" and row["status"] == "completed"


def test_existing_installs_are_backfilled_on_startup(tmp_path):
    db_path = tmp_path / "hub.db"
    init_db(db_path)
    conn = get_connection(db_path)
    rid = _live(conn, "ana", 10)
    conn.execute("DELETE FROM live_history")  # as before live_history existed
    conn.commit()

    init_db(db_path)  # next boot

    assert conn.execute("SELECT recording_id FROM live_history").fetchall()[0][0] == rid


def test_lives_deleted_earlier_come_back_from_the_daily_backups(tmp_path):
    db_path = tmp_path / "hub.db"
    init_db(db_path)
    conn = get_connection(db_path)
    kept = _live(conn, "ana", 10)
    gone = _live(conn, "ana", 11)
    ongoing = recordings_repo.start(
        conn, channel_id=None, username="ana", file_path="/x/now.ts"
    )
    backups.run_if_due(db_path, tmp_path / "bk", today="2026-11-20")

    # Deleted before live_history existed: gone from both tables.
    for rid in (gone, ongoing):
        recordings_repo.delete(conn, rid)
    conn.execute(
        "DELETE FROM live_history WHERE recording_id IN (?, ?)", (gone, ongoing)
    )
    conn.commit()
    assert _lives(conn, "ana") == 1

    assert backups.recover_live_history(conn, tmp_path / "bk") == 1
    assert _lives(conn, "ana") == 2
    ids = {r[0] for r in conn.execute("SELECT recording_id FROM live_history")}
    assert ids == {kept, gone}  # the row still "recording" in the backup is skipped
    assert backups.recover_live_history(conn, tmp_path / "bk") == 0  # idempotent


def test_unreadable_backup_is_skipped(tmp_path):
    db_path = tmp_path / "hub.db"
    init_db(db_path)
    conn = get_connection(db_path)
    folder = tmp_path / "bk"
    folder.mkdir()
    (folder / "hub-2026-11-20.db").write_bytes(b"not a database")
    assert backups.recover_live_history(conn, folder) == 0
    assert isinstance(conn, sqlite3.Connection)
