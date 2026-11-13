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


def test_library_filters_by_channel(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a_flv.flv"
    )

    response = client.get("/library")
    assert response.status_code == 200
    assert "@creator" in response.text

    response = client.get(f"/library?channel_id={channel_id}")
    assert "creator" in response.text

    response = client.get(f"/library?channel_id={channel_id + 999}")
    assert "No hay grabaciones" in response.text


def test_library_filters_recordings_without_clips(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    with_clip = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )
    recordings_repo.mark_completed(
        conn, with_clip, file_path="/output/a.mp4", format="mp4"
    )
    clip_marks_repo.insert(
        conn, recording_id=with_clip, start_seconds=1.0, end_seconds=2.0, label=None
    )

    without_clip = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/b.mp4"
    )
    recordings_repo.mark_completed(
        conn, without_clip, file_path="/output/b.mp4", format="mp4"
    )

    response = client.get("/library?no_clips=1")

    assert response.status_code == 200
    assert f"recording_id={without_clip}" in response.text
    assert f"recording_id={with_clip}" not in response.text


def test_library_treats_empty_query_params_as_unset(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a_flv.flv"
    )

    # A <select> with an empty "Todos los canales" option (or a hand-edited
    # URL) submits channel_id= / recording_id= as empty strings, not an
    # absent param - these must not 422.
    response = client.get("/library?channel_id=&recording_id=")

    assert response.status_code == 200
    assert "@creator" in response.text


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


def test_library_shows_selected_recording(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )
    recordings_repo.mark_completed(
        conn, recording_id, file_path="/output/a.mp4", format="mp4"
    )

    response = client.get(f"/library?recording_id={recording_id}")

    assert response.status_code == 200
    assert "creator" in response.text
    assert "data-delete-recording" in response.text


