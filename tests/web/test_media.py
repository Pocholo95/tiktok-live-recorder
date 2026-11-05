import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import channels_repo, recordings_repo  # noqa: E402
from db.database import get_connection  # noqa: E402
from utils.enums import Mode  # noqa: E402
import web.routers.media as media_module  # noqa: E402


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


def _raw_recording(app, tmp_path, status="completed"):
    conn = get_connection(app.state.db_path)
    raw = tmp_path / "TK_creator_2026.01.01_00-00-00.ts"
    raw.write_bytes(b"raw transport stream")
    recording_id = recordings_repo.start(
        conn, channel_id=None, username="creator", file_path=str(raw)
    )
    if status == "completed":
        recordings_repo.mark_completed(
            conn, recording_id, file_path=str(raw), format="ts"
        )
    return conn, recording_id, raw


def _fake_probe(codec="h264", format_name="mov,mp4,m4a,3gp,3g2,mj2"):
    return lambda path: {
        "streams": [{"codec_type": "video", "codec_name": codec}],
        "format": {"format_name": format_name, "duration": "61.4"},
    }


def test_upload_converted_replaces_raw_recording(client, app, tmp_path, monkeypatch):
    monkeypatch.setattr(media_module, "_probe", _fake_probe())
    monkeypatch.setattr(media_module, "generate_thumbnail", lambda path: None)
    conn, recording_id, raw = _raw_recording(app, tmp_path)

    response = client.put(f"/recordings/{recording_id}/converted", content=b"mp4 bytes")

    assert response.status_code == 200
    recording = recordings_repo.get(conn, recording_id)
    assert recording["format"] == "mp4"
    assert recording["file_path"].endswith(".mp4")
    assert recording["duration_seconds"] == 61
    assert Path(recording["file_path"]).read_bytes() == b"mp4 bytes"
    assert not raw.exists()


def test_upload_converted_rejects_non_browser_codec(client, app, tmp_path, monkeypatch):
    monkeypatch.setattr(media_module, "_probe", _fake_probe(codec="hevc"))
    conn, recording_id, raw = _raw_recording(app, tmp_path)

    response = client.put(f"/recordings/{recording_id}/converted", content=b"x")

    assert response.status_code == 400
    assert recordings_repo.get(conn, recording_id)["format"] == "ts"
    assert raw.exists()
    assert not list(tmp_path.glob("*.part"))


def test_upload_converted_rejects_unreadable_file(client, app, tmp_path, monkeypatch):
    monkeypatch.setattr(media_module, "_probe", lambda path: None)
    conn, recording_id, raw = _raw_recording(app, tmp_path)

    response = client.put(f"/recordings/{recording_id}/converted", content=b"junk")

    assert response.status_code == 400
    assert raw.exists()


def test_upload_converted_refuses_recording_still_in_progress(client, app, tmp_path):
    _, recording_id, raw = _raw_recording(app, tmp_path, status="recording")

    response = client.put(f"/recordings/{recording_id}/converted", content=b"x")

    assert response.status_code == 409
    assert raw.exists()


def test_upload_converted_404_for_unknown_recording(client):
    response = client.put("/recordings/999/converted", content=b"x")
    assert response.status_code == 404


def test_avatar_served_with_sniffed_type_and_misses_cached(client, app, tmp_path):
    from db import profiles_repo

    conn = get_connection(app.state.db_path)
    avatar = tmp_path / "a.jpg"
    avatar.write_bytes(b"RIFF\x10\x00\x00\x00WEBPVP8 fake")
    profiles_repo.upsert(conn, "creator", nickname="C", avatar_path=str(avatar))

    hit = client.get("/avatars/creator")
    assert hit.status_code == 200
    assert hit.headers["content-type"] == "image/webp"

    miss = client.get("/avatars/nobody")
    assert miss.status_code == 404
    assert "max-age" in miss.headers["cache-control"]
