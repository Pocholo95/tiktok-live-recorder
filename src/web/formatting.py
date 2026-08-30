def format_hms(total_seconds):
    """Formats a number of seconds as HH:MM:SS for display."""
    total_seconds = int(round(total_seconds or 0))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
