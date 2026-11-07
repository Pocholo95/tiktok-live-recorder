"""
Loads a creator's learned live schedule (core/schedule.py) from the DB,
cached for a few minutes: the dashboard asks for it every 3 s per channel
and workers before every wait, but it only changes when a live starts/ends.
"""

import threading
import time
from datetime import datetime, timezone

from core import schedule

CACHE_SECONDS = 300
_cache = {}  # username -> (computed_at, schedule)
_lock = threading.Lock()


def _spans(conn, username, now):
    rows = conn.execute(
        """
        SELECT started_at, ended_at, duration_seconds, status FROM recordings
        WHERE username = ? AND status IN ('completed', 'recording')
        """,
        (username,),
    ).fetchall()
    spans = []
    for row in rows:
        start = schedule.parse_utc(row["started_at"])
        if start is None:
            continue
        end = schedule.parse_utc(row["ended_at"])
        if end is None and row["status"] == "recording":
            end = now
        if end is None and row["duration_seconds"]:
            end = datetime.fromtimestamp(
                start.timestamp() + row["duration_seconds"], timezone.utc
            )
        if end is not None:
            spans.append((start, end))
    return spans


def _observed_since(conn, username):
    row = conn.execute(
        "SELECT created_at FROM channels WHERE username = ?", (username,)
    ).fetchone()
    return schedule.parse_utc(row["created_at"]) if row else None


def get(conn, username, now=None, use_cache=True):
    now = now or datetime.now(timezone.utc)
    if use_cache:
        with _lock:
            cached = _cache.get(username)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]

    result = schedule.compute(
        _spans(conn, username, now), _observed_since(conn, username), now=now
    )
    with _lock:
        _cache[username] = (time.monotonic(), result)
    return result


def invalidate(username=None):
    with _lock:
        if username is None:
            _cache.clear()
        else:
            _cache.pop(username, None)


def effective_interval(conn, channel_row, now=None):
    """
    (minutes, reason) the channel actually waits between checks right now.
    reason: "fixed" (smart interval off), "learning", "usual", "unusual".
    """
    base = channel_row["automatic_interval"]
    if not channel_row["smart_interval"]:
        return base, "fixed"
    now = now or datetime.now(timezone.utc)
    return schedule.smart_interval(base, get(conn, channel_row["username"], now), now)
