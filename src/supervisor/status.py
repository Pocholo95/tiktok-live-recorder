import threading
import time


class ChannelStatusStore:
    """
    Thread-safe in-memory status board. Worker threads write to it as they
    move through phases (recording/converting/idle/error), and the web
    layer polls it to render live status without touching the DB or
    parsing logs.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._statuses = {}

    def set(self, channel_id, *, phase, detail=None):
        with self._lock:
            self._statuses[channel_id] = {
                "phase": phase,
                "detail": detail,
                "updated_at": time.time(),
            }

    def get(self, channel_id):
        with self._lock:
            return self._statuses.get(channel_id)

    def all(self):
        with self._lock:
            return dict(self._statuses)

    def discard(self, channel_id):
        with self._lock:
            self._statuses.pop(channel_id, None)
