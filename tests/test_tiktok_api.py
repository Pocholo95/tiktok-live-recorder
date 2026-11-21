import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.tiktok_api import TikTokAPI  # noqa: E402
from utils.custom_exceptions import UserLiveError  # noqa: E402


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return FakeResponse(self.responses.pop(0))


def build_api(*responses):
    api = TikTokAPI.__new__(TikTokAPI)
    api.WEBCAST_URL = "https://webcast.tiktok.com"
    api.http_client = FakeHttpClient(list(responses))
    return api


def test_is_room_alive_rejects_fake_check_alive_positive():
    api = build_api(
        {"data": [{"alive": True, "room_id": 123}], "status_code": 0},
        {"data": {"message": "Request params error"}, "status_code": 10011},
    )

    assert api.is_room_alive("123") is False


def test_is_room_alive_accepts_confirmed_stream_room():
    api = build_api(
        {"data": [{"alive": True, "room_id": 123}], "status_code": 0},
        {
            "data": {
                "status": 2,
                "stream_url": {
                    "live_core_sdk_data": {"pull_data": {"stream_data": '{"data": {}}'}}
                },
            },
            "status_code": 0,
        },
    )

    assert api.is_room_alive("123") is True


def test_is_room_alive_keeps_restricted_live_as_alive():
    api = build_api(
        {"data": [{"alive": True, "room_id": 123}], "status_code": 0},
        {"data": {}, "status_code": 4003110},
    )

    assert api.is_room_alive("123") is True


def test_is_room_alive_skips_room_info_when_check_alive_is_false():
    api = build_api({"data": [{"alive": False, "room_id": 123}], "status_code": 0})

    assert api.is_room_alive("123") is False
    assert len(api.http_client.urls) == 1


def test_is_room_alive_rejects_null_check_alive_data():
    api = build_api({"data": None, "status_code": 0})

    assert api.is_room_alive("123") is False
    assert len(api.http_client.urls) == 1


def test_is_room_alive_rejects_null_room_info_data():
    api = build_api(
        {"data": [{"alive": True, "room_id": 123}], "status_code": 0},
        {"data": None, "status_code": 0},
    )

    assert api.is_room_alive("123") is False


def test_is_room_alive_rejects_ended_room_with_stale_stream_urls():
    api = build_api(
        {"data": [{"alive": True, "room_id": 123}], "status_code": 0},
        {
            "data": {
                "status": 4,
                "finish_time": 1784118433,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {"stream_data": '{"data": {}}'}
                    },
                    "flv_pull_url": {"HD1": "https://example.com/stale.flv"},
                },
            },
            "status_code": 0,
        },
    )

    assert api.is_room_alive("123") is False


def test_get_live_url_rejects_ended_room_with_stale_stream_urls():
    api = build_api(
        {
            "data": {
                "status": 4,
                "finish_time": 1784118433,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {"stream_data": '{"data": {}}'}
                    },
                    "flv_pull_url": {"HD1": "https://example.com/stale.flv"},
                },
            },
            "status_code": 0,
        },
    )

    with pytest.raises(UserLiveError, match="not hosting a live stream"):
        api.get_live_url("123", user="creator")


def test_get_live_url_candidates_returns_ordered_unique_streams():
    api = build_api(
        {
            "data": {
                "status": 2,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {
                            "stream_data": (
                                '{"data": {'
                                '"hd": {"main": {"flv": "https://cdn/hd.flv"}},'
                                '"ld": {"main": {"flv": "https://cdn/ld.flv"}},'
                                '"ao": {"main": {"flv": "https://cdn/audio.flv"}}'
                                "}}"
                            ),
                            "options": {
                                "qualities": [
                                    {"sdk_key": "hd", "level": 3},
                                    {"sdk_key": "ld", "level": 1},
                                ]
                            },
                        }
                    },
                    "flv_pull_url": {
                        "HD1": "https://cdn/hd.flv",
                        "SD1": "https://cdn/sd.flv",
                    },
                },
            },
            "status_code": 0,
        },
    )

    assert api.get_live_url_candidates("123", user="creator") == [
        "https://cdn/hd.flv",
        "https://cdn/ld.flv",
        "https://cdn/audio.flv",
        "https://cdn/sd.flv",
    ]


