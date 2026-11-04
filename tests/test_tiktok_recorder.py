import sys
from pathlib import Path
from threading import Event

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.tiktok_recorder import TikTokRecorder  # noqa: E402
from utils.custom_exceptions import TikTokRecorderError  # noqa: E402
from utils.enums import Mode  # noqa: E402
from utils.recorder_config import RecorderConfig  # noqa: E402


class FakeTikTokAPI:
    def __init__(self, blacklisted=True):
        self.blacklisted = blacklisted
        self.calls = []

    def is_country_blacklisted(self):
        self.calls.append("is_country_blacklisted")
        return self.blacklisted

    def get_room_id_from_user(self, user):
        self.calls.append(f"get_room_id_from_user:{user}")
        return "1234567890"

    def get_user_from_room_id(self, room_id):
        self.calls.append(f"get_user_from_room_id:{room_id}")
        return "creator"

    def get_sec_uid(self):
        self.calls.append("get_sec_uid")
        return "sec_uid"

    def is_room_alive(self, room_id):
        self.calls.append(f"is_room_alive:{room_id}")
        return True


def test_setup_resolves_room_id_before_country_check_for_manual_user():
    recorder = TikTokRecorder(
        RecorderConfig(mode=Mode.MANUAL, user="creator", cookies={})
    )
    fake_api = FakeTikTokAPI(blacklisted=True)
    recorder.tiktok = fake_api

    recorder._setup()

    assert recorder.room_id == "1234567890"
    assert fake_api.calls == [
        "get_room_id_from_user:creator",
        "is_country_blacklisted",
        "is_room_alive:1234567890",
    ]


def test_setup_keeps_followers_country_check_before_sec_uid():
    recorder = TikTokRecorder(RecorderConfig(mode=Mode.FOLLOWERS, cookies={}))
    fake_api = FakeTikTokAPI(blacklisted=True)
    recorder.tiktok = fake_api

    with pytest.raises(TikTokRecorderError, match="Captcha required"):
        recorder._setup()

    assert fake_api.calls == ["is_country_blacklisted"]


def test_setup_keeps_automatic_mode_blocked_after_room_resolution():
    recorder = TikTokRecorder(
        RecorderConfig(mode=Mode.AUTOMATIC, user="creator", cookies={})
    )
    fake_api = FakeTikTokAPI(blacklisted=True)
    recorder.tiktok = fake_api

    with pytest.raises(TikTokRecorderError, match="Automatic mode is available"):
        recorder._setup()

    assert recorder.room_id == "1234567890"
    assert fake_api.calls == [
        "get_room_id_from_user:creator",
        "is_country_blacklisted",
    ]


def test_setup_keeps_manual_room_id_allowed_when_country_check_is_blocked():
    recorder = TikTokRecorder(
        RecorderConfig(mode=Mode.MANUAL, room_id="1234567890", cookies={})
    )
    fake_api = FakeTikTokAPI(blacklisted=True)
    recorder.tiktok = fake_api

    recorder._setup()

    assert recorder.room_id == "1234567890"
    assert fake_api.calls == [
        "get_user_from_room_id:1234567890",
        "is_country_blacklisted",
        "is_room_alive:1234567890",
    ]


def test_automatic_mode_exits_immediately_when_stop_event_is_set():
    stop_event = Event()
    stop_event.set()

    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.AUTOMATIC, user="creator", cookies={}, stop_event=stop_event
        )
    )
    fake_api = FakeTikTokAPI(blacklisted=False)
    recorder.tiktok = fake_api

    recorder.automatic_mode()

    assert fake_api.calls == []


class _StreamingFakeTikTokAPI:
    """Fake TikTokAPI for exercising start_recording end-to-end."""

    def __init__(self, stream_chunks):
        self._stream_chunks = stream_chunks
        self._room_alive_calls = 0

    def is_room_alive(self, room_id):
        # Live for the first check inside start_recording's loop, offline on
        # the recheck afterwards - mirrors how a real stream ending is
        # detected only once the current chunk generator is exhausted.
        self._room_alive_calls += 1
        return self._room_alive_calls == 1

    def get_live_url_candidates(self, room_id, user=None):
        return ["https://example.invalid/live.flv"]

    def download_live_stream(self, live_url):
        yield from self._stream_chunks


class _FakeRemuxer:
    """Stands in for TsRemuxer: records what the recorder hands it."""

    instances = []

    def __init__(self, output_path, ffmpeg_path=None):
        self.output_path = output_path
        self.chunks = []
        self.connections = 1
        self.closed = False
        Path(output_path).write_bytes(b"")
        _FakeRemuxer.instances.append(self)

    def new_connection(self):
        self.connections += 1

    def write(self, chunk):
        self.chunks.append(chunk)

    def close(self):
        self.closed = True


def test_start_recording_writes_ts_without_converting(tmp_path, monkeypatch):
    events = []
    _FakeRemuxer.instances.clear()
    monkeypatch.setattr("core.tiktok_recorder.TsRemuxer", _FakeRemuxer)

    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            cookies={},
            output=str(tmp_path),
            on_status=lambda phase, **data: events.append((phase, data)),
        )
    )
    recorder.tiktok = _StreamingFakeTikTokAPI(stream_chunks=[b"x" * 5000])

    recorder.start_recording("creator", "1234567890")

    assert [phase for phase, _ in events] == ["recording_started", "recording_finished"]
    assert events[-1][1]["format"] == "ts"
    assert events[-1][1]["file_path"].endswith(".ts")
    remuxer = _FakeRemuxer.instances[-1]
    assert remuxer.chunks == [b"x" * 5000]
    assert remuxer.closed


def test_start_recording_tells_remuxer_about_reconnects(tmp_path, monkeypatch):
    _FakeRemuxer.instances.clear()
    monkeypatch.setattr("core.tiktok_recorder.TsRemuxer", _FakeRemuxer)

    class _ReconnectingAPI(_StreamingFakeTikTokAPI):
        def is_room_alive(self, room_id):
            # Alive for two connections, then the live ends.
            self._room_alive_calls += 1
            return self._room_alive_calls <= 2

    recorder = TikTokRecorder(
        RecorderConfig(
            mode=Mode.MANUAL,
            user="creator",
            room_id="1234567890",
            cookies={},
            output=str(tmp_path),
        )
    )
    recorder.tiktok = _ReconnectingAPI(stream_chunks=[b"x" * 5000])

    recorder.start_recording("creator", "1234567890")

    remuxer = _FakeRemuxer.instances[-1]
    assert remuxer.connections == 2
    assert len(remuxer.chunks) == 2
