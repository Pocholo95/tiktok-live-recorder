"""
One-off fix for guest time ranges saved before they followed the video.

Until 2026-11-20 `ParticipantTracker` measured wall-clock seconds since the
recording started. When the reconnect grace spliced a creator's return into
the same file, the video had no gap but the clock kept running, so every
guest range after a cut was late by the time spent waiting.

Raw recordings can be fixed exactly: TikTok stamps the video with H.264 SEI
messages carrying the capture time in epoch ms (`"ts":17...`). Sampling
those along the file gives the wall-clock offset at each point of the
video; a jump in that offset is a cut, and its size is the time lost.
Converted MP4s lost the SEI in the re-encode, so they can't be located
(they are only counted in the log).

Runs once per recording, in the background at hub startup, only for
recordings started before the new tracker shipped (settings key
VIDEO_CLOCK_SINCE_KEY marks that moment).
"""

import json
import re
from pathlib import Path

from core.schedule import parse_utc
from db import participants_repo, settings_repo
from utils.logger_manager import logger

VIDEO_CLOCK_SINCE_KEY = "guest_times_video_clock_since"
FIXED_KEY = "guest_times_fixed"

TS_PACKET = 188
WINDOW_BYTES = 1_500_000
SAMPLES = 48
CUT_THRESHOLD = 5.0  # seconds of offset jump that count as a cut
MAX_LATENCY = 30.0  # a stream delay above this means the start went unseen
DEFAULT_LATENCY = 3.0
_PTS_WRAP = 1 << 33
_SEI_TS = re.compile(rb'"ts":(1\d{12})')


def _pts(header):
    return (
        ((header[0] >> 1) & 0x07) << 30
        | header[1] << 22
        | (header[2] >> 1) << 15
        | header[3] << 7
        | header[4] >> 1
    )


def _video_pes(data):
    """[(pts_90khz, payload)] of the video PES packets in a chunk of TS."""
    start = 0
    while start + 2 * TS_PACKET < len(data) and not (
        data[start] == 0x47 and data[start + TS_PACKET] == 0x47
    ):
        start += 1
    open_pes = {}  # pid -> [pts, bytearray]
    done = []
    for p in range(start, len(data) - TS_PACKET + 1, TS_PACKET):
        if data[p] != 0x47:
            continue
        pid = ((data[p + 1] & 0x1F) << 8) | data[p + 2]
        pusi = data[p + 1] & 0x40
        afc = (data[p + 3] >> 4) & 0x03
        if afc in (0, 2):
            continue
        i = p + 4
        if afc == 3:
            i += 1 + data[i]
        payload = data[i : p + TS_PACKET]
        if pusi:
            if pid in open_pes:
                done.append(open_pes.pop(pid))
            if (
                len(payload) >= 14
                and payload[:3] == b"\x00\x00\x01"
                and 0xE0 <= payload[3] <= 0xEF
                and payload[7] & 0x80
            ):
                body = payload[9 + payload[8] :]
                open_pes[pid] = [_pts(payload[9:14]), bytearray(body)]
        elif pid in open_pes:
            open_pes[pid][1] += payload
    done.extend(open_pes.values())
    return done


def _read(path, offset):
    with open(path, "rb") as f:
        f.seek(max(0, offset))
        return f.read(WINDOW_BYTES)


def _first_pts(path):
    for pts, _payload in _video_pes(_read(path, 0)):
        return pts
    return None


def _sample(path, offset, base_pts):
    """(video seconds, TikTok capture time in s) near `offset`, or None."""
    for pts, payload in _video_pes(_read(path, offset)):
        match = _SEI_TS.search(payload)
        if match:
            if pts < base_pts:
                pts += _PTS_WRAP
            return (pts - base_pts) / 90_000, int(match.group(1)) / 1000
    return None


