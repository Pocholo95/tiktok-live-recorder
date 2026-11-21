import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import settings_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from supervisor import canary  # noqa: E402


class _Api:
    def __init__(self, script):
        self.script = script  # list of "live" | "offline" | "error"

    def get_room_id_from_user(self, user):
        if self.script[0] == "error":
            raise RuntimeError("tiktok changed")
        return "123"

    def is_room_alive(self, room_id):
        return self.script[0] == "live"


def _setup(tmp_path, monkeypatch, script):
    sent = []
    monkeypatch.setattr(
        canary.alerts.discord_webhook,
        "send_in_background",
        lambda url, embed: sent.append(embed["title"]),
    )
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    settings_repo.set(
        get_connection(db_path),
        "discord_webhook_url",
        "https://discord.com/api/webhooks/1/a",
    )
    now = [0.0]
    api = _Api(script)
    c = canary.DetectionCanary(
        db_path,
        get_connection,
        lambda: api,
        user="weathernewslive",
        clock=lambda: now[0],
    )
    return c, api, now, sent


def test_alerts_once_after_half_an_hour_without_seeing_the_canary(
    tmp_path, monkeypatch
):
    c, api, now, sent = _setup(tmp_path, monkeypatch, ["live"])
    c.check_once()
    api.script = ["offline"]
    for t in (600, 1200, 1800):
        now[0] = t
        c.check_once()
    assert sent == []  # 20 min bad so far
    api.script = ["error"]
    now[0] = 2400
    c.check_once()
    assert len(sent) == 1 and "rota" in sent[0]
    now[0] = 3000
    c.check_once()
    assert len(sent) == 1  # once per episode

    api.script = ["live"]
    c.check_once()
    api.script = ["offline"]
    now[0] = 10_000
    c.check_once()
    now[0] = 10_000 + canary.ALERT_AFTER_SECONDS
    c.check_once()
    assert len(sent) == 1  # new episode, but the 6 h throttle still holds


def test_canary_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("HUB_CANARY_USER", "")
    assert canary.canary_user() == ""
    monkeypatch.delenv("HUB_CANARY_USER")
    assert canary.canary_user() == "weathernewslive"
