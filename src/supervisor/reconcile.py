from pathlib import Path

from db import recordings_repo
from db.database import get_connection
from supervisor.thumbnails import generate_thumbnail
from utils.logger_manager import logger
from utils.video_management import VideoManagement


def recover_orphaned_recordings(db_path, ffmpeg_path=None):
    """
    Runs once at boot, before the supervisor starts any workers.

    A `recordings` row still marked 'recording' when the process starts
    can only be a leftover from a previous process that died mid-recording
    (a fresh process can't already be "in the middle" of one) - most
    commonly a container restart/crash while a stream was being recorded
    or converted. Finish converting the raw file if it's still on disk, or
    mark the row failed if it's gone, so it doesn't sit in the library
    forever claiming to still be recording.
    """
    conn = get_connection(db_path)
    try:
        orphans = conn.execute(
            "SELECT * FROM recordings WHERE status = 'recording'"
        ).fetchall()

        if not orphans:
            return

        logger.info(
            f"Recovering {len(orphans)} orphaned recording(s) from a previous run..."
        )

        for recording in orphans:
            file_path = recording["file_path"]
            logger.info(f"Recovering recording #{recording['id']}: {file_path}")

            if not file_path or not Path(file_path).is_file():
                recordings_repo.mark_failed(
                    conn,
                    recording["id"],
                    error_message="File missing after restart - recording lost",
                )
                continue

            final_path = VideoManagement.convert_flv_to_mp4(
                file_path, ffmpeg_path=ffmpeg_path
            )
            if final_path:
                thumbnail_path = generate_thumbnail(final_path, ffmpeg_path=ffmpeg_path)
                try:
                    file_size_bytes = Path(final_path).stat().st_size
                except OSError:
                    file_size_bytes = None
                recordings_repo.mark_completed(
                    conn,
                    recording["id"],
                    file_path=final_path,
                    format="mp4",
                    file_size_bytes=file_size_bytes,
                    thumbnail_path=thumbnail_path,
                )
            else:
                recordings_repo.mark_failed(
                    conn,
                    recording["id"],
                    error_message="Conversion failed while recovering after restart",
                )
    finally:
        conn.close()
