import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import supervisor.profiles as profiles  # noqa: E402
from db import profiles_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402

WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 fake"


class _Response:
    def __init__(self, content, content_type="image/webp", status=200):
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _db(tmp_path):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    return db_path


def test_save_profile_downloads_avatar_and_nickname(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles.requests, "get", lambda *a, **k: _Response(WEBP))
    db_path = _db(tmp_path)

    profiles.save_profile(
        db_path, get_connection, tmp_path, "some.user", "https://cdn/x", "Some User"
    )

    profile = profiles_repo.get(get_connection(db_path), "some.user")
    assert profile["nickname"] == "Some User"
    assert Path(profile["avatar_path"]).read_bytes() == WEBP
    assert Path(profile["avatar_path"]).parent == tmp_path / "avatars"


def test_failed_download_keeps_previous_avatar(tmp_path, monkeypatch):
    db_path = _db(tmp_path)
    monkeypatch.setattr(profiles.requests, "get", lambda *a, **k: _Response(WEBP))
    profiles.save_profile(db_path, get_connection, tmp_path, "u", "https://cdn/x", "U")

    monkeypatch.setattr(
        profiles.requests, "get", lambda *a, **k: _Response(b"<html>", "text/html")
    )
    profiles.save_profile(db_path, get_connection, tmp_path, "u", "https://cdn/y", None)

    profile = profiles_repo.get(get_connection(db_path), "u")
    assert profile["nickname"] == "U"
    assert Path(profile["avatar_path"]).read_bytes() == WEBP


def test_avatar_filename_is_sanitized(tmp_path):
    path = profiles.avatar_path_for(tmp_path, "../../etc/passwd")
    assert path.parent == tmp_path / "avatars"
