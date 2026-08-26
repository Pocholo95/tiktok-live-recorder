import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import channels_repo, recordings_repo  # noqa: E402
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


def test_settings_page_loads(client):
    response = client.get("/settings")
    assert response.status_code == 200
