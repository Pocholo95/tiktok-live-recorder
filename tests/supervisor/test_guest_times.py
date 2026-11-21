import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import participants_repo, recordings_repo, settings_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from supervisor import guest_times  # noqa: E402

STARTED = "2026-11-10 20:00:00"
STARTED_EPOCH = 1_794_340_800  # STARTED as epoch seconds
LATENCY = 2.0


def _pts_bytes(pts):
    return bytes(
        [
            0x21 | ((pts >> 29) & 0x0E),
            (pts >> 22) & 0xFF,
            0x01 | ((pts >> 14) & 0xFE),
            (pts >> 7) & 0xFF,
            0x01 | ((pts << 1) & 0xFE),
        ]
    )


def _frame(pts, wall_ms, pid=256):
    """One video PES (SEI with a capture time + filler) as TS packets."""
    body = b"\x00\x00\x01\x06" + b'{"infos":[],"ts":%d}' % wall_ms
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + _pts_bytes(pts) + body
    pes += b"\xff" * (-len(pes) % 184 + 184 * 3)
    packets = bytearray()
    for i in range(0, len(pes), 184):
        flags = 0x40 if i == 0 else 0x00
        packets += bytes([0x47, flags | (pid >> 8), pid & 0xFF, 0x10])
        packets += pes[i : i + 184]
    return bytes(packets)


def _raw_recording(path, seconds, cuts):
    """A .ts whose capture times jump by each cut's gap at its position
    (cuts: {video_second: seconds lost}), one frame per video second."""
    lost = 0.0
    with open(path, "wb") as f:
        for second in range(seconds):
            lost += cuts.get(second, 0)
            wall = STARTED_EPOCH + LATENCY + second + lost
            f.write(_frame(126_000 + second * 90_000, int(wall * 1000)))
    return path


@pytest.fixture(autouse=True)
def small_windows(monkeypatch):
    monkeypatch.setattr(guest_times, "WINDOW_BYTES", 188 * 12)


def test_finds_each_cut_and_how_long_it_was(tmp_path):
    path = _raw_recording(tmp_path / "a.ts", 4000, {1000: 120, 3000: 180})

    cuts = guest_times.find_cuts(path, STARTED_EPOCH)

    assert len(cuts) == 2
    (first, gap1), (second, gap2) = cuts
    assert abs(first - 1000) <= 3 and abs(gap1 - 120) < 1
    assert abs(second - 3000) <= 3 and abs(gap2 - 300) < 1


def test_recording_without_cuts_needs_nothing(tmp_path):
    path = _raw_recording(tmp_path / "a.ts", 2000, {})
    assert guest_times.find_cuts(path, STARTED_EPOCH) == []


def test_file_without_capture_times_is_unreadable(tmp_path):
    path = tmp_path / "a.ts"
    path.write_bytes(b"\x47" + b"\x00" * 187 * 50)
    assert guest_times.find_cuts(path, STARTED_EPOCH) is None


def test_wall_times_map_back_onto_the_video():
    cuts = [(1000.0, 120.0), (3000.0, 300.0)]
    assert guest_times.wall_to_video(500, cuts) == 500  # before any cut
    assert guest_times.wall_to_video(1300, cuts) == 1180
    assert guest_times.wall_to_video(1050, cuts) == 1000  # during the wait
    assert guest_times.wall_to_video(3500, cuts) == 3200
    assert guest_times.wall_to_video(None, cuts) is None
    assert guest_times.wall_to_video(9000, cuts, duration=4000) == 4000


def _db(tmp_path):
    db_path = str(tmp_path / "hub.db")
    init_db(db_path)
    return get_connection(db_path)


def _recording(conn, path, fmt, started=STARTED, ended=None, duration=None):
    rec_id = recordings_repo.start(
        conn, channel_id=None, username="creator", file_path=str(path), format=fmt
    )
    conn.execute(
        "UPDATE recordings SET status = 'completed', started_at = ?, ended_at = ?,"
        " duration_seconds = ? WHERE id = ?",
        (started, ended, duration, rec_id),
    )
    conn.commit()
    return rec_id


def test_fix_existing_moves_old_raw_recordings_once(tmp_path):
    conn = _db(tmp_path)
    raw = _raw_recording(tmp_path / "old.ts", 3000, {1000: 120})
    old = _recording(conn, raw, "ts")
    participants_repo.replace_for_recording(
        conn,
        old,
        [
            {"username": "early", "start_seconds": 100, "end_seconds": 400},
            {"username": "late", "start_seconds": 1500, "end_seconds": None},
        ],
    )
    new = _recording(conn, raw, "ts", started="2026-11-21 10:00:00")
    participants_repo.replace_for_recording(
        conn, new, [{"username": "x", "start_seconds": 1500, "end_seconds": 1600}]
    )
    guest_times.mark_video_clock_start(conn, "2026-11-21 00:00:00")

    summary = guest_times.fix_existing(conn)

    assert summary["fixed"] == 1
    rows = {r["username"]: r for r in participants_repo.list_for_recording(conn, old)}
    assert rows["early"]["start_seconds"] == 100  # before the cut: unchanged
    assert abs(rows["late"]["start_seconds"] - 1380) <= 3
    assert rows["late"]["end_seconds"] is None
    # Recorded with the new clock: left alone.
    assert participants_repo.list_for_recording(conn, new)[0]["start_seconds"] == 1500
    # Second run doesn't shift it again.
    assert guest_times.fix_existing(conn)["fixed"] == 0
    rows = {r["username"]: r for r in participants_repo.list_for_recording(conn, old)}
    assert abs(rows["late"]["start_seconds"] - 1380) <= 3


def test_converted_recordings_are_only_counted(tmp_path):
    conn = _db(tmp_path)
    cut = _recording(
        conn, tmp_path / "a.mp4", "mp4", ended="2026-11-10 21:00:00", duration=3300
    )
    clean = _recording(
        conn, tmp_path / "b.mp4", "mp4", ended="2026-11-10 21:00:00", duration=3598
    )
    for rec_id in (cut, clean):
        participants_repo.replace_for_recording(
            conn, rec_id, [{"username": "g", "start_seconds": 10, "end_seconds": 20}]
        )
    guest_times.mark_video_clock_start(conn, "2026-11-21 00:00:00")

    summary = guest_times.fix_existing(conn)

    assert summary["converted_with_cut"] == 1 and summary["no_cut"] == 1
    assert participants_repo.list_for_recording(conn, cut)[0]["start_seconds"] == 10


def test_clock_start_is_only_set_once(tmp_path):
    conn = _db(tmp_path)
    guest_times.mark_video_clock_start(conn, "2026-11-21 00:00:00")
    guest_times.mark_video_clock_start(conn, "2026-12-01 00:00:00")
    value = settings_repo.get(conn, guest_times.VIDEO_CLOCK_SINCE_KEY)
    assert value == "2026-11-21 00:00:00"
