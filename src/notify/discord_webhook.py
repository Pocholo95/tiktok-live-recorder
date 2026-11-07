"""
Live start/end notifications through a Discord webhook.

The webhook URL (Server settings -> Integrations -> Webhooks) is the only
setting; it's stored in hub.db (`settings` table, key `discord_webhook_url`)
so it survives container rebuilds without another mounted file.

Sending never blocks or breaks a recording: it runs on a daemon thread and
any failure is only logged.
"""

import os
import threading

import requests

from utils.logger_manager import logger

SETTING_KEY = "discord_webhook_url"

_VALID_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
)

_RED = 0xE5484D
_GREY = 0x8B91A0
_AMBER = 0xE0A63C
_GREEN = 0x3ECF8E


def is_valid_url(url):
    return bool(url) and url.strip().startswith(_VALID_PREFIXES)


def send(webhook_url, embed):
    """
    Posts one embed synchronously. Returns None on success or an error
    message string (shown by the settings page's "send test" button).
    """
    if not is_valid_url(webhook_url):
        return "Falta configurar la URL del webhook de Discord."
    try:
        response = requests.post(
            webhook_url.strip(),
            json={"username": "TikTok Live Recorder", "embeds": [embed]},
            timeout=15,
        )
    except requests.RequestException as e:
        return f"No se pudo conectar con Discord: {e}"
    if response.status_code not in (200, 204):
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        return f"Discord respondió {response.status_code}: {detail}"
    return None


def send_in_background(webhook_url, embed):
    if not is_valid_url(webhook_url):
        return

    def run():
        error = send(webhook_url, embed)
        if error:
            logger.warning(f"Discord notification failed: {error}")

    threading.Thread(target=run, name="discord-notify", daemon=True).start()


def _hub_link(recording_id):
    base = os.environ.get("HUB_PUBLIC_URL", "").rstrip("/")
    if not base or recording_id is None:
        return None
    return f"{base}/library?recording_id={recording_id}"


def _escape(text):
    # Usernames can contain "_" and "." - keep markdown from mangling them.
    return str(text).replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*")


def _duration(seconds):
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _size(num_bytes):
    gb = num_bytes / 1024**3
    return f"{gb:.1f} GB" if gb >= 1 else f"{num_bytes / 1024**2:.0f} MB"


def test_embed():
    return {
        "title": "✅ Los avisos funcionan",
        "description": "Este canal va a recibir los avisos de lives del hub.",
        "color": _GREEN,
    }


def live_started(webhook_url, username, recording_id=None):
    link = _hub_link(recording_id)
    embed = {
        "title": f"🔴 @{username} está en vivo",
        "url": f"https://www.tiktok.com/@{username}/live",
        "description": "Grabando…" + (f"\n[Abrir en el hub]({link})" if link else ""),
        "color": _RED,
    }
    send_in_background(webhook_url, embed)


def live_finished(
    webhook_url, username, recording_id=None, duration_seconds=None, size_bytes=None
):
    fields = [
        {"name": "Duración", "value": _duration(duration_seconds), "inline": True}
    ]
    if size_bytes:
        fields.append({"name": "Tamaño", "value": _size(size_bytes), "inline": True})
    fields.append({"name": "Estado", "value": "Pendiente de convertir", "inline": True})
    link = _hub_link(recording_id)
    embed = {
        "title": f"⏹ Terminó el live de @{username}",
        "description": f"[Abrir en el hub]({link})" if link else None,
        "fields": fields,
        "color": _GREY,
    }
    send_in_background(webhook_url, {k: v for k, v in embed.items() if v})


def live_failed(webhook_url, username, error, recording_id=None):
    link = _hub_link(recording_id)
    embed = {
        "title": f"⚠️ Falló la grabación de @{username}",
        "description": _escape(error)[:1000]
        + (f"\n[Abrir en el hub]({link})" if link else ""),
        "color": _AMBER,
    }
    send_in_background(webhook_url, embed)


def problem_embed(title, description):
    """Health problems (not live notices): amber, no hub link needed."""
    return {"title": f"⚠️ {title}", "description": description[:1500], "color": _AMBER}
