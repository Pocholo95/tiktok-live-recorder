import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import channels_repo, recordings_repo  # noqa: E402
from db.database import get_connection  # noqa: E402
from utils.enums import Mode  # noqa: E402


def test_media_returns_404_for_unknown_recording(client):
    response = client.get("/media/999")
    assert response.status_code == 404


def test_media_serves_file_with_range_support(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    video_path = Path(app.state.output_dir) / "a.mp4"
    video_path.write_bytes(b"0123456789" * 100)

    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path=str(video_path)
    )
    recordings_repo.mark_completed(
        conn, recording_id, file_path=str(video_path), format="mp4"
    )

    response = client.get(f"/media/{recording_id}", headers={"Range": "bytes=0-9"})
    assert response.status_code == 206
    assert response.content == b"0123456789"


def test_media_404s_when_db_row_exists_but_file_is_gone(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn,
        channel_id=channel_id,
        username="creator",
        file_path="/nonexistent/a.mp4",
    )

    response = client.get(f"/media/{recording_id}")
    assert response.status_code == 404


def test_download_media_forces_attachment_with_filename(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    video_path = Path(app.state.output_dir) / "TK_creator_2026.01.01_00-00-00.mp4"
    video_path.write_bytes(b"fake video bytes")

    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path=str(video_path)
    )
    recordings_repo.mark_completed(
        conn, recording_id, file_path=str(video_path), format="mp4"
    )

    response = client.get(f"/media/{recording_id}/download")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert video_path.name in response.headers["content-disposition"]


def test_download_media_returns_404_for_unknown_recording(client):
    response = client.get("/media/999/download")
    assert response.status_code == 404


class _FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, size):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        pass


class _FakeProcess:
    def __init__(self, chunks):
        self.stdout = _FakeStdout(chunks)

    def wait(self):
        pass


def test_download_clip_streams_ffmpeg_output_without_touching_disk(
    client, app, monkeypatch
):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    video_path = Path(app.state.output_dir) / "a.mp4"
    video_path.write_bytes(b"fake source video")
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path=str(video_path)
    )
    recordings_repo.mark_completed(
        conn, recording_id, file_path=str(video_path), format="mp4"
    )

    fake_chunks = [b"clip-bytes-part1", b"clip-bytes-part2"]
    captured_cmd = {}

    def fake_popen(cmd, stdout=None, stderr=None):
        captured_cmd["cmd"] = cmd
        return _FakeProcess(fake_chunks)

    monkeypatch.setattr("web.routers.media.subprocess.Popen", fake_popen)

    response = client.get(f"/media/{recording_id}/clip?start=1&end=5")

    assert response.status_code == 200
    assert response.content == b"".join(fake_chunks)
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-type"] == "video/mp4"
    # -c copy + fragmented mp4 is what makes writing to a pipe possible at all
    assert "copy" in captured_cmd["cmd"]
    assert "frag_keyframe+empty_moov" in captured_cmd["cmd"]


def test_download_clip_rejects_invalid_range(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )

    response = client.get(f"/media/{recording_id}/clip?start=5&end=1")
    assert response.status_code == 400


def test_download_clip_404_for_unknown_recording(client):
    response = client.get("/media/999/clip?start=0&end=5")
    assert response.status_code == 404


def test_thumbnail_falls_back_to_placeholder_when_none_generated(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a_flv.flv"
    )

    response = client.get(f"/media/{recording_id}/thumbnail")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
