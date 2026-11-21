"""
What actually arrived from TikTok while recording, saved next to the
recording as `<name>.stats.json`.

Counted at the FLV tag level in `FlvNormalizer` (no decoding), on the same
video timeline as the file: audio/video frames and bytes per minute, gaps
where one of them stopped arriving, reconnects (with how long each took on
the wall clock), codec config changes (a new resolution usually) and
ffmpeg remuxer restarts. It's there to answer "was it the recording or the
conversion?" when a converted video turns out to be broken - e.g. half of
it without audio - and it stays after the raw file is converted/deleted.

A muted microphone still sends (tiny, silent) AAC frames, so an audio gap
here means TikTok really stopped sending audio. Silence shows up instead as
a drop in the per-minute audio bytes.
"""

import json
import os
import time
from pathlib import Path

from utils.logger_manager import logger

AUDIO = 8
VIDEO = 9
# Frames this far apart on the video timeline count as a gap. AAC frames
# arrive every ~23 ms and video every ~33-66 ms, so this never trips on
# normal jitter.
GAP_MS = 2000
SAVE_EVERY_SECONDS = 60
_NAMES = {AUDIO: "audio", VIDEO: "video"}


def stats_path(recording_path):
    return Path(recording_path).with_suffix(".stats.json")


def load(recording_path):
    try:
        return json.loads(stats_path(recording_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def summary(stats):
    """Short view for the UI: total seconds missing per stream, cuts."""
    if not stats:
        return None
    end = stats.get("media_seconds") or 0

    def missing(kind):
        gaps = stats.get(kind, {}).get("gaps", [])
        return round(sum(max(0, (e if e is not None else end) - s) for s, e in gaps))

    return {
        "audio_missing": missing("audio"),
        "video_missing": missing("video"),
        "audio_gaps": len(stats.get("audio", {}).get("gaps", [])),
        "video_gaps": len(stats.get("video", {}).get("gaps", [])),
        "reconnects": max(0, len(stats.get("connections", [])) - 1),
        "ffmpeg_restarts": len(stats.get("ffmpeg_restarts", [])),
    }


class StreamStats:
    """Fed by FlvNormalizer/TsRemuxer; written with `save()` (atomically)."""

    def __init__(self, path, clock=time.time):
        self._path = Path(path)
        self._clock = clock
        self._started = clock()
        self._last_saved = self._started
        self._media_ms = 0
        self._streams = {
            kind: {"frames": 0, "bytes": 0, "last_ms": None, "gaps": []}
            for kind in (AUDIO, VIDEO)
        }
        self._minutes = {}  # minute -> [audio frames, audio bytes, video frames, video bytes]
        self._connections = []
        self._config_changes = []
        self._ffmpeg_restarts = []

    def connection(self):
        """A (re)connection to TikTok is about to feed bytes."""
        self._connections.append(
            {"at": round(self._media_ms / 1000, 1), "wall": round(self._clock(), 1)}
        )

    def config(self, tag_type, data):
        if tag_type not in _NAMES:
            return
        self._config_changes.append(
            {
                "type": _NAMES[tag_type],
                "at": round(self._media_ms / 1000, 1),
                "bytes": len(data),
            }
        )

    def frame(self, tag_type, out_ms, size):
        stream = self._streams.get(tag_type)
        if stream is None:
            return
        last = stream["last_ms"]
        if last is None:
            if out_ms > GAP_MS:  # this stream started late
                stream["gaps"].append([0.0, round(out_ms / 1000, 1)])
        elif out_ms - last > GAP_MS:
            stream["gaps"].append([round(last / 1000, 1), round(out_ms / 1000, 1)])
        stream["last_ms"] = out_ms if last is None else max(last, out_ms)
        stream["frames"] += 1
        stream["bytes"] += size
        self._media_ms = max(self._media_ms, out_ms)
        bucket = self._minutes.setdefault(out_ms // 60000, [0, 0, 0, 0])
        index = 0 if tag_type == AUDIO else 2
        bucket[index] += 1
        bucket[index + 1] += size

    def ffmpeg_restart(self, detail):
        self._ffmpeg_restarts.append(
            {"at": round(self._media_ms / 1000, 1), "detail": str(detail)[:300]}
        )

    def to_dict(self, final=False):
        end_ms = self._media_ms
        result = {
            "version": 1,
            "final": final,
            "started_wall": round(self._started, 1),
            "updated_wall": round(self._clock(), 1),
            "media_seconds": round(end_ms / 1000, 1),
        }
        for kind, name in _NAMES.items():
            stream = self._streams[kind]
            gaps = [list(g) for g in stream["gaps"]]
            last = stream["last_ms"]
            if last is None:
                if end_ms > GAP_MS:
                    gaps.append([0.0, None])  # never arrived (None = until the end)
            elif end_ms - last > GAP_MS:
                gaps.append([round(last / 1000, 1), None])  # stopped, never came back
            result[name] = {
                "frames": stream["frames"],
                "bytes": stream["bytes"],
                "gaps": gaps,
            }
        result["connections"] = self._connections
        result["config_changes"] = self._config_changes
        result["ffmpeg_restarts"] = self._ffmpeg_restarts
        result["minutes"] = [[m, *v] for m, v in sorted(self._minutes.items())]
        result["minutes_columns"] = [
            "minute",
            "audio_frames",
            "audio_bytes",
            "video_frames",
            "video_bytes",
        ]
        return result

    def save(self, final=False):
        """Writes the file; errors are logged, never raised (recording first)."""
        self._last_saved = self._clock()
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(final)), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning(f"Could not save stream stats {self._path.name}: {e}")

    def maybe_save(self):
        if self._clock() - self._last_saved >= SAVE_EVERY_SECONDS:
            self.save()

    def discard(self):
        Path(self._path).unlink(missing_ok=True)
        self._path.with_name(self._path.name + ".tmp").unlink(missing_ok=True)
