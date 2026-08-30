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
