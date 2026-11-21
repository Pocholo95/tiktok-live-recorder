import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import recordings_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from supervisor import storyboards  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg not installed",
)


def _mp4(path, seconds=30, size="180x320"):
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
            "-g", "60", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def test_interval_keeps_tile_count_bounded():
    assert storyboards.interval_for(60) == 2  # never below the keyframe gap
    assert storyboards.interval_for(9594) == 10
    assert storyboards.interval_for(9594) * storyboards.MAX_TILES >= 9594


@needs_ffmpeg
def test_generate_builds_sheets_and_meta(tmp_path):
    video = _mp4(tmp_path / "TK_a.mp4", seconds=30)

    meta = storyboards.generate(video, 30)

    assert meta == storyboards.load_meta(video)
    assert meta["interval"] == 2 and meta["count"] == 15
    assert meta["tile_height"] == storyboards.TILE_HEIGHT
    assert meta["tile_width"] == 90  # 180x320 scaled to height 160
    assert (tmp_path / "TK_a.storyboard" / "sheet_001.jpg").is_file()
    assert not (tmp_path / "TK_a.storyboard.part").exists()

    storyboards.remove(video)
    assert storyboards.load_meta(video) is None


def test_generate_failure_leaves_nothing(tmp_path):
    video = tmp_path / "TK_bad.mp4"
    video.write_bytes(b"not a video")

    assert storyboards.generate(video, 30) is None
    assert not (tmp_path / "TK_bad.storyboard").exists()
    assert not (tmp_path / "TK_bad.storyboard.part").exists()


@needs_ffmpeg
def test_worker_builds_missing_and_skips_failures(tmp_path):
    db_path = tmp_path / "hub.db"
    init_db(db_path)
    conn = get_connection(db_path)
    good = _mp4(tmp_path / "TK_good.mp4", seconds=10)
    bad = tmp_path / "TK_bad.mp4"
    bad.write_bytes(b"x")
    for path in (good, bad):
        rid = recordings_repo.start(
            conn, channel_id=None, username="c", file_path=str(path)
        )
        recordings_repo.mark_completed(
            conn, rid, file_path=str(path), format="mp4", duration_seconds=10
        )
    raw = recordings_repo.start(
        conn, channel_id=None, username="c", file_path=str(tmp_path / "x.ts")
    )
    recordings_repo.mark_completed(
        conn, raw, file_path=str(tmp_path / "x.ts"), format="ts"
    )

    worker = storyboards.StoryboardWorker(str(db_path), get_connection)

    assert worker.run_once() == 1
    assert storyboards.load_meta(good) is not None
    assert worker.run_once() == 0  # done ones and failures aren't retried
