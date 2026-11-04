def format_hms(total_seconds):
    """Formats a number of seconds as HH:MM:SS for display."""
    total_seconds = int(round(total_seconds or 0))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_bytes(size):
    """Human-readable file size (1.4 GB, 820 MB...) for display."""
    if size is None:
        return "-"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024


def basename(path):
    """File name of a stored path, whichever OS separator it was saved with."""
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1]


def _parse_db_time(value):
    from datetime import datetime, timezone

    try:
        parsed = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    # SQLite's datetime('now') is UTC.
    return parsed.replace(tzinfo=timezone.utc)


_MONTHS = (
    "ene",
    "feb",
    "mar",
    "abr",
    "may",
    "jun",
    "jul",
    "ago",
    "sep",
    "oct",
    "nov",
    "dic",
)


def time_ago(value, now=None):
    """'hace 5 min', 'hace 3 h', 'ayer', 'hace 4 días' or '12 oct' for a DB timestamp."""
    from datetime import datetime, timezone

    then = _parse_db_time(value)
    if then is None:
        return ""
    now = now or datetime.now(timezone.utc)
    seconds = (now - then).total_seconds()
    if seconds < 60:
        return "hace un momento"
    if seconds < 3600:
        return f"hace {int(seconds // 60)} min"
    if seconds < 86400:
        return f"hace {int(seconds // 3600)} h"
    days = int(seconds // 86400)
    if days == 1:
        return "ayer"
    if days < 7:
        return f"hace {days} días"
    label = f"{then.day} {_MONTHS[then.month - 1]}"
    return label if then.year == now.year else f"{label} {then.year}"


def format_hours(total_seconds):
    """Compact duration for stats: '45 min', '3,5 h', '128 h'."""
    total_seconds = int(total_seconds or 0)
    if total_seconds < 3600:
        return f"{total_seconds // 60} min"
    hours = total_seconds / 3600
    return f"{hours:.1f} h".replace(".", ",") if hours < 10 else f"{hours:.0f} h"


def avatar_hue(username):
    """Stable color hue (0-359) per username for the initial-letter avatars."""
    return sum(ord(ch) * (i + 1) for i, ch in enumerate(str(username))) % 360
