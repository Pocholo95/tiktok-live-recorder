import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import supervisor.alerts as alerts  # noqa: E402
import supervisor.health as health  # noqa: E402
from db import channels_repo, settings_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from supervisor.status import ChannelStatusStore  # noqa: E402
from utils.enums import Mode  # noqa: E402

WEBHOOK = "https://discord.com/api/webhooks/1/abc"


def _db(tmp_path, webhook=True):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    if webhook:
        settings_repo.set(conn, "discord_webhook_url", WEBHOOK)
    return db_path, conn


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(
        alerts.discord_webhook,
        "send_in_background",
        lambda url, embed: sent.append(embed["title"]),
    )
    return sent


def test_status_store_keeps_since_and_flags_while_phase_unchanged(monkeypatch):
    import supervisor.status as status_module

    now = [100.0]
    monkeypatch.setattr(status_module.time, "time", lambda: now[0])
    store = ChannelStatusStore()

    store.set(1, phase="error", detail="a")
    store.set_flag(1, "stalled_since", 5)
    now[0] = 200
    store.set(1, phase="error", detail="b")
    assert store.get(1)["since"] == 100
    assert store.get(1)["flags"] == {"stalled_since": 5}

    now[0] = 300
    store.set(1, phase="recording")
    assert store.get(1)["since"] == 300
    assert store.get(1)["flags"] == {}


def test_alerts_need_webhook_and_respect_toggle_and_throttle(tmp_path, monkeypatch):
    sent = _capture(monkeypatch)
    _, conn = _db(tmp_path, webhook=False)
    assert alerts.send(conn, "k", "t", "d", 60, now=1000) is False

    settings_repo.set(conn, "discord_webhook_url", WEBHOOK)
    assert alerts.send(conn, "k", "t", "d", 60, now=1000) is True
    assert alerts.send(conn, "k", "t", "d", 60, now=1030) is False  # throttled
    assert alerts.send(conn, "other", "t2", "d", 60, now=1030) is True
    assert alerts.send(conn, "k", "t", "d", 60, now=1061) is True

    settings_repo.set(conn, alerts.ENABLED_KEY, "0")
    assert alerts.send(conn, "new", "t", "d", 60, now=5000) is False
    assert sent == ["⚠️ t", "⚠️ t2", "⚠️ t"]


def test_session_problem_is_remembered_and_cleared(tmp_path):
    _, conn = _db(tmp_path)
    assert alerts.last_session_problem(conn) is None
    alerts.record_session_problem(conn, "creator", now=1234.5)
    assert alerts.last_session_problem(conn) == (1234.5, "creator")
    alerts.clear_session_problem(conn)
    assert alerts.last_session_problem(conn) is None


def _monitor(db_path, store, now, sizes):
    def size_of(path):
        if path not in sizes:
            raise OSError("gone")
        return sizes[path]

    return health.HealthMonitor(
        db_path, get_connection, store, clock=lambda: now[0], size_of=size_of
    )


def test_channel_stuck_in_error_alerts_once_per_episode(tmp_path, monkeypatch):
    import supervisor.status as status_module

    sent = _capture(monkeypatch)
    db_path, conn = _db(tmp_path)
    cid = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    store = ChannelStatusStore()
    now = [0.0]
    monkeypatch.setattr(status_module.time, "time", lambda: now[0])
    store.set(cid, phase="error", detail="boom")
    monitor = _monitor(db_path, store, now, {})

    now[0] = 29 * 60
    monitor.check_once()
    assert sent == []
    now[0] = 31 * 60
    monitor.check_once()
    now[0] = 90 * 60
    monitor.check_once()
    assert sent == ["⚠️ @creator lleva 31 min con error"]


def test_stalled_recording_flags_and_alerts(tmp_path, monkeypatch):
    sent = _capture(monkeypatch)
    db_path, conn = _db(tmp_path)
    cid = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    store = ChannelStatusStore()
    store.set(cid, phase="recording", detail="/out/a.ts")
    now = [0.0]
    sizes = {"/out/a.ts": 100}
    monitor = _monitor(db_path, store, now, sizes)

    monitor.check_once()  # first sight
    now[0] = 60
    sizes["/out/a.ts"] = 200  # growing
    monitor.check_once()
    now[0] = 60 + 4 * 60
    monitor.check_once()
    assert sent == [] and "stalled_since" not in store.get(cid)["flags"]

    now[0] = 60 + 6 * 60  # 6 min without growth
    monitor.check_once()
    assert store.get(cid)["flags"]["stalled_since"] == 60
    assert sent == ["⚠️ La grabación de @creator dejó de crecer"]

    sizes["/out/a.ts"] = 300  # recovers
    now[0] += 60
    monitor.check_once()
    assert "stalled_since" not in store.get(cid)["flags"]


def test_tracker_reports_denied_access_once():
    from core.participants import ParticipantTracker

    calls = []

    class _Api:
        http_client = None
        last_room_status_code = 4003110

        def get_room_data(self, room_id):
            return None

    tracker = ParticipantTracker(
        _Api(), "room", lambda s: None, on_access_denied=lambda: calls.append(1)
    )
    tracker.poll_once()
    tracker.poll_once()
    assert calls == [1]


def test_worker_session_problem_alerts(tmp_path, monkeypatch):
    import supervisor.worker as worker_module

    sent = _capture(monkeypatch)
    monkeypatch.setattr(worker_module, "read_cookies", lambda: {})
    db_path, conn = _db(tmp_path)
    row = channels_repo.get(
        conn, channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    )
    worker = worker_module.Worker(
        row, db_path, get_connection, tmp_path, ChannelStatusStore()
    )

    worker._on_status("session_problem", user="creator", has_session=True)
    worker._on_status("session_problem", user="creator", has_session=True)
    assert sent == ["⚠️ Tu sesión de TikTok no está funcionando"]
    assert alerts.last_session_problem(conn)[1] == "creator"

    worker._on_status("session_problem", user="creator", has_session=False)
    assert sent[-1] == "⚠️ Live de @creator con restricción de edad"
