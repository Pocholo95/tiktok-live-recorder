import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import stream_stats  # noqa: E402
from core.flv_stream import FlvNormalizer  # noqa: E402
from core.stream_stats import StreamStats  # noqa: E402

FLV_HEADER = b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00\x00\x00\x00"


def _tag(tag_type, ts, data):
    header = bytes([tag_type]) + len(data).to_bytes(3, "big")
    header += (ts & 0xFFFFFF).to_bytes(3, "big") + bytes([(ts >> 24) & 0xFF])
    header += b"\x00\x00\x00"
    return header + data + (11 + len(data)).to_bytes(4, "big")


def _video(ts):
    return _tag(9, ts, bytes([0x27, 1, 0, 0, 0]) + b"frame")


def _audio(ts):
    return _tag(8, ts, bytes([0xAF, 1]) + b"aac")


def _avc_config():
    return _tag(9, 0, bytes([0x17, 0, 0, 0, 0]) + b"avcC")


def _media(start_ms, end_ms, audio=True, step=500):
    out = b""
    for ts in range(start_ms, end_ms, step):
        out += _video(ts)
        if audio:
            out += _audio(ts + 10)
    return out


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_audio_gap_reconnects_and_config_changes_are_counted(tmp_path):
    clock = _Clock()
    stats = StreamStats(tmp_path / "TK_a.stats.json", clock=clock)
    n = FlvNormalizer(stats=stats)

    n.feed(FLV_HEADER + _avc_config() + _media(0, 10_000))
    n.feed(_media(10_000, 30_000, audio=False))  # audio stops, video goes on
    n.feed(_media(30_000, 40_000))
    clock.now += 90  # the reconnect took 90 s of wall clock
    n.new_connection()
    n.feed(FLV_HEADER + _avc_config() + _media(500, 5_000))  # unrelated ts

    stats.save(final=True)
    data = json.loads((tmp_path / "TK_a.stats.json").read_text(encoding="utf-8"))

    assert data["final"] is True
    assert data["audio"]["gaps"] == [[9.5, 30.0]]
    assert data["video"]["gaps"] == []
    assert [c["wall"] - data["started_wall"] for c in data["connections"]] == [0, 90]
    assert data["connections"][1]["at"] == data["connections"][0]["at"] + 39.5
    assert [c["type"] for c in data["config_changes"]] == ["video", "video"]
    assert data["minutes"][0][0] == 0 and data["minutes"][0][3] > 0
    assert stream_stats.summary(data) == {
        "audio_missing": 20,
        "video_missing": 0,
        "audio_gaps": 1,
        "video_gaps": 0,
        "reconnects": 1,
        "ffmpeg_restarts": 0,
    }


def test_stream_that_stops_or_never_arrives_is_an_open_gap(tmp_path):
    stats = StreamStats(tmp_path / "x.stats.json")
    n = FlvNormalizer(stats=stats)
    n.feed(FLV_HEADER + _media(0, 5_000) + _media(5_000, 20_000, audio=False))

    data = stats.to_dict()
    assert data["audio"]["gaps"] == [[4.5, None]]
    assert stream_stats.summary(data)["audio_missing"] == 15  # 4.5 -> 19.5

    silent = StreamStats(tmp_path / "y.stats.json")
    FlvNormalizer(stats=silent).feed(FLV_HEADER + _media(0, 10_000, audio=False))
    assert silent.to_dict()["audio"]["gaps"] == [[0.0, None]]


def test_save_is_throttled_and_discard_removes_the_file(tmp_path):
    clock = _Clock()
    path = tmp_path / "z.stats.json"
    stats = StreamStats(path, clock=clock)
    stats.maybe_save()
    assert not path.exists()
    clock.now += stream_stats.SAVE_EVERY_SECONDS
    stats.maybe_save()
    assert stream_stats.load(tmp_path / "z.ts")["final"] is False
    stats.discard()
    assert not path.exists()


def test_broken_stats_never_break_the_normalizer():
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    n = FlvNormalizer(stats=Broken())
    out = n.feed(FLV_HEADER + _media(0, 2_000))
    assert out.startswith(b"FLV") and n.media_seconds == 1.51
