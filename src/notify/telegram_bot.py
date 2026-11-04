"""
Live start/end notifications through a Telegram bot (Bot API over HTTPS).

A bot token (from @BotFather) plus the user's chat id is all it needs - no
interactive login, unlike the Telethon user-account client in
upload/telegram.py, so it works unattended inside Docker. Settings live in
telegram.json under `bot_token` / `bot_chat_id`.

Sending never blocks or breaks a recording: it runs on a daemon thread and
any failure is only logged.
"""

import html
import os
import threading

import requests

from utils.logger_manager import logger
from utils.utils import read_telegram_config

_API = "https://api.telegram.org/bot{token}/sendMessage"


def _bot_settings():
    try:
        config = read_telegram_config()
    except Exception:
        return None, None
    token = str(config.get("bot_token") or "").strip()
    chat_id = str(config.get("bot_chat_id") or "").strip()
    return (token, chat_id) if token and chat_id else (None, None)


def is_configured():
    token, _ = _bot_settings()
    return token is not None


def send_message(text):
    """
    Sends `text` (Telegram HTML) synchronously. Returns None on success or
    an error message string - used by the settings page's "send test" button.
    """
    token, chat_id = _bot_settings()
    if token is None:
        return "Falta configurar el token del bot y el chat_id."
    try:
        response = requests.post(
            _API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        return f"No se pudo conectar con Telegram: {e}"
    if response.status_code != 200:
        try:
            detail = response.json().get("description", response.text)
        except ValueError:
            detail = response.text
        return f"Telegram respondió {response.status_code}: {detail}"
    return None


def send_in_background(text):
    def run():
        error = send_message(text)
        if error:
            logger.warning(f"Telegram notification failed: {error}")

    threading.Thread(target=run, name="telegram-notify", daemon=True).start()


def _library_link(recording_id):
    base = os.environ.get("HUB_PUBLIC_URL", "").rstrip("/")
    if not base or recording_id is None:
        return ""
    return f'\n<a href="{base}/library?recording_id={recording_id}">Abrir en el hub</a>'


def _duration(seconds):
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _size(num_bytes):
    if not num_bytes:
        return ""
    gb = num_bytes / 1024**3
    return f" · {gb:.1f} GB" if gb >= 1 else f" · {num_bytes / 1024**2:.0f} MB"


def live_started(username, recording_id=None):
    user = html.escape(username)
    send_in_background(
        f"🔴 <b>@{user}</b> está en vivo, grabando…\n"
        f'<a href="https://www.tiktok.com/@{user}/live">Ver en TikTok</a>'
        + _library_link(recording_id)
    )


def live_finished(username, recording_id=None, duration_seconds=None, size_bytes=None):
    user = html.escape(username)
    send_in_background(
        f"⏹ Terminó el live de <b>@{user}</b>\n"
        f"Duración {_duration(duration_seconds)}{_size(size_bytes)} · pendiente de convertir"
        + _library_link(recording_id)
    )


def live_failed(username, error, recording_id=None):
    send_in_background(
        f"⚠️ Falló la grabación de <b>@{html.escape(username)}</b>\n"
        f"{html.escape(str(error))}" + _library_link(recording_id)
    )
