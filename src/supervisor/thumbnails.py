from pathlib import Path

import ffmpeg

from utils.logger_manager import logger


def generate_thumbnail(video_path, ffmpeg_path=None):
    """
    Grabs a single frame from the finished recording to use as a library
    thumbnail. Tries a few seconds in first, then falls back to the very
    start for clips too short for that.

    Returns the thumbnail path, or None if ffmpeg couldn't produce one.
    """
    thumbnail_path = str(Path(video_path).with_suffix(".jpg"))

    for seek_seconds in (5, 0.5):
        try:
            ffmpeg.input(video_path, ss=seek_seconds).output(
                thumbnail_path, vframes=1, y="-y"
            ).run(quiet=True, cmd=ffmpeg_path or "ffmpeg")
        except ffmpeg.Error as e:
            logger.warning(
                f"Thumbnail generation failed at {seek_seconds}s for "
                f"{video_path}: {e.stderr.decode() if hasattr(e, 'stderr') else e}"
            )
            continue

        if Path(thumbnail_path).exists():
            return thumbnail_path

    return None
