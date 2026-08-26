import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import supervisor.supervisor as supervisor_module  # noqa: E402
from db import channels_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from utils.enums import Mode  # noqa: E402


class _FakeWorker:
    instances = []

    def __init__(self, channel_row, db_path, get_connection, output_dir, status_store):
        self.channel_row = channel_row
        self.started = False
        self.stopped = False
        self._alive = True
        _FakeWorker.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self._alive = False

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return self._alive


def test_reconcile_starts_worker_for_enabled_channel_only(tmp_path, monkeypatch):
    _FakeWorker.instances = []
    monkeypatch.setattr(supervisor_module, "_Worker", _FakeWorker)

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)

    enabled_id = channels_repo.insert(conn, username="alice", mode=Mode.AUTOMATIC)
    channels_repo.insert(conn, username="bob", mode=Mode.AUTOMATIC, enabled=False)

    sup = supervisor_module.ChannelSupervisor(db_path, tmp_path)
    sup._reconcile_once()

    assert set(sup._workers.keys()) == {enabled_id}
    assert _FakeWorker.instances[0].started is True


def test_reconcile_stops_worker_when_channel_is_disabled(tmp_path, monkeypatch):
    _FakeWorker.instances = []
    monkeypatch.setattr(supervisor_module, "_Worker", _FakeWorker)

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    channel_id = channels_repo.insert(conn, username="alice", mode=Mode.AUTOMATIC)

    sup = supervisor_module.ChannelSupervisor(db_path, tmp_path)
    sup._reconcile_once()
    assert sup._workers

    channels_repo.set_enabled(conn, channel_id, False)
    sup._reconcile_once()

    assert sup._workers == {}
    assert _FakeWorker.instances[0].stopped is True


def test_reconcile_restarts_worker_when_channel_config_changes(tmp_path, monkeypatch):
    _FakeWorker.instances = []
    monkeypatch.setattr(supervisor_module, "_Worker", _FakeWorker)

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    channel_id = channels_repo.insert(
        conn, username="alice", mode=Mode.AUTOMATIC, automatic_interval=5
    )

    sup = supervisor_module.ChannelSupervisor(db_path, tmp_path)
    sup._reconcile_once()
    first_worker = _FakeWorker.instances[0]

    channels_repo.update(conn, channel_id, automatic_interval=10)
    sup._reconcile_once()

    assert first_worker.stopped is True
    assert len(_FakeWorker.instances) == 2
    assert _FakeWorker.instances[1].started is True
    assert sup._workers[channel_id][0] is _FakeWorker.instances[1]


def test_reconcile_prunes_workers_that_died_on_their_own(tmp_path, monkeypatch):
    _FakeWorker.instances = []
    monkeypatch.setattr(supervisor_module, "_Worker", _FakeWorker)

    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    conn = get_connection(db_path)
    channels_repo.insert(conn, username="alice", mode=Mode.AUTOMATIC)

    sup = supervisor_module.ChannelSupervisor(db_path, tmp_path)
    sup._reconcile_once()

    _FakeWorker.instances[0]._alive = False
    sup._reconcile_once()

    # channel is still enabled, so a fresh worker should replace the dead one
    assert len(_FakeWorker.instances) == 2
    assert _FakeWorker.instances[1].started is True
