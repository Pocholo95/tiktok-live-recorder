"""
Raw recordings kept after conversion.

When a converted MP4 is uploaded, the raw .ts it replaces is no longer
deleted right away: it stays where it was for `keep_days` (Settings, key
`keep_originals_days`, default 7, 0 = delete at once like before), so a
conversion that turns out broken (half the video without audio, the MP4 of
another live...) can still be checked or converted again. The recording row
remembers it in `original_path` / `original_kept_at`.

`cleanup()` (run hourly by the housekeeping thread) deletes the expired
ones, and also the oldest ones early whenever the recordings disk has less
than `min_free_bytes()` free - kept originals must never be what makes an
ongoing recording run out of space.
"""

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import recordings_repo, settings_repo
from utils.logger_manager import logger

KEEP_DAYS_KEY = "keep_originals_days"
KEEP_DAYS_DEFAULT = 7
KEEP_DAYS_MAX = 60
MIN_FREE_GB_DEFAULT = 30


def keep_days(conn):
    try:
        value = int(settings_repo.get(conn, KEEP_DAYS_KEY, KEEP_DAYS_DEFAULT))
    except (TypeError, ValueError):
        value = KEEP_DAYS_DEFAULT
    return max(0, min(KEEP_DAYS_MAX, value))


def min_free_bytes():
    try:
        gb = float(os.environ.get("HUB_ORIGINALS_MIN_FREE_GB", MIN_FREE_GB_DEFAULT))
    except ValueError:
        gb = MIN_FREE_GB_DEFAULT
    return int(gb * 1024**3)


def _parse_utc(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def expires_at(recording, days):
    """When this recording's kept original gets deleted (UTC), or None."""
    kept = _parse_utc(recording["original_kept_at"])
    return kept + timedelta(days=days) if kept else None


def size_of(recording):
    try:
        return Path(recording["original_path"]).stat().st_size
    except (OSError, TypeError):
        return 0


def available(recording):
    """The kept original of this recording, if it's still on disk."""
    path = recording["original_path"] if recording else None
    if path and Path(path).is_file():
        return Path(path)
    return None


def after_conversion(conn, recording_id, raw_path, final_path):
    """Keep (or, with 0 days, delete) the raw file a conversion replaced."""
    raw_path = Path(raw_path)
    if raw_path == Path(final_path) or not raw_path.exists():
        return
    if keep_days(conn) > 0:
        recordings_repo.keep_original(conn, recording_id, str(raw_path))
    else:
        raw_path.unlink(missing_ok=True)


def purge(conn, recording):
    path = recording["original_path"]
    if path:
        Path(path).unlink(missing_ok=True)
    recordings_repo.forget_original(conn, recording["id"])


def summary(conn):
    rows = recordings_repo.with_originals(conn)
    return {"count": len(rows), "bytes": sum(size_of(r) for r in rows)}


def cleanup(conn, output_dir, now=None, free_bytes=None):
    """Deletes expired originals, then oldest-first while the disk is low.
    Returns how many were deleted."""
    now = now or datetime.now(timezone.utc)
    days = keep_days(conn)
    deleted = 0
    remaining = []
    for row in recordings_repo.with_originals(conn):
        expiry = expires_at(row, days)
        if not Path(row["original_path"]).exists():
            recordings_repo.forget_original(conn, row["id"])  # deleted by hand
        elif days == 0 or expiry is None or expiry <= now:
            purge(conn, row)
            deleted += 1
        else:
            remaining.append(row)

    if free_bytes is None:
        try:
            free_bytes = shutil.disk_usage(output_dir).free
        except OSError:
            free_bytes = None
    if free_bytes is not None:
        needed = min_free_bytes()
        for row in remaining:
            if free_bytes >= needed:
                break
            size = size_of(row)
            purge(conn, row)
            free_bytes += size
            deleted += 1
            logger.info(
                f"Low disk: deleted the kept original of recording #{row['id']} early"
            )
    if deleted:
        logger.info(f"Deleted {deleted} kept original recording(s)")
    return deleted
