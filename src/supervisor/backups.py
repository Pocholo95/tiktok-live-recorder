"""
Daily copy of hub.db: clip marks, guests, schedules and settings are the
only things the hub can't rebuild from the video files.

Made with SQLite's online backup API (consistent even while the hub is
writing, WAL included) into `HUB_BACKUP_DIR` (default
`<OUTPUT_DIR>/backups`) as `hub-YYYY-MM-DD.db`, keeping the newest KEEP.
The default folder sits on the same disk as the recordings: it protects
from a bad write or a mistake, not from that disk dying - point
HUB_BACKUP_DIR at another disk for that.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from utils.logger_manager import logger

KEEP = 14
_PREFIX = "hub-"


def backup_dir(output_dir):
    return Path(os.environ.get("HUB_BACKUP_DIR") or Path(output_dir) / "backups")


def existing(folder):
    return sorted(Path(folder).glob(f"{_PREFIX}????-??-??.db"))


def run_if_due(db_path, folder, today=None):
    """Makes today's backup if it doesn't exist yet. Returns its path or None."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = Path(folder)
    target = folder / f"{_PREFIX}{today}.db"
    if target.exists():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".db.part")
    part.unlink(missing_ok=True)
    source = sqlite3.connect(db_path, timeout=30)
    try:
        dest = sqlite3.connect(part)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    os.replace(part, target)
    for old in existing(folder)[:-KEEP]:
        old.unlink(missing_ok=True)
    logger.info(f"Database backup saved: {target}")
    return target
