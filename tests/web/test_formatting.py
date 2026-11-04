import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from web.formatting import format_hms  # noqa: E402


def test_format_hms_pads_to_two_digits():
    assert format_hms(5) == "00:00:05"


def test_format_hms_handles_minutes_and_hours():
    assert format_hms(65) == "00:01:05"
    assert format_hms(3665) == "01:01:05"


def test_format_hms_rounds_fractional_seconds():
    assert format_hms(12.6) == "00:00:13"


def test_format_hms_handles_none_and_zero():
    assert format_hms(None) == "00:00:00"
    assert format_hms(0) == "00:00:00"


def test_time_ago_buckets():
    from datetime import datetime, timezone

    from web.formatting import time_ago

    now = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
    assert time_ago("2026-11-03 11:59:30", now) == "hace un momento"
    assert time_ago("2026-11-03 11:45:00", now) == "hace 15 min"
    assert time_ago("2026-11-03 09:00:00", now) == "hace 3 h"
    assert time_ago("2026-11-02 10:00:00", now) == "ayer"
    assert time_ago("2026-10-30 12:00:00", now) == "hace 4 días"
    assert time_ago("2026-10-12 12:00:00", now) == "12 oct"
    assert time_ago("2025-10-12 12:00:00", now) == "12 oct 2025"
    assert time_ago(None, now) == ""


def test_format_hours():
    from web.formatting import format_hours

    assert format_hours(None) == "0 min"
    assert format_hours(45 * 60) == "45 min"
    assert format_hours(int(3.5 * 3600)) == "3,5 h"
    assert format_hours(128 * 3600) == "128 h"
