import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import clip_marks_repo, recordings_repo, settings_repo  # noqa: E402
from db.database import get_connection  # noqa: E402
from notify import telegram_clips  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg not installed",
)


class _FakeTelegram:
    """Stands in for telegram_clips._call: records calls, answers like the API."""

    def __init__(self, updates=None):
        self.calls = []
        self.updates = updates or []

    def __call__(self, token, method, files=None, timeout=None, **params):
        if not token:
            raise telegram_clips.TelegramError(
                "Falta configurar el bot de Telegram en Ajustes."
            )
        if token == "bad":
            raise telegram_clips.TelegramError(
                "Telegram respondió: el token no es válido"
            )
        sent = None
        if files:
            sent = files["video"][1].read()
        self.calls.append((method, params, sent))
        if method == "getMe":
            return {"username": "clips_bot"}
        if method == "getUpdates":
            return self.updates
        return {"message_id": 1}


@pytest.fixture
def fake(monkeypatch):
    fake = _FakeTelegram(
        updates=[
            {"message": {"chat": {"id": 42, "type": "private", "first_name": "Ana"}}},
            {
                "my_chat_member": {
                    "chat": {"id": -100, "type": "channel", "title": "Clips"}
                }
            },
            {"message": {"chat": {"id": 42, "type": "private", "first_name": "Ana"}}},
        ]
    )
    monkeypatch.setattr(telegram_clips, "_call", fake)
    return fake


def _mp4(path, seconds=6):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         f"testsrc=size=320x640:rate=30:duration={seconds}", "-f", "lavfi", "-i",
         f"sine=duration={seconds}", "-c:v", "libx264", "-g", "30", "-c:a", "aac",
         "-shortest", str(path)],
        check=True,
    )  # fmt: skip
    return path


def _recording_with_clip(app, path, fmt="mp4"):
    conn = get_connection(app.state.db_path)
    rid = recordings_repo.start(
        conn, channel_id=None, username="creator", file_path=str(path), format=fmt
    )
    recordings_repo.mark_completed(
        conn, rid, file_path=str(path), format=fmt, file_size_bytes=1
    )
    mark_id = clip_marks_repo.insert(
        conn, recording_id=rid, start_seconds=1, end_seconds=4, label="Gol"
    )
    return rid, mark_id


def _configure(app, token="t0k", chat="42"):
    conn = get_connection(app.state.db_path)
    settings_repo.set(conn, telegram_clips.TOKEN_KEY, token)
    settings_repo.set(conn, telegram_clips.CHAT_KEY, chat)


def test_find_chats_lists_each_chat_once(fake):
    chats = telegram_clips.find_chats("t0k")
    assert chats == [
        {"id": "42", "title": "Ana", "type": "private"},
        {"id": "-100", "title": "Clips", "type": "channel"},
    ]


def test_settings_save_bot_pick_chat_and_test(client, app, fake):
    response = client.post("/settings/telegram-bot", data={"token": "bad"})
    assert "no es válido" in response.text
    response = client.post("/settings/telegram-bot", data={"token": "t0k"})
    assert "@clips_bot" in response.text and "todavía no elegiste" in response.text

    found = client.post("/settings/telegram-bot/find-chats")
    assert "Ana" in found.text and "Clips" in found.text and "canal" in found.text

    client.post(
        "/settings/telegram-bot/chat", data={"chat_id": "-100", "chat_title": "Clips"}
    )
    page = client.post("/settings/telegram-bot/test")
    assert "Mensaje de prueba enviado" in page.text
    assert fake.calls[-1][0] == "sendMessage" and fake.calls[-1][1]["chat_id"] == "-100"

    client.post("/settings/telegram-bot", data={"clear": "1"})
    conn = get_connection(app.state.db_path)
    assert settings_repo.get(conn, telegram_clips.CHAT_KEY) is None


def test_clip_button_only_when_configured(client, app, tmp_path, fake):
    rid, _mark = _recording_with_clip(app, tmp_path / "rec.mp4")
    assert "/telegram" not in client.get(f"/library?recording_id={rid}").text
    _configure(app)
    assert "/telegram" in client.get(f"/library?recording_id={rid}").text


@needs_ffmpeg
def test_sends_the_cut_clip_and_remembers_it(client, app, tmp_path, fake):
    rid, mark_id = _recording_with_clip(app, _mp4(tmp_path / "rec.mp4"))
    _configure(app)

    response = client.post(f"/clip-marks/{mark_id}/telegram")

    assert response.status_code == 200 and "✓ Telegram" in response.text
    method, params, video = fake.calls[-1]
    assert method == "sendVideo" and params["chat_id"] == "42"
    assert params["caption"].startswith("✂ Gol\n@creator · 0:01–0:04")
    assert params["width"] == "320" and params["height"] == "640"
    assert video[4:8] == b"ftyp" and b"moov" in video[:4096]  # faststart
    mark = clip_marks_repo.get(get_connection(app.state.db_path), mark_id)
    assert mark["telegram_sent_at"]
    assert not list(Path(app.state.output_dir).glob("tmp*"))  # temp clip removed


@needs_ffmpeg
def test_refuses_clips_over_the_bot_limit(client, app, tmp_path, fake, monkeypatch):
    _rid, mark_id = _recording_with_clip(app, _mp4(tmp_path / "rec.mp4"))
    _configure(app)
    monkeypatch.setattr(telegram_clips, "MAX_BYTES", 1000)

    response = client.post(f"/clip-marks/{mark_id}/telegram")

    assert "solo puede mandar hasta" in response.text
    assert not [c for c in fake.calls if c[0] == "sendVideo"]


def test_raw_recordings_cannot_send(client, app, tmp_path, fake):
    raw = tmp_path / "rec.ts"
    raw.write_bytes(b"x")
    _rid, mark_id = _recording_with_clip(app, raw, fmt="ts")
    _configure(app)
    response = client.post(f"/clip-marks/{mark_id}/telegram")
    assert "ya convertidas" in response.text
