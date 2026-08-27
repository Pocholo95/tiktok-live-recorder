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
    def convert_flv_to_mp4(file, bitrate=None, ffmpeg_path=None):
        """
        Produce the final MP4 from the raw FLV recording via a full re-encode.

        A straight remux (-c copy) isn't reliable here: TikTok's live
        HTTP-FLV connection can (and regularly does) drop and reconnect
        mid-recording, and each reconnect starts a brand-new FLV stream
        that gets appended to the same file - the result is a single file
        containing more than one FLV stream spliced together. `-c copy`
        either truncates at that splice point or carries the resulting
        invalid/discontinuous timestamps straight into the output,
        producing dropped frames or an outright broken container (verified
        empirically: even remuxing to a fresh container fails on a spliced
        capture). Decoding and re-encoding lets ffmpeg discard the corrupt
        packets at the splice and regenerate clean timestamps instead.

        Returns the path to the resulting .mp4 file, or None on failure -
        the raw .flv is kept (not deleted) whenever this fails, so nothing
        is lost and the conversion can be retried later.
        """
        logger.info("Converting {} to MP4 format...".format(file))

        if not VideoManagement.wait_for_file_release(file):
            logger.error(
                f"File {file} is still locked after waiting. Skipping conversion."
            )
            return None

        output_file = file.replace("_flv.flv", ".mp4")

        output_args = {
            "c:v": "libx264",
            "preset": "veryfast",
            "c:a": "aac",
            "b:a": "128k",
            "ac": 2,
            "avoid_negative_ts": "make_zero",
            "movflags": "+faststart",
            "y": "-y",
        }
        if bitrate:
            output_args["b:v"] = bitrate
        else:
            output_args["crf"] = 23

        try:
            ffmpeg.input(file, fflags="+genpts+discardcorrupt").output(
                output_file, **output_args
            ).run(quiet=True, cmd=ffmpeg_path or "ffmpeg")
        except ffmpeg.Error as e:
            logger.error(
                f"ffmpeg conversion to MP4 failed: "
                f"{e.stderr.decode() if hasattr(e, 'stderr') else str(e)}. "
                f"Keeping raw recording at {Path(file).resolve()}"
            )
            return None

        os.remove(file)
        logger.info(f"Finished converting {Path(output_file).resolve()}\n")
        return output_file
