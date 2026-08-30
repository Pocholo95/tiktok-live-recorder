import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import channels_repo, clip_marks_repo, recordings_repo  # noqa: E402
from db.database import get_connection  # noqa: E402
from utils.enums import Mode  # noqa: E402


def test_dashboard_renders_empty_state(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Todavía no agregaste ningún canal." in response.text


def test_create_channel_adds_row_and_strips_leading_at(client, app):
    response = client.post(
        "/channels",
        data={"username": "@creator", "mode": "automatic", "automatic_interval": "5"},
    )
    assert response.status_code == 200
    assert "@creator" in response.text

    conn = get_connection(app.state.db_path)
    rows = channels_repo.list_all(conn)
    assert len(rows) == 1
    assert rows[0]["username"] == "creator"


def test_toggle_channel_flips_enabled(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    response = client.post(f"/channels/{channel_id}/toggle")
    assert response.status_code == 200

    row = channels_repo.get(conn, channel_id)
    assert row["enabled"] == 0


def test_delete_channel_removes_row(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    response = client.delete(f"/channels/{channel_id}")
    assert response.status_code == 200
    assert response.text == ""
    assert channels_repo.get(conn, channel_id) is None


def test_recordings_partial_filters_by_channel(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a_flv.flv"
    )

    response = client.get("/library")
    assert response.status_code == 200
    assert "@creator" in response.text

    response = client.get(f"/partials/recordings?channel_id={channel_id}")
    assert "creator" in response.text

    response = client.get(f"/partials/recordings?channel_id={channel_id + 999}")
    assert "No hay grabaciones" in response.text


def test_delete_recording_removes_row_and_files(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    video_path = Path(app.state.output_dir) / "a.mp4"
    video_path.write_bytes(b"fake video")
    thumb_path = Path(app.state.output_dir) / "a.jpg"
    thumb_path.write_bytes(b"fake thumb")

    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path=str(video_path)
    )
    recordings_repo.mark_completed(
        conn,
        recording_id,
        file_path=str(video_path),
        format="mp4",
        thumbnail_path=str(thumb_path),
    )

    response = client.delete(f"/recordings/{recording_id}")

    assert response.status_code == 200
    assert response.text == ""
    assert recordings_repo.get(conn, recording_id) is None
    assert not video_path.exists()
    assert not thumb_path.exists()


def test_delete_recording_is_a_noop_for_unknown_id(client):
    response = client.delete("/recordings/999")
    assert response.status_code == 200


def test_settings_page_loads(client):
    response = client.get("/settings")
    assert response.status_code == 200


def test_clip_editor_page_renders(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )
    recordings_repo.mark_completed(
        conn, recording_id, file_path="/output/a.mp4", format="mp4"
    )

    response = client.get(f"/recordings/{recording_id}/clip")

    assert response.status_code == 200
    assert "creator" in response.text


def test_clip_editor_404_for_unknown_recording(client):
    response = client.get("/recordings/999/clip")
    assert response.status_code == 404


def test_create_and_delete_clip_mark(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )

    response = client.post(
        f"/recordings/{recording_id}/clip-marks",
        data={"start": "1.5", "end": "10.0", "label": "highlight"},
    )

    assert response.status_code == 200
    assert "highlight" in response.text

    marks = clip_marks_repo.list_for_recording(conn, recording_id)
    assert len(marks) == 1
    mark_id = marks[0]["id"]

    response = client.delete(f"/clip-marks/{mark_id}")
    assert response.status_code == 200
    assert clip_marks_repo.get(conn, mark_id) is None


def test_create_clip_mark_rejects_invalid_range(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )

    client.post(
        f"/recordings/{recording_id}/clip-marks",
        data={"start": "10", "end": "5", "label": "bad"},
    )

    assert clip_marks_repo.list_for_recording(conn, recording_id) == []