def test_library_htmx_request_returns_partial_without_page_chrome(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )
    recordings_repo.mark_completed(
        conn, recording_id, file_path="/output/a.mp4", format="mp4"
    )

    response = client.get(
        f"/library?recording_id={recording_id}", headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    assert "data-delete-recording" in response.text
    # A partial swap must not nest the page chrome inside itself.
    assert "<html" not in response.text
    assert 'class="nav"' not in response.text
    assert 'id="library-content"' in response.text


def test_library_recording_click_only_swaps_the_player_panel(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )
    recordings_repo.mark_completed(
        conn, recording_id, file_path="/output/a.mp4", format="mp4"
    )

    response = client.get(
        f"/library?recording_id={recording_id}",
        headers={"HX-Request": "true", "HX-Target": "library-main"},
    )

    assert response.status_code == 200
    assert "data-delete-recording" in response.text
    assert 'id="library-main"' in response.text
    # Selecting a recording must not re-render the sidebar - that would
    # reset its scroll position on every click.
    assert 'class="library-sidebar"' not in response.text
    assert "rec-item" not in response.text


def test_library_defaults_to_most_recent_recording_when_none_selected(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )
    recordings_repo.mark_completed(
        conn, recording_id, file_path="/output/a.mp4", format="mp4"
    )

    response = client.get("/library")

    assert response.status_code == 200
    assert "data-delete-recording" in response.text


def test_old_clip_route_redirects_to_library(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )

    response = client.get(f"/recordings/{recording_id}/clip", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == f"/library?recording_id={recording_id}"


def test_create_and_delete_clip_mark(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/output/a.mp4"
    )

    response = client.post(
        f"/recordings/{recording_id}/clip-marks",
        data={"start": "2.0", "end": "10.0", "label": "highlight"},
    )

    assert response.status_code == 200
    assert "highlight" in response.text
    assert "00:00:02" in response.text  # start rendered as HH:MM:SS, not raw seconds
    assert "00:00:10" in response.text

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

    response = client.post(
        f"/recordings/{recording_id}/clip-marks",
        data={"start": "10", "end": "5", "label": "bad"},
    )

    assert clip_marks_repo.list_for_recording(conn, recording_id) == []
    assert "No se guardó" in response.text  # failure is visible, not silent


def _completed(conn, channel_id, file_path, format):
    recording_id = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path=file_path
    )
    recordings_repo.mark_completed(
        conn, recording_id, file_path=file_path, format=format
    )
    return recording_id


def test_library_state_tabs_filter_raw_and_ready(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    raw_id = _completed(conn, channel_id, "/output/raw.ts", "ts")
    ready_id = _completed(conn, channel_id, "/output/ready.mp4", "mp4")

    raw = client.get("/library?state=raw").text
    assert f'data-recording-id="{raw_id}"' in raw
    assert f'data-recording-id="{ready_id}"' not in raw

    ready = client.get("/library?state=ready").text
    assert f'data-recording-id="{ready_id}"' in ready
    assert f'data-recording-id="{raw_id}"' not in ready

    # Unknown states fall back to showing everything.
    everything = client.get("/library?state=bogus").text
    assert f'data-recording-id="{raw_id}"' in everything
    assert f'data-recording-id="{ready_id}"' in everything


def test_library_raw_recording_shows_conversion_workflow(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    raw_id = _completed(conn, channel_id, "/output/TK_creator_x.ts", "ts")

    text = client.get(f"/library?recording_id={raw_id}").text

    assert 'id="convert-workflow"' in text
    assert 'data-raw-name="TK_creator_x.ts"' in text
    assert 'id="clip-video"' not in text


def test_library_mp4_recording_shows_clip_editor(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    ready_id = _completed(conn, channel_id, "/output/a.mp4", "mp4")

    text = client.get(f"/library?recording_id={ready_id}").text

    assert 'id="clip-video"' in text
    assert 'id="convert-workflow"' not in text


def test_creators_index_lists_creators_with_stats(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    channels_repo.insert(conn, username="newbie", mode=Mode.AUTOMATIC)
    rid = _completed(conn, channel_id, "/output/a.ts", "ts")
    conn.execute("UPDATE recordings SET duration_seconds = 7200 WHERE id = ?", (rid,))
    conn.commit()

    text = client.get("/creators").text

    assert 'href="/creators/creator"' in text
    assert "2,0 h" in text
    assert "1 por convertir" in text
    # Tracked channels without recordings still get a card.
    assert 'href="/creators/newbie"' in text


def test_creator_detail_shows_recordings(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    rid = _completed(conn, channel_id, "/output/a.mp4", "mp4")

    response = client.get("/creators/@creator")

    assert response.status_code == 200
    assert f"recording_id={rid}" in response.text
    assert "https://www.tiktok.com/@creator" in response.text


def test_creator_detail_404_for_unknown(client):
    assert client.get("/creators/nobody").status_code == 404


def test_toggle_channel_notify(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    response = client.post(f"/channels/{channel_id}/notify")

    assert response.status_code == 200
    assert channels_repo.get(conn, channel_id)["use_telegram"] == 1
    assert "bell on" in response.text


def test_library_shows_guests_in_sidebar_panel_and_player(client, app):
    from db import participants_repo

    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    raw_id = _completed(conn, channel_id, "/output/raw.ts", "ts")
    ready_id = _completed(conn, channel_id, "/output/ready.mp4", "mp4")
    guests = [{"username": "os_grce", "start_seconds": 75, "end_seconds": 300}]
    participants_repo.replace_for_recording(conn, raw_id, guests)
    participants_repo.replace_for_recording(conn, ready_id, guests)

    raw = client.get(f"/library?recording_id={raw_id}").text
    assert "con @os_grce" in raw  # sidebar
    assert "00:01:15 → 00:05:00" in raw  # guests panel
    assert "drawtext" not in raw  # names are never burned into the video
    assert 'id="on-screen"' not in raw  # no player for raw recordings

    ready = client.get(f"/library?recording_id={ready_id}").text
    assert 'id="on-screen"' in ready  # synced "En pantalla" bar
    assert '"username": "os_grce"' in ready


def test_update_channel_interval(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    response = client.post(f"/channels/{channel_id}/interval", data={"minutes": "12"})
    assert response.status_code == 200
    assert 'value="12"' in response.text
    assert channels_repo.get(conn, channel_id)["automatic_interval"] == 12

    # Invalid values are ignored, keeping the previous one.
    client.post(f"/channels/{channel_id}/interval", data={"minutes": "0"})
    client.post(f"/channels/{channel_id}/interval", data={"minutes": "abc"})
    assert channels_repo.get(conn, channel_id)["automatic_interval"] == 12


def test_dashboard_shows_live_recording_info(client, app, tmp_path):
    from db import participants_repo

    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    raw = tmp_path / "live.ts"
    raw.write_bytes(b"x" * 2048)
    rid = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path=str(raw)
    )
    conn.execute(
        "UPDATE recordings SET started_at = datetime('now', '-75 minutes') WHERE id = ?",
        (rid,),
    )
    conn.commit()
    participants_repo.replace_for_recording(
        conn,
        rid,
        [
            {"username": "gone", "start_seconds": 1, "end_seconds": 5},
            {"username": "os_grce", "start_seconds": 10, "end_seconds": None},
        ],
    )

    statuses = {channel_id: {"phase": "recording", "detail": str(raw), "flags": {}}}
    app.state.supervisor.get_status = lambda cid: statuses.get(cid)

    text = client.get("/partials/channels").text

    assert "01:15:" in text  # elapsed
    assert "2 KB" in text  # current file size
    assert "con @os_grce" in text and "@gone" not in text
    assert f"/library?recording_id={rid}" in text

    statuses[channel_id]["flags"] = {"stalled_since": 0}
    assert "sin datos nuevos" in client.get("/partials/channels").text


def test_settings_problem_alerts_toggle_and_session_health(client, app):
    from db import settings_repo
    from supervisor import alerts

    conn = get_connection(app.state.db_path)
    client.post("/settings/problem-alerts", data={})
    assert settings_repo.get(conn, alerts.ENABLED_KEY) == "0"
    client.post("/settings/problem-alerts", data={"enabled": "1"})
    assert settings_repo.get(conn, alerts.ENABLED_KEY) == "1"

    alerts.record_session_problem(conn, "creator")
    import utils.utils as utils_module

    original = utils_module.read_cookies
    utils_module.read_cookies = lambda: {"sessionid_ss": "abcdefgh"}
    try:
        assert "TikTok rechazó la sesión" in client.get("/settings").text
    finally:
        utils_module.read_cookies = original


def _nightly_recordings(conn, channel_id, nights, hour=22):
    for d in range(1, nights + 1):
        rid = recordings_repo.start(
            conn, channel_id=channel_id, username="creator", file_path=f"/o/{d}.ts"
        )
        recordings_repo.mark_completed(conn, rid, file_path=f"/o/{d}.ts", format="ts")
        conn.execute(
            f"""UPDATE recordings
                SET started_at = datetime('now', 'start of day', '-{d} days', '+{hour} hours'),
                    ended_at = datetime('now', 'start of day', '-{d} days', '+{hour + 2} hours')
                WHERE id = ?""",
            (rid,),
        )
    conn.execute(
        "UPDATE channels SET created_at = datetime('now', '-30 days') WHERE id = ?",
        (channel_id,),
    )
    conn.commit()


def test_creator_page_shows_learned_schedule(client, app):
    from supervisor import schedules

    schedules.invalidate()
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    _nightly_recordings(conn, channel_id, 10)

    text = client.get("/creators/creator").text

    assert 'id="schedule"' in text
    assert "basado en 10 lives" in text
    assert '"learned": true' in text


def test_smart_interval_toggle_and_dashboard_indicator(client, app):
    from supervisor import schedules

    schedules.invalidate()
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)

    response = client.post(f"/channels/{channel_id}/smart")
    assert channels_repo.get(conn, channel_id)["smart_interval"] == 1
    assert "aprendiendo horarios (0/5 lives" in response.text

    _nightly_recordings(conn, channel_id, 10)
    schedules.invalidate()
    text = client.get("/partials/channels").text
    assert "ahora cada" in text  # learned: shows the effective interval

    client.post(f"/channels/{channel_id}/smart")
    assert channels_repo.get(conn, channel_id)["smart_interval"] == 0


def test_library_marks_off_hours_recording(client, app):
    from supervisor import schedules

    schedules.invalidate()
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    _nightly_recordings(conn, channel_id, 12)
    odd = recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path="/o/odd.ts"
    )
    recordings_repo.mark_completed(conn, odd, file_path="/o/odd.ts", format="ts")
    conn.execute(
        """UPDATE recordings SET
           started_at = datetime('now', 'start of day', '-2 days', '+11 hours'),
           ended_at = datetime('now', 'start of day', '-2 days', '+12 hours')
           WHERE id = ?""",
        (odd,),
    )
    conn.commit()
    schedules.invalidate()

    assert "fuera de horario" in client.get(f"/library?recording_id={odd}").text
    night = conn.execute(
        "SELECT id FROM recordings WHERE file_path = '/o/3.ts'"
    ).fetchone()["id"]
    assert "fuera de horario" not in client.get(f"/library?recording_id={night}").text


def test_settings_reconnect_grace_saved_and_validated(client, app):
    from db import settings_repo

    conn = get_connection(app.state.db_path)
    assert 'name="minutes" value="5"' in client.get("/settings").text

    bad = client.post("/settings/reconnect-grace", data={"minutes": "45"})
    assert "entre 0 y 30 minutos" in bad.text

    client.post("/settings/reconnect-grace", data={"minutes": "2,5"})
    assert settings_repo.get(conn, "reconnect_grace_seconds") == "150"


def test_dashboard_shows_waiting_for_return(client, app, tmp_path):
    import time as time_module

    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    channels_repo.update(conn, channel_id, smart_interval=1)
    raw = tmp_path / "live.ts"
    raw.write_bytes(b"x")
    recordings_repo.start(
        conn, channel_id=channel_id, username="creator", file_path=str(raw)
    )
    status = {
        "phase": "recording",
        "detail": str(raw),
        "flags": {"waiting_return_until": time_module.time() + 200},
    }
    app.state.supervisor.get_status = lambda cid: status

    text = client.get("/partials/channels").text

    assert "esperando que vuelva" in text
    # The smart-interval hint means nothing while recording: hidden.
    assert "aprendiendo horarios" not in text and "ahora cada" not in text


def test_guests_are_grouped_per_person(client, app):
    from db import participants_repo

    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    rid = _completed(conn, channel_id, "/output/g.mp4", "mp4")
    participants_repo.replace_for_recording(
        conn,
        rid,
        [
            {"username": "julyyrg22", "start_seconds": 1, "end_seconds": 62},
            {"username": "other", "start_seconds": 62, "end_seconds": None},
            {"username": "julyyrg22", "start_seconds": 244, "end_seconds": 426},
        ],
    )

    text = client.get(f"/library?recording_id={rid}").text

    assert text.count('class="guest" data-guest') == 2  # one row per person
    assert "2 personas" in text
    assert "00:00:01 → 00:01:02" in text and "00:04:04 → 00:07:06" in text
    assert 'data-seek="244' in text  # each range jumps to its own start


def test_dates_are_localized_client_side(client, app):
    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    _completed(conn, channel_id, "/output/a.ts", "ts")

    text = client.get("/library").text
    assert 'data-started="' in text  # raw UTC for the browser to regroup
    assert "function regroup(list)" in text


def test_dashboard_delete_is_a_danger_button(client, app):
    conn = get_connection(app.state.db_path)
    channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    text = client.get("/partials/channels").text
    assert 'class="button-danger row-delete"' in text
    assert 'data-label="Estado"' in text


def test_library_is_video_first_and_full_width(client, app):
    from db import participants_repo

    conn = get_connection(app.state.db_path)
    channel_id = channels_repo.insert(conn, username="creator", mode=Mode.AUTOMATIC)
    rid = _completed(conn, channel_id, "/output/v.mp4", "mp4")
    participants_repo.replace_for_recording(
        conn, rid, [{"username": "guest", "start_seconds": 1, "end_seconds": 5}]
    )

    text = client.get(f"/library?recording_id={rid}").text

    assert 'class="container container-wide"' in text
    # Video column first, guests/clip tools in the side column after it.
    assert text.index('class="rec-media"') < text.index('class="rec-side"')
    assert text.index('id="clip-video"') < text.index("Con quién estuvo")
    assert text.count("Con quién estuvo") == 1
