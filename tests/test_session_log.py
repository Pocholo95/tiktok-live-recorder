import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils import session_log  # noqa: E402


class _Headers:
    def __init__(self, values):
        self._values = values

    def get_list(self, name):
        return self._values if name == "set-cookie" else []


class _Response:
    def __init__(self, cookies):
        self.headers = _Headers(cookies)


def _events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_logs_cookie_names_never_values(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    session_log._last_seen.clear()
    response = _Response(
        ["sessionid_ss=SECRET123; Path=/; Domain=.tiktok.com", "csrfToken=abc; Path=/"]
    )

    session_log.note_response(
        "https://www.tiktok.com/api-live/user/room?uniqueId=x", response
    )
    session_log.note_response(
        "https://www.tiktok.com/api-live/user/room?uniqueId=y", response
    )

    path = tmp_path / session_log.FILE_NAME
    text = path.read_text(encoding="utf-8")
    assert "SECRET123" not in text and "abc" not in text and "uniqueId" not in text
    events = _events(path)
    assert len(events) == 1  # same endpoint + names: once per hour
    assert events[0]["names"] == ["csrfToken", "sessionid_ss"]
    assert events[0]["path"] == "/api-live/user/room"


def test_refusals_and_events_are_logged_only_in_the_hub(tmp_path, monkeypatch):
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    session_log.note_refusal("ana", 1, "live_urls", True, {"prompts": ""})
    assert not list(tmp_path.iterdir())  # CLI: nothing written

    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    session_log.note_refusal("ana", 1, "live_urls", True, {"prompts": "edad"})
    session_log.note_event("session_saved")
    events = _events(tmp_path / session_log.FILE_NAME)
    assert events[0]["event"] == "refused" and events[0]["tiktok_message"] == {
        "prompts": "edad"
    }
    assert events[1]["event"] == "session_saved"
