import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import supervisor.guest_profiles as guest_profiles  # noqa: E402
import supervisor.profiles as profiles  # noqa: E402
from db import profiles_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402


def _write(dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x")


class _Api:
    def __init__(self, users):
        self.users = users
        self.asked = []

    def _unsigned_user_room(self, username):
        self.asked.append(username)
        return {"user": self.users.get(username, {})}


def _fetcher(tmp_path, api):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    fetcher = guest_profiles.GuestProfiles(
        db_path, get_connection, tmp_path, lambda: api, pause=0
    )
    fetcher._start = lambda: None  # run synchronously in tests
    return fetcher, db_path


def _drain(fetcher):
    while not fetcher._queue.empty():
        fetcher.fetch(fetcher._queue.get())


def test_fetches_guest_avatar_once(tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(
        profiles, "_download", lambda url, dest: saved.append(url) or _write(dest)
    )
    api = _Api({"guest1": {"avatarThumb": "https://cdn/a", "nickname": "Guest"}})
    fetcher, db_path = _fetcher(tmp_path, api)

    fetcher.want(["guest1", "@guest1", ""])
    _drain(fetcher)
    fetcher.want(["guest1"])
    _drain(fetcher)

    assert api.asked == ["guest1"]
    assert saved == ["https://cdn/a"]
    profile = profiles_repo.get(get_connection(db_path), "guest1")
    assert profile["nickname"] == "Guest" and profile["avatar_path"]


def test_skips_fresh_profiles_and_backfills_known_guests(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_download", lambda url, dest: _write(dest))
    api = _Api({"old": {"avatarThumb": "https://cdn/o"}})
    fetcher, db_path = _fetcher(tmp_path, api)
    conn = get_connection(db_path)
    profiles_repo.upsert(conn, "fresh", avatar_path="/x.jpg")
    conn.execute(
        "INSERT INTO recordings (id, username, file_path, status) "
        "VALUES (1, 'host', '/r.ts', 'completed')"
    )
    conn.executemany(
        "INSERT INTO recording_participants (recording_id, username, start_seconds) "
        "VALUES (1, ?, 0)",
        [("old",), ("fresh",), ("",)],
    )
    conn.commit()
    conn.close()

    fetcher.want_all_known_guests()
    _drain(fetcher)

    assert api.asked == ["old"]


def test_unknown_user_saves_nothing(tmp_path):
    fetcher, db_path = _fetcher(tmp_path, _Api({}))
    assert fetcher.fetch("nobody") is False
    assert profiles_repo.get(get_connection(db_path), "nobody") is None
