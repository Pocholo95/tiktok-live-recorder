"""
The one place that builds the ffmpeg command for converting a raw
recording on the user's PC. Used by the library page (command shown to
copy) and by the auto-convert script (args fetched from the API), so the
two can never drift apart.

Keep the `scale+pad+fps` video filter and `aresample=async=1`: they are
what fix TikTok's mid-stream resolution changes and the splice gaps.
"""

ENCODERS = {
    "h264_nvenc": "NVIDIA H.264 (compatible con todo)",
    "av1_nvenc": "NVIDIA AV1 (más liviano, RTX 40+)",
    "libx264": "CPU x264 (sin GPU, lento)",
}
SIZES = {
    "720x1280": "720×1280 vertical",
    "1080x1920": "1080×1920 vertical",
    "1280x720": "1280×720 horizontal",
}
QUALITIES = {"high": "Alta", "normal": "Normal", "light": "Liviana"}

_QUALITY_VALUE = {
    "h264_nvenc": {"high": 24, "normal": 28, "light": 32},
    "av1_nvenc": {"high": 30, "normal": 34, "light": 38},
    "libx264": {"high": 20, "normal": 23, "light": 27},
}

DEFAULTS = {"encoder": "h264_nvenc", "size": "720x1280", "quality": "normal"}


def normalize(encoder=None, size=None, quality=None):
    """Valid options, falling back to the defaults for anything unknown."""
    return {
        "encoder": encoder if encoder in ENCODERS else DEFAULTS["encoder"],
        "size": size if size in SIZES else DEFAULTS["size"],
        "quality": quality if quality in QUALITIES else DEFAULTS["quality"],
    }


def ffmpeg_args(input_path, output_path, options):
    """Argument list (without the `ffmpeg` executable itself)."""
    o = normalize(**options)
    width, height = o["size"].split("x")
    value = _QUALITY_VALUE[o["encoder"]][o["quality"]]
    if o["encoder"] == "libx264":
        video = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(value)]
    else:
        video = [
            "-c:v", o["encoder"], "-preset", "p5",
            "-rc", "vbr", "-cq", str(value), "-b:v", "0",
        ]  # fmt: skip
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p"
    )
    return [
        "-hide_banner",
        "-fflags", "+genpts+discardcorrupt",
        "-i", input_path,
        "-vf", video_filter,
        "-af", "aresample=async=1:first_pts=0",
        *video,
        "-g", "60",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
        output_path,
    ]  # fmt: skip


def _quote(arg):
    """Quote for display/pasting in PowerShell or cmd when needed."""
    if arg.startswith("$_") or arg.startswith("("):
        return arg  # PowerShell expressions in the folder loop
    if any(ch in arg for ch in ' (),"') or arg == "":
        return '"' + arg.replace('"', '\\"') + '"'
    return arg


def output_name(raw_name):
    stem = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name
    return stem + ".mp4"


def command_text(raw_name, options, scope="file"):
    """The command line shown on the library page."""
    if scope == "folder":
        args = ffmpeg_args("$_.FullName", '($_.BaseName + ".mp4")', options)
        return (
            "Get-ChildItem *.ts, *.flv | ForEach-Object { ffmpeg "
            + " ".join(_quote(a) for a in args)
            + " }"
        )
    args = ffmpeg_args(raw_name, output_name(raw_name), options)
    return "ffmpeg " + " ".join(_quote(a) for a in args)
