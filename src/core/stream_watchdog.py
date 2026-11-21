"""
Reconnects when TikTok stops sending audio (or video) while the other one
keeps arriving.

Some lives lose their audio halfway and never get it back on the same
connection; a fresh connection often lands on another CDN node that sends
both again. The recorder already knows how to reconnect into the same
file (FlvNormalizer splices it, ~2-5 s lost), so the watchdog only says
when: it reads the counts in StreamStats, measured on the VIDEO timeline,
so "missing" only grows while the other stream is still flowing - a live
that stops sending anything is the health monitor's job, not this one.

Per episode (from when a stream goes missing until it's back):
- missing for AUDIO_MISSING_SECONDS (VIDEO_MISSING_SECONDS for video, which
  also goes quiet while a creator pauses the live) -> reconnect;
- still missing -> reconnect again after WAIT_AFTER_ATTEMPT seconds;
- after MAX_ATTEMPTS it gives up: keeps recording whatever arrives (video
  without audio beats no video) and the hub sends a Discord problem alert.
A muted microphone still sends (silent) audio frames, so it never trips.
Every attempt is logged in the stats file.
"""

from core import stream_stats

AUDIO_MISSING_SECONDS = 45
VIDEO_MISSING_SECONDS = 120
WAIT_AFTER_ATTEMPT = (120, 300, 300)  # seconds of video after attempt 1, 2, 3
MAX_ATTEMPTS = len(WAIT_AFTER_ATTEMPT)
_RECOVERED_SECONDS = stream_stats.GAP_MS / 1000
_THRESHOLDS = (("audio", AUDIO_MISSING_SECONDS), ("video", VIDEO_MISSING_SECONDS))


class StreamWatchdog:
    def __init__(self, stats):
        self._stats = stats
        self._episodes = {}  # kind -> {"attempts", "last_at", "gave_up"}

    def check(self):
        """
        Call after each chunk. Returns None, or an event dict
        {"action": "reconnect" | "give_up" | "recovered", "kind", "attempt"}
        (already logged in the stats). Never raises.
        """
        try:
            return self._check()
        except Exception:
            return None

    def _check(self):
        now = self._stats.media_seconds
        for kind, threshold in _THRESHOLDS:
            missing = self._stats.missing_seconds(kind)
            episode = self._episodes.get(kind)
            if missing < _RECOVERED_SECONDS:
                if episode is not None:
                    del self._episodes[kind]
                    return self._event("recovered", kind, episode["attempts"])
                continue
            if missing < threshold:
                continue
            if episode is None:
                episode = {"attempts": 0, "last_at": None, "gave_up": False}
                self._episodes[kind] = episode
            if episode["gave_up"]:
                continue
            attempts = episode["attempts"]
            if attempts and now - episode["last_at"] < WAIT_AFTER_ATTEMPT[attempts - 1]:
                continue
            if attempts >= MAX_ATTEMPTS:
                episode["gave_up"] = True
                return self._event("give_up", kind, attempts)
            episode["attempts"] = attempts + 1
            episode["last_at"] = now
            return self._event("reconnect", kind, attempts + 1)
        return None

    def _event(self, action, kind, attempt):
        event = {"action": action, "kind": kind, "attempt": attempt}
        self._stats.watchdog(dict(event))
        return event
