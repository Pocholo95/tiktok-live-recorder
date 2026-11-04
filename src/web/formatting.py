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
