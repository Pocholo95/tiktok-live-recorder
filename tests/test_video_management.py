import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils.video_management import VideoManagement  # noqa: E402


def _has_ffmpeg():
    try:
        subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, check=True, timeout=5
        )
        subprocess.run(
            ["ffprobe", "-version"], capture_output=True, check=True, timeout=5
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
def test_convert_flv_to_mp4_keeps_keyframes_close_together(tmp_path):
    # libx264's default keyframe interval (~250 frames) makes the browser's
    # seek bar feel chunky on a several-minute recording - the conversion
    # must force a keyframe roughly every 2s regardless of source fps, so
    # scrubbing lands on a decodable frame almost anywhere on the timeline.
    input_path = tmp_path / "sample_flv.flv"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=6:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(input_path),
        ],
        check=True,
        capture_output=True,
    )

    output_path = VideoManagement.convert_flv_to_mp4(str(input_path))

    assert output_path is not None
    assert Path(output_path).exists()

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pict_type",
            "-of",
            "json",
            output_path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    frames = json.loads(probe.stdout)["frames"]
    keyframe_count = sum(1 for f in frames if f.get("pict_type") == "I")

    # A 6s clip with keyframes every ~2s should have at least 2-3 of them,
    # versus a single one at the start with the old default GOP size.
    assert keyframe_count >= 2
