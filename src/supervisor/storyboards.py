"""
Seek-bar preview thumbnails ("storyboards") for converted MP4s.

For each MP4 a folder `<name>.storyboard/` next to the video holds JPEG
sheets of small frames (one every `interval` seconds, COLS x ROWS per
sheet) plus `meta.json` describing the grid. The library player reads it
to show a frame while hovering/dragging the seek bar.

Normally the user's PC makes them right after converting, from the MP4 it
just made (`ffmpeg_args`, sent by /api/convert/args), and uploads them as
a zip after the MP4 (`install_uploaded`). They used to be a second output
of the conversion itself, but that follows the RAW file's timestamps: when
TikTok's audio jumps (seen on recording 42, 2026-11-20) the MP4 gets
padded, a filter reinit drops a half-filled sheet, and the thumbnails
drifted minutes away from the video. The background worker below is the
fallback for MP4s that arrive without them (uploaded by hand, older ones):
built one video at a time, at low priority: only
keyframes are decoded (`-skip_frame nokey`, our MP4s have one every 2 s),
so a 3 h live takes about a minute of CPU. Recording never re-encodes, so
it isn't affected. A failure only means no previews for that video.
"""

import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from utils.logger_manager import logger

TILE_HEIGHT = 160
COLS = 10
ROWS = 10
MAX_TILES = 1000
MIN_INTERVAL = 2  # the converted MP4s have a keyframe every 2 s
RESCAN_SECONDS = 600
# A fresh MP4 gets this long for its storyboard to be uploaded by the PC
# that converted it before the hub builds one itself.
UPLOAD_GRACE_SECONDS = 15 * 60
MAX_SHEETS = 20
MAX_SHEET_BYTES = 8 * 1024 * 1024
_SHEET_NAME = re.compile(r"^sheet_(\d{3})\.jpg$")


def _settled(video_path):
    """The MP4 exists and is old enough that its upload grace is over."""
    try:
        age = time.time() - Path(video_path).stat().st_mtime
    except OSError:
        return False
    return age >= UPLOAD_GRACE_SECONDS


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


def ffmpeg_args(input_path, pattern, interval):
    """
    ffmpeg arguments (without the executable) that write the storyboard
    sheets of a finished MP4 to `pattern` (".../sheet_%03d.jpg"). Only
    keyframes are decoded (one every 2 s in our MP4s). Used by the hub's
    worker and, via /api/convert/args, by the converting PC - always on
    the MP4 itself, so the thumbnails follow the video's own timeline.
    """
    return [
        "-skip_frame", "nokey", "-i", str(input_path),
        "-an", "-sn",
        "-vf", f"fps=1/{interval},scale=-2:{TILE_HEIGHT},tile={COLS}x{ROWS}",
        "-q:v", "6", str(pattern),
    ]  # fmt: skip


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
        "-threads", "2",
        *ffmpeg_args(video_path, work / "sheet_%03d.jpg", interval),
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


def install_uploaded(video_path, zip_path, interval, duration, ffprobe_path="ffprobe"):
    """
    Installs storyboard sheets uploaded as a zip (made by the PC while
    converting). Returns (meta, None) or (None, reason) - on any problem
    nothing is changed.
    """
    video_path = Path(video_path)
    if not 1 <= interval <= 3600:
        return None, "Intervalo inválido."
    final = storyboard_dir(video_path)
    work = final.with_name(final.name + ".upload")
    shutil.rmtree(work, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            entries = [e for e in archive.infolist() if not e.is_dir()]
            numbers = []
            for entry in entries:
                match = _SHEET_NAME.match(Path(entry.filename).name)
                if not match or entry.file_size > MAX_SHEET_BYTES:
                    return None, f"Archivo inesperado en el zip: {entry.filename}"
                numbers.append(int(match.group(1)))
            if not numbers or len(numbers) > MAX_SHEETS:
                return None, "El zip no trae hojas de miniaturas."
            if sorted(numbers) != list(range(1, len(numbers) + 1)):
                return None, "Faltan hojas de miniaturas."
            work.mkdir(parents=True)
            for entry, number in zip(entries, numbers, strict=True):
                data = archive.read(entry)
                if data[:3] != b"\xff\xd8\xff":
                    raise ValueError(f"{entry.filename} no es un JPEG")
                (work / f"sheet_{number:03d}.jpg").write_bytes(data)
        width, height = _probe_size(work / "sheet_001.jpg", ffprobe_path)
        if not width or not height:
            raise ValueError("no se pudo leer el tamaño de las hojas")
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as e:
        shutil.rmtree(work, ignore_errors=True)
        return None, f"Miniaturas inválidas: {e}"

    meta = {
        "interval": interval,
        "cols": COLS,
        "rows": ROWS,
        "tile_width": width // COLS,
        "tile_height": height // ROWS,
        "count": min(
            math.ceil((duration or 0) / interval) or len(numbers) * COLS * ROWS,
            len(numbers) * COLS * ROWS,
        ),
        "sheets": len(numbers),
    }
    (work / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    shutil.rmtree(final, ignore_errors=True)
    work.rename(final)
    return meta, None


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
            and _settled(r["file_path"])
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
