import os
import time
from pathlib import Path

import ffmpeg

from utils.logger_manager import logger


class VideoManagement:
    @staticmethod
    def wait_for_file_release(file, timeout=10):
        """
        Wait until the file is released (not locked anymore) or timeout is reached.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                with open(file, "ab"):
                    return True
            except PermissionError:
                time.sleep(0.5)
        return False

    @staticmethod
    def convert_flv_to_mkv(file, ffmpeg_path=None):
        """
        Remux the raw FLV recording into a Matroska (.mkv) master file.

        Unlike MP4 (whose index/moov atom is written last and is lost if the
        process is interrupted mid-write), MKV stays playable even if this
        step gets cut short, so it's used as the crash-safe intermediate
        before producing the final MP4.

        Returns the path to the resulting .mkv file, or None on failure.
        """
        logger.info("Remuxing {} to MKV format...".format(file))

        if not VideoManagement.wait_for_file_release(file):
            logger.error(f"File {file} is still locked after waiting. Skipping remux.")
            return None

        output_file = file.replace("_flv.flv", ".mkv")

        try:
            ffmpeg.input(file).output(output_file, c="copy", y="-y").run(
                quiet=True, cmd=ffmpeg_path or "ffmpeg"
            )
        except ffmpeg.Error as e:
            logger.error(
                f"ffmpeg remux to MKV failed: {e.stderr.decode() if hasattr(e, 'stderr') else str(e)}"
            )
            return None

        os.remove(file)
        return output_file

    @staticmethod
    def convert_mkv_to_mp4(file, bitrate=None, ffmpeg_path=None):
        """
        Produce the final MP4 from the MKV master.

        This is a quick remux (or transcode, if a bitrate is given), so the
        window during which it could be interrupted is short. If it fails,
        the MKV master is kept instead of being deleted, so nothing is lost.
        """
        logger.info("Converting {} to MP4 format...".format(file))

        output_args = {
            "c": "copy",
            "y": "-y",
        }
        output_file = file.replace(".mkv", ".mp4")

        if bitrate:
            output_args["b:v"] = bitrate
            del output_args["c"]
            output_args["c:v"] = "libx264"
            output_args["c:a"] = "copy"

        try:
            ffmpeg.input(file).output(output_file, **output_args).run(
                quiet=True, cmd=ffmpeg_path or "ffmpeg"
            )
        except ffmpeg.Error as e:
            logger.error(
                f"ffmpeg conversion to MP4 failed: "
                f"{e.stderr.decode() if hasattr(e, 'stderr') else str(e)}. "
                f"Keeping MKV master at {Path(file).resolve()}"
            )
            return None

        os.remove(file)
        logger.info(f"Finished converting {Path(output_file).resolve()}\n")
        return output_file
