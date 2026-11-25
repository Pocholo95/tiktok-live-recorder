"""
Each person's @ on their panel, burned into the MP4 when converting
(optional; user request 2026-11-21, tried on recording 44 first).

The panel layout comes from TikTok's mix-layout SEI (core/layout.py):
- recordings made since then have `<recording>.layout.jsonl`, written while
  recording (`LayoutTimeline`);
- older raw .ts files are scanned once (`scan_ts`, about a second of
  reading per minute of video on the NAS) and the result is cached in the
  same file, so later requests are instant.
Names: the recording's own creator for the host panel, and each guest
panel's cid -> @handle from the `"panels"` lines of `.participants.jsonl`.
A panel without a known name gets no label - never someone else's.

The output is an ASS subtitle file whose coordinates are fractions of the
SOURCE frame (PlayRes = the source size), applied with ffmpeg's `ass`
filter BEFORE the scale/pad, so a resolution change mid-live keeps every
label on its panel. Labels only show while two or more panels are on
screen.
"""

import json
import re
from pathlib import Path

from core import stream_stats
from core.layout import SILENCE_SECONDS, all_grids, extract_app_data
from supervisor.guest_times import TS_PACKET, _video_pes

_PTS_WRAP = 1 << 33
_CHUNK = TS_PACKET * 40_000  # ~7.5 MB, keeps packets aligned
_SAFE_NAME = re.compile(r"[^\w.\-]")


def timeline_path(recording_path):
    return Path(recording_path).with_suffix(".layout.jsonl")


def read_timeline(path):
    lines = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(line, dict) and "t" in line:
                    lines.append(line)
    except OSError:
        return []
    return lines


def scan_ts(path):
    """Layout changes read from a raw .ts: [{"t", "grids"}] (same format as
    the recorder writes), with "grids": [] where the mix stops."""
    lines = []
    last_grids = None
    base = None
    last_sample = None
    with open(path, "rb") as f:
        while True:
            data = f.read(_CHUNK)
            if not data:
                break
            for pts, payload in _video_pes(data):
                if base is None:
                    base = pts
                if pts < base:
                    pts += _PTS_WRAP
                t = (pts - base) / 90_000
                if last_sample is not None and 0 <= t - last_sample < 0.9:
                    continue
                app_data = extract_app_data(bytes(payload))
                if app_data is None:
                    continue
                if (
                    last_sample is not None
                    and last_grids
                    and t - last_sample >= SILENCE_SECONDS
                ):
                    lines.append({"t": round(last_sample + 1, 2), "grids": []})
                    last_grids = []
                last_sample = t
                grids = all_grids(app_data)
                if grids != last_grids:
                    lines.append({"t": round(t, 2), "grids": grids})
                    last_grids = grids
    if last_grids:
        lines.append({"t": round(last_sample + 1, 2), "grids": []})
    return lines


def ensure_timeline(recording):
    """The recording's layout timeline, scanning its raw .ts the first time
    if it was recorded before the timeline existed. [] when unknown."""
    path = timeline_path(recording["file_path"])
    if path.exists():
        return read_timeline(path)
    source = Path(recording["file_path"])
    if (
        recording["status"] != "completed"
        or source.suffix.lower() != ".ts"
        or not source.exists()
    ):
        return []
    lines = scan_ts(source)
    part = path.with_suffix(".jsonl.part")
    part.write_text("".join(json.dumps(line) + "\n" for line in lines), "utf-8")
    part.replace(path)  # cached, even when empty (a solo live)
    return lines


def cid_names(recording_path):
    """{cid: @handle} from the "panels" lines of .participants.jsonl."""
    names = {}
    path = Path(recording_path).with_suffix(".participants.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                for cid, panel in (line.get("panels") or {}).items():
                    if isinstance(panel, dict) and panel.get("name"):
                        names[str(cid)] = panel["name"]
    except OSError:
        pass
    return names


def _ass_time(seconds):
    centis = int(round(max(0.0, seconds) * 100))
    hours, centis = divmod(centis, 360_000)
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def build_ass(timeline, names, host_name, width=720, height=1440):
    """(ASS text, number of labels). `names` maps cid -> @handle."""
    width, height = int(width or 720), int(height or 1440)
    font = max(12, round(height * 0.02))
    margin = round(height * 0.007)
    events = []
    for i, line in enumerate(timeline):
        grids = line.get("grids") or []
        if len(grids) < 2:
            continue
        start = float(line["t"])
        end = float(timeline[i + 1]["t"]) if i + 1 < len(timeline) else start + 1
        if end <= start:
            continue
        for grid in grids:
            name = host_name if grid.get("host") else names.get(str(grid.get("cid")))
            name = _SAFE_NAME.sub("", (name or "").lstrip("@"))
            if not name:
                continue
            # Bottom-right corner of the panel, like TikTok's own name tags
            # (user request 2026-11-25).
            x, y, w, h = grid["box"]
            right = round((x + w) * width - margin)
            bottom = round((y + h) * height - margin)
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Name,,0,0,0,,"
                f"{{\\pos({right},{bottom})}}"
                f"@{name}"
            )
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        # Bold white on a translucent black box, anchored bottom-right (3).
        f"Style: Name,Arial,{font},&H00FFFFFF,&H00FFFFFF,&H70000000,&H70000000,"
        f"1,0,0,0,100,100,0,0,3,{max(2, round(font * 0.2))},0,3,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ]
    return "\n".join(header + events) + "\n", len(events)


def _source_size(recording_path):
    """The first connection's resolution (stream stats), or None."""
    stats = stream_stats.load(recording_path) or {}
    for source in stats.get("sources") or []:
        if source.get("width") and source.get("height"):
            return source["width"], source["height"]
    return None


def overlay_for(recording):
    """(ASS text, number of labels) for a recording; scans if needed."""
    timeline = ensure_timeline(recording)
    size = _source_size(recording["file_path"]) or (720, 1440)
    return build_ass(
        timeline,
        cid_names(recording["file_path"]),
        recording["username"],
        *size,
    )
