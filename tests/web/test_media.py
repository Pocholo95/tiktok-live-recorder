import json
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
    # The raw file is kept for a few days in case the conversion is broken.
    assert raw.exists() and recording["original_path"] == str(raw)
    assert recording["original_kept_at"]

    original = client.get(f"/media/{recording_id}/original")
    assert original.status_code == 200 and original.content == b"raw transport stream"
    assert "attachment" in original.headers["content-disposition"]

    page = client.get(f"/library?recording_id={recording_id}")
    assert "Descargar original (.ts)" in page.text

    # Deleting the recording deletes the kept original too.
    assert client.delete(f"/recordings/{recording_id}").status_code == 200
    assert not raw.exists()


def test_upload_converted_deletes_raw_when_keeping_is_off(
    client, app, tmp_path, monkeypatch
):
    from db import settings_repo
    from supervisor import originals

    monkeypatch.setattr(media_module, "_probe", _fake_probe())
    monkeypatch.setattr(media_module, "generate_thumbnail", lambda path: None)
    conn, recording_id, raw = _raw_recording(app, tmp_path)
    settings_repo.set(conn, originals.KEEP_DAYS_KEY, "0")

    response = client.put(f"/recordings/{recording_id}/converted", content=b"mp4 bytes")

    assert response.status_code == 200
    assert not raw.exists()
    assert recordings_repo.get(conn, recording_id)["original_path"] is None
    assert client.get(f"/media/{recording_id}/original").status_code == 404


def test_stream_stats_download_and_warning(client, app, tmp_path):
    conn, recording_id, raw = _raw_recording(app, tmp_path)
    stats = raw.with_suffix(".stats.json")
    stats.write_text(
        json.dumps(
            {
                "media_seconds": 600,
                "audio": {"gaps": [[100, 400]]},
                "video": {"gaps": []},
                "connections": [{"at": 0}, {"at": 300}],
            }
        ),
        encoding="utf-8",
    )

    assert client.get(f"/recordings/{recording_id}/stats").status_code == 200
    page = client.get(f"/library?recording_id={recording_id}")
    assert "sin audio al grabar" in page.text and "0:05:00" in page.text

    client.delete(f"/recordings/{recording_id}")
    assert not stats.exists()


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


def test_participants_debug_download_and_cleanup(client, app, tmp_path):
    conn = get_connection(app.state.db_path)
    raw = tmp_path / "TK_creator_2026.01.01_00-00-00.ts"
    raw.write_bytes(b"ts")
    debug = tmp_path / "TK_creator_2026.01.01_00-00-00.participants.jsonl"
    debug.write_bytes(b'{"ids": []}' + bytes([10]))
    recording_id = recordings_repo.start(
        conn, channel_id=None, username="creator", file_path=str(raw)
    )
    recordings_repo.mark_completed(conn, recording_id, file_path=str(raw), format="ts")

    response = client.get(f"/recordings/{recording_id}/participants-debug")
    assert response.status_code == 200
    assert response.text == '{"ids": []}\n'
    assert "participants.jsonl" in response.headers["content-disposition"]

    client.delete(f"/recordings/{recording_id}")
    assert not debug.exists()
    assert (
        client.get(f"/recordings/{recording_id}/participants-debug").status_code == 404
    )


def test_storyboard_endpoints(client, app, tmp_path):
    from supervisor import storyboards

    video = tmp_path / "TK_sb.mp4"
    video.write_bytes(b"x")
    folder = storyboards.storyboard_dir(video)
    folder.mkdir()
    meta = {"interval": 2, "cols": 10, "rows": 10, "tile_width": 90,
            "tile_height": 160, "count": 15, "sheets": 1}  # fmt: skip
    (folder / "meta.json").write_text(json.dumps(meta))
    (folder / "sheet_001.jpg").write_bytes(b"jpg")
    conn = get_connection(app.state.db_path)
    rid = recordings_repo.start(
        conn, channel_id=None, username="c", file_path=str(video)
    )
    recordings_repo.mark_completed(conn, rid, file_path=str(video), format="mp4")

    assert client.get(f"/media/{rid}/storyboard").json() == meta
    sheet = client.get(f"/media/{rid}/storyboard/1.jpg")
    assert sheet.status_code == 200 and sheet.content == b"jpg"
    assert client.get(f"/media/{rid}/storyboard/2.jpg").status_code == 404
    assert client.get("/media/999/storyboard").status_code == 404

    # Deleting the recording removes its storyboard too.
    client.delete(f"/recordings/{rid}")
    assert not folder.exists()


