"""
Saved clips to Telegram through a bot (user request 2026-11-26: "solo para
mandar los clips"; a bot, a button per clip, one chat).

Settings live in hub.db (`settings` table): the bot token from @BotFather
and the chat id it posts to (a private chat with the bot, a group or a
channel where the bot was added). `find_chats` lists the chats the bot has
seen recently (getUpdates), so nobody has to dig the chat id out by hand.

A clip is cut from the recording's MP4 with stream copy into a temporary
faststart MP4 (Telegram needs the moov atom up front to play it inline),
sent with sendVideo and deleted. Bots can upload at most 50 MB per file
(about 5 min at the hub's usual ~1.3 Mbps): bigger clips are refused with
the size, nothing is re-encoded.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import requests

TOKEN_KEY = "telegram_bot_token"
CHAT_KEY = "telegram_chat_id"
BOT_NAME_KEY = "telegram_bot_username"
CHAT_TITLE_KEY = "telegram_chat_title"
MAX_BYTES = 50 * 1000 * 1000  # Bot API upload limit (decimal MB)
_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    """A user-facing (Spanish) reason the request failed."""


def _call(token, method, files=None, timeout=(10, 60), **params):
    if not token:
        raise TelegramError("Falta configurar el bot de Telegram en Ajustes.")
    try:
        response = requests.post(
            _API.format(token=token.strip(), method=method),
            data=params,
            files=files,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise TelegramError(f"No se pudo conectar con Telegram: {e}") from e
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not data.get("ok"):
        detail = data.get("description") or f"HTTP {response.status_code}"
        if response.status_code == 401:
            detail = "el token no es válido"
        raise TelegramError(f"Telegram respondió: {detail}")
    return data.get("result")


def bot_username(token):
    """Checks the token (getMe); returns the bot's @username."""
    return _call(token, "getMe").get("username")


def find_chats(token):
    """Chats the bot got messages from recently: [{id, title, type}]."""
    updates = _call(token, "getUpdates", allowed_updates=json.dumps([])) or []
    chats = {}
    for update in updates:
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (update.get(key) or {}).get("chat")
            if not chat:
                continue
            title = chat.get("title") or " ".join(
                p for p in (chat.get("first_name"), chat.get("last_name")) if p
            )
            if chat.get("username"):
                title = (
                    f"{title} (@{chat['username']})"
                    if title
                    else f"@{chat['username']}"
                )
            chats[str(chat["id"])] = {
                "id": str(chat["id"]),
                "title": title or str(chat["id"]),
                "type": chat.get("type", ""),
            }
    return list(chats.values())


def send_test(token, chat_id):
    if not chat_id:
        raise TelegramError("Falta elegir a qué chat mandar los clips.")
    _call(
        token,
        "sendMessage",
        chat_id=chat_id,
        text="✂ Prueba del hub: acá van a llegar los clips.",
    )


def _hhmmss(seconds):
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    return (
        f"{hours}:{rest // 60:02d}:{rest % 60:02d}"
        if hours
        else f"{rest // 60}:{rest % 60:02d}"
    )


def caption_for(recording, mark):
    lines = []
    if mark["label"]:
        lines.append(f"✂ {mark['label']}")
    when = (recording["started_at"] or "")[:10]
    lines.append(
        f"@{recording['username']} · {_hhmmss(mark['start_seconds'])}–"
        f"{_hhmmss(mark['end_seconds'])}"
        + (f" · live del {when} (UTC)" if when else "")
    )
    return "\n".join(lines)


def _probe_size(path, ffprobe="ffprobe"):
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        stream = (json.loads(out).get("streams") or [{}])[0]
        return stream.get("width"), stream.get("height")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None


def cut_clip(source, start, end, destination, ffmpeg="ffmpeg"):
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-to",
            str(end),
            "-i",
            str(source),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0 or not Path(destination).exists():
        raise TelegramError(
            f"No se pudo cortar el clip: {result.stderr.strip()[-200:]}"
        )


def send_clip(token, chat_id, recording, mark, work_dir=None):
    """Cuts the clip and sends it. Raises TelegramError with the reason."""
    if not chat_id:
        raise TelegramError("Falta elegir a qué chat mandar los clips (Ajustes).")
    source = Path(recording["file_path"])
    if recording["format"] != "mp4" or not source.is_file():
        raise TelegramError(
            "Solo se pueden mandar clips de grabaciones ya convertidas."
        )
    ffmpeg = os.environ.get("FFMPEG_PATH", "ffmpeg")
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        clip = Path(tmp) / f"{source.stem}_clip_{int(mark['start_seconds'])}.mp4"
        cut_clip(source, mark["start_seconds"], mark["end_seconds"], clip, ffmpeg)
        size = clip.stat().st_size
        if size > MAX_BYTES:
            raise TelegramError(
                f"El clip pesa {size / 1e6:.0f} MB y un bot de Telegram solo puede "
                f"mandar hasta {MAX_BYTES / 1e6:.0f} MB. Guardá un clip más corto."
            )
        width, height = _probe_size(clip, ffmpeg.replace("ffmpeg", "ffprobe"))
        params = {
            "chat_id": chat_id,
            "caption": caption_for(recording, mark),
            "supports_streaming": "true",
            "duration": str(int(round(mark["end_seconds"] - mark["start_seconds"]))),
        }
        if width and height:
            params.update(width=str(width), height=str(height))
        with open(clip, "rb") as f:
            _call(
                token,
                "sendVideo",
                files={"video": (clip.name, f, "video/mp4")},
                timeout=(10, 600),
                **params,
            )
