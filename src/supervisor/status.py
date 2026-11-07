import threading
import time


class ChannelStatusStore:
    """
    Thread-safe in-memory status board. Worker threads write to it as they
    move through phases (recording/idle/error), and the web
    layer polls it to render live status without touching the DB or
    parsing logs.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._statuses = {}

    def set(self, channel_id, *, phase, detail=None):
        with self._lock:
            now = time.time()
            previous = self._statuses.get(channel_id)
            same_phase = previous is not None and previous["phase"] == phase
            self._statuses[channel_id] = {
                "phase": phase,
                "detail": detail,
                "updated_at": now,
                # When the channel entered this phase; unchanged by repeated
                # sets of the same phase (used for "stuck in error" alerts).
                "since": previous["since"] if same_phase else now,
                # Health-monitor annotations (e.g. "stalled_since") survive
                # only while the phase stays the same.
                "flags": dict(previous.get("flags", {})) if same_phase else {},
            }

    def set_flag(self, channel_id, key, value):
        """Annotate the current status (None removes the flag)."""
        with self._lock:
            status = self._statuses.get(channel_id)
            if status is None:
                return
            flags = dict(status.get("flags", {}))
            if value is None:
                flags.pop(key, None)
            else:
                flags[key] = value
            status["flags"] = flags

    def get(self, channel_id):
        with self._lock:
            status = self._statuses.get(channel_id)
            return dict(status) if status is not None else None

    def all(self):
        with self._lock:
            return {cid: dict(status) for cid, status in self._statuses.items()}

    def discard(self, channel_id):
        with self._lock:
            self._statuses.pop(channel_id, None)
