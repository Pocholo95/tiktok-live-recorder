import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import supervisor.reconcile as reconcile_module  # noqa: E402
from db import channels_repo, recordings_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from utils.enums import Mode  # noqa: E402


def test_recover_completes_orphan_whose_file_still_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reconcile_module.VideoManagement,
        "convert_flv_to_mp4",
        staticmethod(lambda file, ffmpeg_path=None: file.replace(".flv", ".mp4")),
    )
    monkeypatch.setattr(
        reconcile_module, "generate_thumbnail", lambda path, ffmpeg_path=None: None
    )

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    raw_file = tmp_path / "TK_creator_2026.01.01_00-00-00_flv.flv"
    raw_file.write_bytes(b"leftover raw recording")
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path=str(raw_file)
    )

    reconcile_module.recover_orphaned_recordings(db_path)

    recording = recordings_repo.get(conn, recording_id)
    assert recording["status"] == "completed"
    assert recording["format"] == "mp4"
    assert recording["file_path"].endswith(".mp4")


def test_recover_marks_failed_when_file_is_gone(tmp_path):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    recording_id = recordings_repo.start(
        conn,
        channel_id=channel_id,
        username="creator",
        file_path=str(tmp_path / "gone_flv.flv"),
    )

    reconcile_module.recover_orphaned_recordings(db_path)

    recording = recordings_repo.get(conn, recording_id)
    assert recording["status"] == "failed"
    assert "missing" in recording["error_message"].lower()


def test_recover_marks_failed_when_conversion_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reconcile_module.VideoManagement,
        "convert_flv_to_mp4",
        staticmethod(lambda file, ffmpeg_path=None: None),
    )

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    raw_file = tmp_path / "TK_creator_2026.01.01_00-00-00_flv.flv"
    raw_file.write_bytes(b"leftover raw recording")
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path=str(raw_file)
    )

    reconcile_module.recover_orphaned_recordings(db_path)

    recording = recordings_repo.get(conn, recording_id)
    assert recording["status"] == "failed"


def test_recover_leaves_completed_recordings_untouched(tmp_path):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )
    recordings_repo.mark_completed(
        conn, recording_id, file_path="/output/a.mp4", format="mp4"
    )

    reconcile_module.recover_orphaned_recordings(db_path)

    recording = recordings_repo.get(conn, recording_id)
    assert recording["status"] == "completed"
