import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.flv_stream import FlvNormalizer, TsRemuxer  # noqa: E402

FLV_HEADER = b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00\x00\x00\x00"


def _tag(tag_type, ts, data):
    header = bytes([tag_type]) + len(data).to_bytes(3, "big")
    header += (ts & 0xFFFFFF).to_bytes(3, "big") + bytes([(ts >> 24) & 0xFF])
    header += b"\x00\x00\x00"
    return header + data + (11 + len(data)).to_bytes(4, "big")


def _video(ts, keyframe=True):
    return _tag(9, ts, bytes([0x17 if keyframe else 0x27, 1, 0, 0, 0]) + b"frame")


def _avc_config(ts=0):
    return _tag(9, ts, bytes([0x17, 0, 0, 0, 0]) + b"avcC")


def _audio(ts):
    return _tag(8, ts, bytes([0xAF, 1]) + b"aac")


def _script(ts=0):
    return _tag(18, ts, b"\x02\x00\x0aonMetaData")


def _parse(stream):
    """[(tag_type, ts)] of a normalized FLV byte stream."""
    assert stream[:3] == b"FLV"
    pos = 13
    tags = []
    while pos < len(stream):
        assert stream[pos : pos + 3] != b"FLV", "a second FLV header leaked through"
        tag_type = stream[pos] & 0x1F
        size = int.from_bytes(stream[pos + 1 : pos + 4], "big")
        ts = int.from_bytes(stream[pos + 4 : pos + 7], "big") | (stream[pos + 7] << 24)
        tags.append((tag_type, ts))
        pos += 11 + size + 4
    assert pos == len(stream)
    return tags


def _feed_in_small_chunks(normalizer, data, size=7):
    out = bytearray()
    for i in range(0, len(data), size):
        out += normalizer.feed(data[i : i + size])
    return bytes(out)


def test_first_connection_timeline_starts_at_zero():
    n = FlvNormalizer()
    out = _feed_in_small_chunks(
        n,
        FLV_HEADER
        + _script()
        + _avc_config()
        + _video(5000)
        + _audio(5010)
        + _video(5033),
    )

    assert _parse(out) == [(18, 0), (9, 0), (9, 0), (8, 10), (9, 33)]


def test_reconnect_drops_second_header_and_continues_timeline():
    n = FlvNormalizer()
    first = FLV_HEADER + _avc_config() + _video(90_000) + _video(90_033)
    # A re-opened stream: new header, metadata, config, unrelated timestamps.
    second = FLV_HEADER + _script() + _avc_config() + _video(12) + _video(45)

    out = n.feed(first)
    n.new_connection()
    out += n.feed(second)

    tags = _parse(out)
    assert 18 not in [t for t, _ in tags]  # stale onMetaData dropped
    times = [ts for _, ts in tags]
    assert times == sorted(times)
    assert times[-1] == 33 + 40 + 33  # last + splice gap + one frame


def test_header_appended_without_new_connection_is_also_spliced():
    # Old raw files were written by blindly appending each connection.
    n = FlvNormalizer()
    data = (
        FLV_HEADER + _video(1000) + _video(1033) + FLV_HEADER + _video(7) + _video(40)
    )

    tags = _parse(_feed_in_small_chunks(n, data))

    assert [ts for _, ts in tags] == [0, 33, 73, 106]


def test_in_stream_timestamp_jump_is_closed():
    n = FlvNormalizer()
    data = FLV_HEADER + _video(0) + _video(33) + _video(600_000) + _video(600_033)

    tags = _parse(n.feed(data))

    assert [ts for _, ts in tags] == [0, 33, 73, 106]


def test_restart_prefix_has_header_and_latest_configs():
    n = FlvNormalizer()
    n.feed(
        FLV_HEADER + _avc_config() + _tag(8, 0, bytes([0xAF, 0]) + b"asc") + _video(0)
    )

    tags = _parse(n.restart_prefix())

    assert [t for t, _ in tags] == [9, 8]


def test_rejects_non_flv_stream():
    with pytest.raises(ValueError):
        FlvNormalizer().feed(b"<html>not a stream</html>")


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _probe_frames(path):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v",
            "-show_entries",
            "frame=width,height",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.strip(",") for line in out.split() if line]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_remuxer_keeps_every_frame_across_reconnect_with_resolution_change(tmp_path):
    for name, size, offset in (("a", "320x568", "1000"), ("b", "240x426", "5")):
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-g",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-output_ts_offset",
            offset,
            str(tmp_path / f"{name}.flv"),
        )

    out = tmp_path / "out.ts"
    remuxer = TsRemuxer(str(out))
    for i, name in enumerate(("a", "b")):
        if i:
            remuxer.new_connection()
        data = (tmp_path / f"{name}.flv").read_bytes()
        for j in range(0, len(data), 4096):
            remuxer.write(data[j : j + 4096])
    remuxer.close()

    frames = _probe_frames(out)
    assert frames.count("320,568") == 60
    assert frames.count("240,426") == 60
