import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import notify.telegram_bot as bot  # noqa: E402


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_send_message_requires_configuration(monkeypatch):
    monkeypatch.setattr(bot, "read_telegram_config", lambda: {"chat_id": "me"})
    assert "token" in bot.send_message("hola")
    assert not bot.is_configured()


def test_send_message_posts_to_bot_api(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bot,
        "read_telegram_config",
        lambda: {"bot_token": "123:abc", "bot_chat_id": "42"},
    )
    monkeypatch.setattr(
        bot.requests,
        "post",
        lambda url, json, timeout: calls.append((url, json)) or _Response(200, {}),
    )

    assert bot.send_message("<b>hola</b>") is None
    url, payload = calls[0]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert payload["chat_id"] == "42"
    assert payload["parse_mode"] == "HTML"


def test_send_message_reports_telegram_error(monkeypatch):
    monkeypatch.setattr(
        bot,
        "read_telegram_config",
        lambda: {"bot_token": "123:abc", "bot_chat_id": "42"},
    )
    monkeypatch.setattr(
        bot.requests,
        "post",
        lambda url, json, timeout: _Response(400, {"description": "chat not found"}),
    )

    assert "chat not found" in bot.send_message("hola")


def test_messages_escape_usernames(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_in_background", sent.append)

    bot.live_started("<evil>")
    bot.live_finished("user", duration_seconds=3725, size_bytes=2 * 1024**3)

    assert "&lt;evil&gt;" in sent[0]
    assert "1:02:05" in sent[1]
    assert "2.0 GB" in sent[1]