def test_storyboard_upload_installs_validated_sheets(
    client, app, tmp_path, monkeypatch
):
    import io
    import zipfile

    from supervisor import storyboards

    calls = []

    def fake_install(video_path, zip_path, interval, duration, ffprobe_path):
        calls.append((Path(video_path).name, zipfile.is_zipfile(zip_path), interval))
        return {"sheets": 2}, None

    monkeypatch.setattr(storyboards, "install_uploaded", fake_install)
    video = tmp_path / "TK_up.mp4"
    video.write_bytes(b"x")
    conn = get_connection(app.state.db_path)
    rid = recordings_repo.start(
        conn, channel_id=None, username="c", file_path=str(video)
    )
    recordings_repo.mark_completed(
        conn, rid, file_path=str(video), format="mp4", duration_seconds=300
    )
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("sheet_001.jpg", b"\xff\xd8\xff")

    response = client.put(
        f"/recordings/{rid}/storyboard?interval=2", content=body.getvalue()
    )

    assert response.status_code == 200 and response.json()["sheets"] == 2
    assert calls == [("TK_up.mp4", True, 2)]
    assert not list(tmp_path.glob("*.part"))  # the temp zip is cleaned up


def test_storyboard_upload_only_for_converted_recordings(client, app, tmp_path):
    conn, recording_id, raw = _raw_recording(app, tmp_path)
    response = client.put(
        f"/recordings/{recording_id}/storyboard?interval=2", content=b"z"
    )
    assert response.status_code == 409
    assert (
        client.put("/recordings/999/storyboard?interval=2", content=b"z").status_code
        == 404
    )


def test_storyboard_upload_reports_invalid_zip(client, app, tmp_path):
    video = tmp_path / "TK_bad.mp4"
    video.write_bytes(b"x")
    conn = get_connection(app.state.db_path)
    rid = recordings_repo.start(
        conn, channel_id=None, username="c", file_path=str(video)
    )
    recordings_repo.mark_completed(conn, rid, file_path=str(video), format="mp4")
    response = client.put(f"/recordings/{rid}/storyboard?interval=2", content=b"nope")
    assert response.status_code == 400


def _probe_by_suffix(raw_seconds, mp4_seconds):
    def probe(path):
        seconds = raw_seconds if str(path).endswith(".ts") else mp4_seconds
        if seconds is None:
            return None
        return {
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
            "format": {"format_name": "mov,mp4,m4a", "duration": str(seconds)},
        }

    return probe


def test_upload_of_another_live_is_refused_unless_forced(
    client, app, tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "_probe", _probe_by_suffix(7200, 3600))
    monkeypatch.setattr(media_module, "generate_thumbnail", lambda path: None)
    conn, recording_id, raw = _raw_recording(app, tmp_path)

    response = client.put(
        f"/recordings/{recording_id}/converted", content=b"other live"
    )

    assert response.status_code == 409
    assert (
        "1:00:00" in response.json()["detail"]
        and "2:00:00" in response.json()["detail"]
    )
    recording = recordings_repo.get(conn, recording_id)
    assert recording["format"] == "ts" and raw.read_bytes() == b"raw transport stream"
    assert not list(tmp_path.glob("*.mp4*"))  # nothing left behind

    forced = client.put(
        f"/recordings/{recording_id}/converted?force=1", content=b"other live"
    )
    assert forced.status_code == 200
    assert recordings_repo.get(conn, recording_id)["format"] == "mp4"


def test_upload_tolerates_padding_and_unknown_raw_duration(
    client, app, tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "generate_thumbnail", lambda path: None)
    # Recording 42: the MP4 came out 33 s (3 %) longer than its raw.
    monkeypatch.setattr(media_module, "_probe", _probe_by_suffix(1077, 1110))
    conn, recording_id, _raw = _raw_recording(app, tmp_path)
    assert (
        client.put(f"/recordings/{recording_id}/converted", content=b"x").status_code
        == 200
    )

    # A raw ffprobe can't read gives no opinion: the upload isn't blocked.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(media_module, "_probe", _probe_by_suffix(None, 1110))
    conn, recording_id, _raw = _raw_recording(app, other)
    assert (
        client.put(f"/recordings/{recording_id}/converted", content=b"x").status_code
        == 200
    )


def test_duration_mismatch_thresholds():
    assert not media_module.duration_mismatch(1077, 1110)  # +3 %
    assert not media_module.duration_mismatch(600, 655)  # within 60 s
    assert media_module.duration_mismatch(600, 700)
    assert media_module.duration_mismatch(7200, 7200 * 1.06)
    assert not media_module.duration_mismatch(None, 100)