def find_cuts(path, started_epoch, samples=SAMPLES):
    """
    Cuts in a raw recording as [(video_seconds, total_gap_after)], gaps
    growing. Empty when there were none, None when the file has no
    readable capture times at all.
    """
    path = Path(path)
    size = path.stat().st_size
    base = _first_pts(path)
    if base is None:
        return None

    def probe(offset):
        found = _sample(path, offset, base)
        if found is None:
            return None
        pos, wall = found
        return offset, pos, wall - started_epoch - pos  # latency + gap so far

    points = [probe(int(size * k / samples)) for k in range(samples)]
    points = [p for p in points if p is not None]
    if not points:
        return None
    lowest = min(p[2] for p in points)
    latency = lowest if lowest < MAX_LATENCY else DEFAULT_LATENCY

    cuts = []
    level = 0.0
    previous = points[0]
    for point in points:
        gap = max(0.0, point[2] - latency)
        if gap > level + CUT_THRESHOLD:
            left, right = previous, point
            for _ in range(12):  # narrow down where the jump happens
                if right[1] - left[1] < 3 or right[0] - left[0] < 2 * WINDOW_BYTES:
                    break
                mid = probe((left[0] + right[0]) // 2)
                if mid is None:
                    break
                if max(0.0, mid[2] - latency) > level + CUT_THRESHOLD:
                    right = mid
                else:
                    left = mid
            cuts.append((round((left[1] + right[1]) / 2, 1), round(gap, 1)))
            level = gap
        previous = point
    return cuts


def wall_to_video(seconds, cuts, duration=None):
    """Old wall-clock guest time -> position in the video."""
    if seconds is None:
        return None
    segments = [(0.0, 0.0), *cuts]
    for k, (start, gap) in enumerate(segments):
        next_start = segments[k + 1][0] if k + 1 < len(segments) else None
        video = seconds - gap
        if next_start is None or video < next_start:
            video = max(start, video)  # inside a cut: where the video resumes
            if duration:
                video = min(video, duration)
            return round(video, 1)
    return seconds


def _fixed_ids(conn):
    try:
        return set(json.loads(settings_repo.get(conn, FIXED_KEY, "[]")))
    except ValueError:
        return set()


def mark_video_clock_start(conn, now_utc):
    """Remembers when guest times started following the video (first boot
    of this version). Recordings from before then are the ones to fix."""
    if settings_repo.get(conn, VIDEO_CLOCK_SINCE_KEY) is None:
        settings_repo.set(conn, VIDEO_CLOCK_SINCE_KEY, now_utc)


def fix_existing(conn):
    """Fixes every old recording once. Returns a summary dict."""
    since = settings_repo.get(conn, VIDEO_CLOCK_SINCE_KEY)
    summary = {"fixed": 0, "no_cut": 0, "converted_with_cut": 0, "unreadable": 0}
    if since is None:
        return summary
    done = _fixed_ids(conn)
    rows = conn.execute(
        """
        SELECT r.* FROM recordings r
        WHERE r.status = 'completed' AND r.started_at < ?
          AND EXISTS (SELECT 1 FROM recording_participants p
                      WHERE p.recording_id = r.id)
        ORDER BY r.id
        """,
        (since,),
    ).fetchall()
    for row in rows:
        if row["id"] in done:
            continue
        try:
            outcome = _fix_one(conn, row)
        except Exception as e:  # one bad file never stops the rest
            logger.warning(f"Guest times of recording #{row['id']} not fixed: {e}")
            outcome = "unreadable"
        summary[outcome] += 1
        done.add(row["id"])
        settings_repo.set(conn, FIXED_KEY, json.dumps(sorted(done)))
    if any(summary.values()):
        logger.info(f"Guest times of old recordings: {summary}")
    return summary


def _fix_one(conn, row):
    started = parse_utc(row["started_at"])
    path = Path(row["file_path"] or "")
    if row["format"] != "ts":
        # The MP4's duration is the real video length; the recording's wall
        # span is longer by however long the cuts took.
        ended = parse_utc(row["ended_at"])
        if started and ended and row["duration_seconds"]:
            lost = (ended - started).total_seconds() - row["duration_seconds"]
            if lost > CUT_THRESHOLD * 3:
                return "converted_with_cut"
        return "no_cut"
    if started is None or not path.is_file():
        return "unreadable"
    cuts = find_cuts(path, started.timestamp())
    if cuts is None:
        return "unreadable"
    if not cuts:
        return "no_cut"
    participants = [
        {
            "username": p["username"],
            "start_seconds": wall_to_video(p["start_seconds"], cuts),
            "end_seconds": wall_to_video(p["end_seconds"], cuts),
        }
        for p in participants_repo.list_for_recording(conn, row["id"])
    ]
    participants_repo.replace_for_recording(conn, row["id"], participants)
    logger.info(
        f"Guest times of recording #{row['id']} moved to the video clock "
        f"({len(cuts)} cut(s): {cuts})"
    )
    return "fixed"


def run_at_startup(db_path, get_connection, now_utc):
    """Background entry point: mark the switch, then fix the old ones."""
    conn = get_connection(db_path)
    try:
        mark_video_clock_start(conn, now_utc)
        fix_existing(conn)
    except Exception as e:
        logger.warning(f"Fixing old guest times failed: {e}")
    finally:
        conn.close()
