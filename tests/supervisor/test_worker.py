import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import supervisor.worker as worker_module  # noqa: E402
from db import channels_repo, recordings_repo, settings_repo  # noqa: E402
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
            file_path="/output/TK_creator.ts",
        )
        on_status(
            "recording_finished",
            user=self.config.user,
            file_path="/output/TK_creator.ts",
            format="ts",
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
    assert recordings[0]["format"] == "ts"
    assert recordings[0]["file_path"] == "/output/TK_creator.ts"

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
                file_path="/output/TK_creator.ts",
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


def test_worker_sends_discord_notices_only_when_channel_opted_in(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(worker_module, "TikTokRecorder", _FakeRecorder)
    monkeypatch.setattr(worker_module, "generate_thumbnail", lambda path, **kw: None)
    monkeypatch.setattr(worker_module, "read_cookies", lambda: {})
    monkeypatch.setattr(
        worker_module.discord_webhook,
        "live_started",
        lambda url, user, rid: sent.append(("started", user, url)),
    )
    monkeypatch.setattr(
        worker_module.discord_webhook,
        "live_finished",
        lambda url, user, rid, **kw: sent.append(("finished", user, url)),
    )

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    url = "https://discord.com/api/webhooks/1/abc"
    settings_repo.set(conn, "discord_webhook_url", url)

    for opted_in in (False, True):
        sent.clear()
        row = _channel_row(
            conn, username=f"creator{int(opted_in)}", use_telegram=opted_in
        )
        worker = worker_module.Worker(
            row, db_path, get_connection, tmp_path, ChannelStatusStore()
        )
        worker.start()
        worker.join(timeout=2)
        name = row["username"]
        expected = [("started", name, url), ("finished", name, url)] if opted_in else []
        assert sent == expected


def test_worker_skips_notices_without_webhook(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(worker_module, "TikTokRecorder", _FakeRecorder)
    monkeypatch.setattr(worker_module, "generate_thumbnail", lambda path, **kw: None)
    monkeypatch.setattr(worker_module, "read_cookies", lambda: {})
    monkeypatch.setattr(
        worker_module.discord_webhook, "live_started", lambda *a, **k: sent.append(a)
    )

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    row = _channel_row(conn, use_telegram=True)
    worker = worker_module.Worker(
        row, db_path, get_connection, tmp_path, ChannelStatusStore()
    )
    worker.start()
    worker.join(timeout=2)

    assert sent == []


def test_worker_throttles_profile_saves(tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(worker_module, "read_cookies", lambda: {})
    monkeypatch.setattr(
        worker_module.profiles, "save_profile_in_background", lambda *a: saved.append(a)
    )

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    worker = worker_module.Worker(
        _channel_row(conn), db_path, get_connection, tmp_path, ChannelStatusStore()
    )

    for _ in range(3):
        worker._on_status("profile", user="creator", avatar_url="u", nickname="n")

    assert len(saved) == 1
    assert saved[0][3:] == ("creator", "u", "n")


def test_worker_stores_participants_for_current_recording(tmp_path, monkeypatch):
    from db import participants_repo

    class _GuestRecorder(_FakeRecorder):
        def run(self):
            on_status = self.config.on_status
            on_status("recording_started", user="creator", file_path="/output/a.ts")
            on_status(
                "participants",
                user="creator",
                participants=[
                    {"username": "guest", "start_seconds": 12.5, "end_seconds": None}
                ],
            )
            on_status(
                "recording_finished",
                user="creator",
                file_path="/output/a.ts",
                format="ts",
            )

    monkeypatch.setattr(worker_module, "TikTokRecorder", _GuestRecorder)
    monkeypatch.setattr(worker_module, "generate_thumbnail", lambda path, **kw: None)
    monkeypatch.setattr(worker_module, "read_cookies", lambda: {})

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    row = _channel_row(conn)
    worker = worker_module.Worker(
        row, db_path, get_connection, tmp_path, ChannelStatusStore()
    )
    worker.start()
    worker.join(timeout=2)

    recording = recordings_repo.list_filtered(conn, channel_id=row["id"])[0]
    rows = participants_repo.list_for_recording(conn, recording["id"])
    assert [(r["username"], r["start_seconds"], r["end_seconds"]) for r in rows] == [
        ("guest", 12.5, None)
    ]
