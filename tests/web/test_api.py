import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import recordings_repo  # noqa: E402
from db.database import get_connection  # noqa: E402
from web import convert  # noqa: E402


def _recording(app, file_path, fmt, status="completed", started="2026-01-01 10:00:00"):
    conn = get_connection(app.state.db_path)
    rid = recordings_repo.start(
        conn, channel_id=None, username="creator", file_path=file_path, format=fmt
    )
    conn.execute("UPDATE recordings SET started_at = ? WHERE id = ?", (started, rid))
    conn.commit()
    if status == "completed":
        recordings_repo.mark_completed(
            conn, rid, file_path=file_path, format=fmt, file_size_bytes=123
        )
    return rid


def test_convert_args_keep_the_filters_that_fix_tiktok_streams():
    args = convert.ffmpeg_args("in.ts", "out.mp4", {})
    vf = args[args.index("-vf") + 1]
    assert "force_original_aspect_ratio=decrease" in vf and "pad=720:1280" in vf
    assert "fps=30" in vf
    assert args[args.index("-af") + 1].startswith("aresample=async=1")
    assert args[args.index("-i") + 1] == "in.ts" and args[-1] == "out.mp4"
    assert "h264_nvenc" in args


def test_convert_options_fall_back_to_defaults():
    assert convert.normalize("bogus", "1x1", "ultra") == convert.DEFAULTS
    args = convert.ffmpeg_args("i", "o", {"encoder": "libx264", "quality": "high"})
    assert args[args.index("-crf") + 1] == "20"


def test_command_text_quotes_only_when_needed():
    text = convert.command_text("TK con espacio.ts", {})
    assert '-i "TK con espacio.ts"' in text
    assert text.endswith('"TK con espacio.mp4"')
    assert '"-c:v"' not in text
    folder = convert.command_text("x.ts", {}, scope="folder")
    assert "-i $_.FullName" in folder and '($_.BaseName + ".mp4")' in folder


def test_api_lists_pending_raw_recordings_oldest_first(client, app):
    newer = _recording(app, "/out/TK_b.ts", "ts", started="2026-01-02 10:00:00")
    older = _recording(app, "/out/TK_a.ts", "ts", started="2026-01-01 10:00:00")
    _recording(app, "/out/TK_c.mp4", "mp4")
    _recording(app, "/out/TK_d.ts", "ts", status="recording")

    data = client.get("/api/recordings?state=raw").json()["recordings"]

    assert [r["id"] for r in data] == [older, newer]
    assert data[0]["file_name"] == "TK_a.ts"
    assert data[0]["output_name"] == "TK_a.mp4"
    assert data[0]["size_bytes"] == 123


def test_api_single_recording_and_404(client, app):
    rid = _recording(app, "/out/TK_a.ts", "ts")
    assert client.get(f"/api/recordings/{rid}").json()["format"] == "ts"
    assert client.get("/api/recordings/999").status_code == 404


def test_api_convert_args_have_placeholders(client):
    data = client.get("/api/convert/args?encoder=av1_nvenc").json()
    assert data["options"]["encoder"] == "av1_nvenc"
    assert "{input}" in data["args"] and data["args"][-1] == "{output}"


def test_api_convert_command_matches_module(client, app):
    rid = _recording(app, "/out/TK_a.ts", "ts")
    data = client.get(f"/api/convert/command?recording_id={rid}&quality=light").json()
    assert data["command"] == convert.command_text("TK_a.ts", {"quality": "light"})


def test_convert_script_download_is_prefilled_with_hub_url(client):
    response = client.get("/tools/convert-pending.ps1")

    assert response.status_code == 200
    text = response.text
    assert text.startswith("﻿")  # BOM for Windows PowerShell 5.1
    assert '[string]$Hub = "http://testserver"' in text
    # The "was the placeholder filled?" check must survive the substitution.
    assert '$placeholder = "__HUB" + "_URL__"' in text
    assert "attachment" in response.headers["content-disposition"]


def test_library_shows_batch_and_single_convert_commands(client, app):
    rid = _recording(app, "/out/TK_a.ts", "ts")

    text = client.get(f"/library?recording_id={rid}").text

    assert 'id="convert-bar"' in text  # batch command, top of the library
    assert "1 por convertir" in text
    assert f'data-convert-cmd data-ids="{rid}"' in text  # this recording only
    assert "convert-pending.ps1?inline=1" in text
    assert "Hacerlo a mano" in text  # manual steps still available


def test_no_convert_bar_when_nothing_pending(client, app):
    _recording(app, "/out/TK_a.mp4", "mp4")
    assert 'id="convert-bar"' not in client.get("/library").text


def test_settings_no_longer_has_scheduled_conversion(client):
    text = client.get("/settings").text
    assert "Register-ScheduledTask" not in text


def test_inline_script_has_no_bom_and_never_exits_the_console(client):
    text = client.get("/tools/convert-pending.ps1?inline=1").text
    assert not text.startswith("﻿")
    assert "[int[]]$Id" in text
    # `exit` is only allowed when running as a file, never in the main logic.
    assert "if ($PSCommandPath) { exit $exitCode }" in text
    body = text.split("function Invoke-HubConvert", 1)[1].split("$exitCode =", 1)[0]
    assert "exit " not in body


def test_script_uses_proxy_scheme_and_host(client):
    text = client.get(
        "/tools/convert-pending.ps1",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "tiktok.example.net",
        },
    ).text
    assert '[string]$Hub = "https://tiktok.example.net"' in text


def test_script_prefers_hub_public_url(client, monkeypatch):
    monkeypatch.setenv("HUB_PUBLIC_URL", "https://hub.example.org/")
    text = client.get(
        "/tools/convert-pending.ps1", headers={"X-Forwarded-Proto": "http"}
    ).text
    assert '[string]$Hub = "https://hub.example.org"' in text


def test_script_verifies_uploads_before_cleaning_up(client):
    text = client.get("/tools/convert-pending.ps1").text
    # A redirect or any non-200 must be an error, never a silent success,
    # and local files are only deleted once the hub shows the MP4.
    assert '-w "%{http_code}"' in text
    assert '$status -ne "200"' in text
    assert '$after.format -ne "mp4"' in text
    assert "%{redirect_url}" in text
