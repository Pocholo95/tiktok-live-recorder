import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import notify.discord_webhook as discord  # noqa: E402

URL = "https://discord.com/api/webhooks/123/token"


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def test_url_validation():
    assert discord.is_valid_url(URL)
    assert discord.is_valid_url("https://discordapp.com/api/webhooks/1/x")
    assert not discord.is_valid_url("https://example.com/api/webhooks/1/x")
    assert not discord.is_valid_url("")
    assert not discord.is_valid_url(None)


def test_send_requires_configuration():
    assert "webhook" in discord.send(None, discord.test_embed())


def test_send_posts_embed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        discord.requests,
        "post",
        lambda url, json, timeout: calls.append((url, json)) or _Response(204),
    )

    assert discord.send(URL, discord.test_embed()) is None
    url, payload = calls[0]
    assert url == URL
    assert payload["embeds"][0]["title"].startswith("✅")


def test_send_reports_discord_error(monkeypatch):
    monkeypatch.setattr(
        discord.requests,
        "post",
        lambda url, json, timeout: _Response(404, {"message": "Unknown Webhook"}),
    )

    assert "Unknown Webhook" in discord.send(URL, discord.test_embed())


def test_live_embeds(monkeypatch):
    sent = []
    monkeypatch.setattr(discord, "send_in_background", lambda url, e: sent.append(e))
    monkeypatch.setenv("HUB_PUBLIC_URL", "http://hub.local:8000/")

    discord.live_started(URL, "some_user", recording_id=7)
    discord.live_finished(
        URL, "some_user", duration_seconds=3725, size_bytes=2 * 1024**3
    )

    started, finished = sent
    assert started["url"] == "https://www.tiktok.com/@some_user/live"
    assert "http://hub.local:8000/library?recording_id=7" in started["description"]
    values = {f["name"]: f["value"] for f in finished["fields"]}
    assert values["Duración"] == "1:02:05"
    assert values["Tamaño"] == "2.0 GB"
    assert "description" not in finished  # no recording id -> no hub link