def _sdk_room(status, data, qualities=()):
    import json as _json

    return {
        "data": {
            "status": status,
            "stream_url": {
                "live_core_sdk_data": {
                    "pull_data": {
                        "stream_data": _json.dumps({"data": data}),
                        "options": {"qualities": list(qualities)},
                    }
                },
            },
        },
        "status_code": 0,
    }


def _variant(name, vbitrate=0, resolution="", backup=True, hls=True):
    import json as _json

    main = {
        "flv": f"https://main/{name}.flv",
        "sdk_params": _json.dumps(
            {"vbitrate": vbitrate, "resolution": resolution, "VCodec": "h264"}
        ),
    }
    if hls:
        main["hls"] = f"https://main/{name}/index.m3u8"
    entry = {"main": main}
    if backup:
        entry["backup"] = {"flv": f"https://backup/{name}.flv"}
    return entry


def test_paused_room_is_still_live():
    room = _sdk_room(3, {"hd": _variant("hd")}, [{"sdk_key": "hd", "level": 3}])
    api = build_api({"data": [{"alive": True}], "status_code": 0}, room)
    assert api.is_room_alive("123") is True

    api = build_api(
        _sdk_room(3, {"hd": _variant("hd")}, [{"sdk_key": "hd", "level": 3}])
    )
    assert api.get_live_url_candidates("123") == ["https://main/hd.flv"]


def test_candidates_are_flv_only_best_real_quality_first_with_backups():
    data = {
        # TikTok's level says "hd" is best, but "uhd" publishes more bitrate.
        "hd": _variant("hd", 2_000_000, "720x1280"),
        "uhd": _variant("uhd", 4_000_000, "1080x1920"),
        "ld": _variant("ld", 600_000, "360x640"),
        "ao": _variant("ao", 0, "", backup=False, hls=False),
    }
    qualities = [
        {"sdk_key": "hd", "level": 5},
        {"sdk_key": "uhd", "level": 4},
        {"sdk_key": "ld", "level": 1},
    ]
    api = build_api(_sdk_room(2, data, qualities))

    urls = api.get_live_url_candidates("123")

    assert urls == [
        "https://main/uhd.flv",
        "https://main/hd.flv",
        "https://main/ld.flv",
        "https://main/ao.flv",  # audio-only last
    ]
    assert not any(u.endswith(".m3u8") for u in urls)
    assert api.alternate_url("https://main/uhd.flv") == "https://backup/uhd.flv"
    assert api.alternate_url("https://backup/uhd.flv") == "https://main/uhd.flv"
    assert api.alternate_url("https://main/ao.flv") is None
    assert api.stream_info["https://main/uhd.flv"] == {
        "quality": "uhd",
        "vbitrate": 4_000_000,
        "width": 1080,
        "height": 1920,
        "codec": "h264",
        "cdn": "main",
    }
    assert api.stream_info["https://backup/hd.flv"]["cdn"] == "backup"


def test_stream_connection_has_a_read_timeout():
    from core import tiktok_api

    calls = []

    class _Response:
        def iter_content(self, chunk_size):
            yield b"FLV"

        def close(self):
            calls.append("closed")

    class _Session:
        def get(self, url, **kwargs):
            calls.append(kwargs)
            return _Response()

    api = TikTokAPI.__new__(TikTokAPI)
    api._http_client_stream = _Session()

    assert list(api.download_live_stream("https://cdn/x.flv")) == [b"FLV"]
    assert calls[0] == {"stream": True, "timeout": tiktok_api.STREAM_TIMEOUT}
    assert calls[-1] == "closed"
