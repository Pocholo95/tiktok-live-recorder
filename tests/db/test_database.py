import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import channels_repo, recordings_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from utils.enums import Mode  # noqa: E402


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "hub.db"

    init_db(db_path)
    init_db(db_path)

    conn = get_connection(db_path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"channels", "recordings"} <= tables


def test_channel_crud_round_trip(tmp_path):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)

    channel_id = channels_repo.insert(
        conn, username="creator", mode=Mode.AUTOMATIC, automatic_interval=10
    )

    channel = channels_repo.get(conn, channel_id)
    assert channel["username"] == "creator"
    assert channel["automatic_interval"] == 10
    assert channel["enabled"] == 1

    channels_repo.update(conn, channel_id, automatic_interval=15, enabled=0)
    channel = channels_repo.get(conn, channel_id)
    assert channel["automatic_interval"] == 15
    assert channel["enabled"] == 0
    assert channels_repo.list_enabled(conn) == []

    channels_repo.delete(conn, channel_id)
    assert channels_repo.get(conn, channel_id) is None


def test_channel_update_rejects_unknown_fields(tmp_path):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    with pytest.raises(ValueError):
        channels_repo.update(conn, channel_id, not_a_real_field="x")


def test_recording_lifecycle_and_filtering(tmp_path):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)

    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a_flv.flv"
    )

    recording = recordings_repo.get(conn, recording_id)
    assert recording["status"] == "recording"

    recordings_repo.mark_completed(
        conn,
        recording_id,
        file_path="/output/a.mp4",
        format="mp4",
        duration_seconds=120,
        file_size_bytes=1024,
    )

    recording = recordings_repo.get(conn, recording_id)
    assert recording["status"] == "completed"
    assert recording["format"] == "mp4"
    assert recording["file_path"] == "/output/a.mp4"

    results = recordings_repo.list_filtered(conn, channel_id=channel_id)
    assert [r["id"] for r in results] == [recording_id]
    assert recordings_repo.list_filtered(conn, channel_id=channel_id + 1) == []


def test_recording_mark_failed_keeps_channel_optional(tmp_path):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)

    recording_id = recordings_repo.start(
        conn, channel_id=None, username="creator", file_path="/output/a_flv.flv"
    )
    recordings_repo.mark_failed(conn, recording_id, error_message="boom")

    recording = recordings_repo.get(conn, recording_id)
    assert recording["status"] == "failed"
    assert recording["error_message"] == "boom"


def test_recording_delete_removes_row(tmp_path):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)

    recording_id = recordings_repo.start(
        conn, channel_id=None, username="creator", file_path="/output/a.mp4"
    )
    recordings_repo.delete(conn, recording_id)

    assert recordings_repo.get(conn, recording_id) is None
