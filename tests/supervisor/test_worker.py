import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import supervisor.worker as worker_module  # noqa: E402
from db import channels_repo, recordings_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from supervisor.status import ChannelStatusStore  # noqa: E402
from utils.enums import Mode  # noqa: E402


class _FakeRecorder:
    """Stands in for TikTokRecorder: synchronously fires a scripted
    on_status sequence instead of doing any real network/ffmpeg work."""

    def __init__(self, config):
        self.config = config

    def run(self):
        on_status = self.config.on_status
        on_status(
            "recording_started",
            user=self.config.user,
            file_path="/output/TK_creator_flv.flv",
        )
        on_status(
            "converting", user=self.config.user, file_path="/output/TK_creator.mp4"
        )
        on_status(
            "recording_finished",
            user=self.config.user,
            file_path="/output/TK_creator.mp4",
            format="mp4",
        )


def _channel_row(conn, **overrides):
    fields = {
        "username": "creator",
        "mode": Mode.AUTOMATIC,
        "automatic_interval": 5,
        "proxy": None,
        "bitrate": None,
        "use_telegram": False,
    }
    fields.update(overrides)
    channel_id = channels_repo.insert(conn, **fields)
    return channels_repo.get(conn, channel_id)


def test_worker_writes_completed_recording_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "TikTokRecorder", _FakeRecorder)
    monkeypatch.setattr(worker_module, "generate_thumbnail", lambda path, **kw: None)
    monkeypatch.setattr(worker_module, "read_cookies", lambda: {})

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    status_store = ChannelStatusStore()
    channel_row = _channel_row(conn)

    worker = worker_module.Worker(
        channel_row, db_path, get_connection, tmp_path, status_store
    )
    worker.start()
    worker.join(timeout=2)

    recordings = recordings_repo.list_filtered(conn, channel_id=channel_row["id"])
    assert len(recordings) == 1
    assert recordings[0]["status"] == "completed"
    assert recordings[0]["format"] == "mp4"
    assert recordings[0]["file_path"] == "/output/TK_creator.mp4"

    assert status_store.get(channel_row["id"])["phase"] == "idle"


def test_worker_marks_recording_failed_on_remux_error(tmp_path, monkeypatch):
    class _FailingRecorder:
        def __init__(self, config):
            self.config = config

        def run(self):
            on_status = self.config.on_status
            on_status(
                "recording_started",
                user=self.config.user,
                file_path="/output/TK_creator_flv.flv",
            )
            on_status("recording_error", user=self.config.user, error="remux failed")

    monkeypatch.setattr(worker_module, "TikTokRecorder", _FailingRecorder)
    monkeypatch.setattr(worker_module, "read_cookies", lambda: {})

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    status_store = ChannelStatusStore()
    channel_row = _channel_row(conn)

    worker = worker_module.Worker(
        channel_row, db_path, get_connection, tmp_path, status_store
    )
    worker.start()
    worker.join(timeout=2)

    recordings = recordings_repo.list_filtered(conn, channel_id=channel_row["id"])
    assert len(recordings) == 1
    assert recordings[0]["status"] == "failed"
    assert recordings[0]["error_message"] == "remux failed"

    status = status_store.get(channel_row["id"])
    assert status["phase"] == "error"
    assert status["detail"] == "remux failed"
