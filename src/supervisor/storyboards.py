"""
Seek-bar preview thumbnails ("storyboards") for converted MP4s.

For each MP4 a folder `<name>.storyboard/` next to the video holds JPEG
sheets of small frames (one every `interval` seconds, COLS x ROWS per
sheet) plus `meta.json` describing the grid. The library player reads it
to show a frame while hovering/dragging the seek bar.

Built in the background, one video at a time, at low priority: only
keyframes are decoded (`-skip_frame nokey`, our MP4s have one every 2 s),
so a 3 h live takes about a minute of CPU. Recording never re-encodes, so
it isn't affected. A failure only means no previews for that video.
"""

import json
import math
import os
import shutil
import subprocess
import threading
from pathlib import Path

from utils.logger_manager import logger

TILE_HEIGHT = 160
COLS = 10
ROWS = 10
MAX_TILES = 1000
MIN_INTERVAL = 2  # the converted MP4s have a keyframe every 2 s
RESCAN_SECONDS = 600


def storyboard_dir(video_path):
    return Path(video_path).with_suffix(".storyboard")


def interval_for(duration):
    return max(MIN_INTERVAL, math.ceil((duration or 0) / MAX_TILES))


def load_meta(video_path):
    try:
        return json.loads(
            (storyboard_dir(video_path) / "meta.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


def remove(video_path):
    shutil.rmtree(storyboard_dir(video_path), ignore_errors=True)


def _probe_size(path, ffprobe_path):
    out = subprocess.run(
        [
            ffprobe_path, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    stream = json.loads(out.stdout or "{}").get("streams", [{}])[0]
    return stream.get("width"), stream.get("height")


def generate(video_path, duration, ffmpeg_path="ffmpeg", ffprobe_path="ffprobe"):
    """Builds the storyboard for one MP4. Returns its meta dict, or None."""
    video_path = Path(video_path)
    if not duration or duration <= 0 or not video_path.is_file():
        return None
    final = storyboard_dir(video_path)
    work = final.with_name(final.name + ".part")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    interval = interval_for(duration)
    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-threads", "2", "-skip_frame", "nokey", "-i", str(video_path),
        "-an", "-sn",
        "-vf", f"fps=1/{interval},scale=-2:{TILE_HEIGHT},tile={COLS}x{ROWS}",
        "-q:v", "6", str(work / "sheet_%03d.jpg"),
    ]  # fmt: skip
    if shutil.which("nice"):
        cmd = ["nice", "-n", "15", *cmd]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=3600)
        sheets = sorted(work.glob("sheet_*.jpg"))
        if not sheets:
            raise RuntimeError("ffmpeg produced no sheets")
        width, height = _probe_size(sheets[0], ffprobe_path)
        if not width or not height:
            raise RuntimeError("could not read the sheet size")
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as e:
        logger.warning(f"Storyboard failed for {video_path.name}: {e}")
        shutil.rmtree(work, ignore_errors=True)
        return None

    meta = {
        "interval": interval,
        "cols": COLS,
        "rows": ROWS,
        "tile_width": width // COLS,
        "tile_height": height // ROWS,
        "count": math.ceil(duration / interval),
        "sheets": len(sheets),
    }
    (work / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    shutil.rmtree(final, ignore_errors=True)
    work.rename(final)
    return meta


class StoryboardWorker:
    """
    Background thread that builds missing storyboards, newest MP4 first
    (the ones most likely to be watched next).
    `wake()` after an upload makes it look again right away; otherwise it
    rescans every RESCAN_SECONDS (also picks up MP4s from before this
    feature existed).
    """

    def __init__(self, db_path, get_connection, ffmpeg_path=None, ffprobe_path=None):
        self._db_path = db_path
        self._get_connection = get_connection
        self._ffmpeg = ffmpeg_path or os.environ.get("FFMPEG_PATH", "ffmpeg")
        self._ffprobe = ffprobe_path or os.environ.get("FFPROBE_PATH", "ffprobe")
        self._wake = threading.Event()
        self._failed = set()  # recording ids that failed this run; not retried

    def start(self):
        threading.Thread(target=self._run, name="storyboards", daemon=True).start()

    def wake(self):
        self._wake.set()

    def _pending(self):
        conn = self._get_connection(self._db_path)
        try:
            rows = conn.execute(
                """
                SELECT id, file_path, duration_seconds FROM recordings
                WHERE status = 'completed' AND format = 'mp4'
                ORDER BY started_at DESC
                """
            ).fetchall()
        finally:
            conn.close()
        return [
            r
            for r in rows
            if r["id"] not in self._failed
            and r["file_path"]
            and load_meta(r["file_path"]) is None
            and Path(r["file_path"]).is_file()
        ]

    def run_once(self):
        """Builds every missing storyboard. Returns how many were built."""
        built = 0
        for row in self._pending():
            if generate(
                row["file_path"], row["duration_seconds"], self._ffmpeg, self._ffprobe
            ):
                built += 1
            else:
                self._failed.add(row["id"])
        return built

    def _run(self):
        while True:
            try:
                self.run_once()
            except Exception as e:  # never let the thread die
                logger.warning(f"Storyboard worker error: {e}")
            self._wake.wait(RESCAN_SECONDS)
            self._wake.clear()
