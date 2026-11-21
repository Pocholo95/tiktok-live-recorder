"""
Who is on screen, read from the video itself.

When creators link up (co-host / multi-guest), TikTok mixes the panels on
its servers and stamps the H.264 stream with SEI messages describing the
mix, about once a second:

    {"app_data": "{\"grids\":[{\"cid\":..,\"uid_str\":..,\"x\":..,...}],
                   \"anchor_link_mic_id\": <creator's uid_str>, ...}", ...}

Each grid is one panel; `cid` is a per-link session id (a new one every time
someone links in), NOT a user id, so this tells exactly WHEN each guest
panel appears and disappears (in video time), but not who it is. Names come
from room info polls (`ParticipantTracker`), which only ever name one
linked creator at a time and rotate among them - see `LayoutTracker.name`.

Everything here is best-effort: a parsing failure only means no layout.
"""

import json
import threading

APP_DATA_MARKER = b'{"app_data":'
# A panel missing from this many seconds of layout updates is gone (the
# SEI occasionally skips a beat).
LEAVE_AFTER_SECONDS = 5.0
# No layout update for this long: the creator is solo again (TikTok stops
# sending the mix description), so every panel is gone.
SILENCE_SECONDS = 10.0

_decoder = json.JSONDecoder()


def extract_app_data(data):
    """The mix description inside one FLV video tag's bytes, or None."""
    start = data.find(APP_DATA_MARKER)
    if start < 0:
        return None
    try:
        outer, _end = _decoder.raw_decode(data[start : start + 65536].decode("latin-1"))
        inner = outer.get("app_data")
        return json.loads(inner) if isinstance(inner, str) else inner
    except (ValueError, AttributeError):
        return None


def guest_panels(app_data):
    """{cid: (x, y, w, h)} of every panel except the creator's own."""
    grids = app_data.get("grids") if isinstance(app_data, dict) else None
    if not isinstance(grids, list):
        return {}
    host = app_data.get("anchor_link_mic_id")
    host_cid = str(app_data.get("channel_id") or "")
    panels = {}
    for grid in grids:
        if not isinstance(grid, dict) or not grid.get("cid"):
            continue
        cid = str(grid["cid"])
        is_host = (host and grid.get("uid_str") == host) or (
            not host and (cid == host_cid or grid.get("p") == 0)
        )
        if not is_host:
            panels[cid] = tuple(grid.get(k) for k in ("x", "y", "w", "h"))
    return panels


class LayoutTracker:
    """
    Guest panels over the recording, in seconds of video. `observe()` is
    fed from the recording thread; `name()`, `tick()` and `snapshot()` from
    the guest tracker's thread - hence the lock.

    Each panel: {"cid", "start", "end" (None while on screen), "name"}.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._panels = []  # every panel seen, in order of appearance
        self._open = {}  # cid -> open panel
        self._missing = {}  # cid -> media time it was first missing
        self._last_seen = None  # media time of the last layout update
        self._parsed_at = None  # media ms of the last parsed frame
        self.seen_any = False

    def feed_video(self, media_ms, data):
        """Called with every video frame written (FlvNormalizer). Cheap: a
        byte search, and at most one JSON parse per second of video."""
        if self._parsed_at is not None and 0 <= media_ms - self._parsed_at < 900:
            return
        if APP_DATA_MARKER not in data:
            return
        app_data = extract_app_data(data)
        if app_data is None:
            return
        self._parsed_at = media_ms
        self.observe(media_ms / 1000, app_data)

    def observe(self, media_seconds, app_data):
        panels = guest_panels(app_data)
        with self._lock:
            self.seen_any = True
            self._last_seen = media_seconds
            for cid in panels:
                self._missing.pop(cid, None)
                if cid not in self._open:
                    panel = {"cid": cid, "start": media_seconds, "end": None}
                    panel["name"] = None
                    self._panels.append(panel)
                    self._open[cid] = panel
            for cid in list(self._open):
                if cid in panels:
                    continue
                first_missing = self._missing.setdefault(cid, media_seconds)
                if media_seconds - first_missing >= LEAVE_AFTER_SECONDS:
                    self._close(cid, first_missing)

    def tick(self, media_seconds):
        """Closes everything once the layout updates stop (solo again)."""
        with self._lock:
            if (
                self._last_seen is not None
                and media_seconds - self._last_seen >= SILENCE_SECONDS
            ):
                for cid in list(self._open):
                    self._close(cid, self._last_seen)

    def finish(self, media_seconds):
        with self._lock:
            end = self._last_seen if self._last_seen is not None else media_seconds
            for cid in list(self._open):
                self._close(cid, min(end, media_seconds))

    def _close(self, cid, at):
        panel = self._open.pop(cid)
        panel["end"] = round(max(at, panel["start"]), 1)
        self._missing.pop(cid, None)

    def name(self, usernames):
        """
        Puts names from a room-info poll on panels. Room info names one
        linked creator at a time (rotating), so a name not yet on screen
        goes to the newest panel still without one: guests mostly link in
        one by one, and the poll after a new panel appears is the likeliest
        to name it.
        """
        with self._lock:
            on_screen = {p["name"] for p in self._open.values() if p["name"]}
            for username in usernames:
                if username in on_screen:
                    continue
                unnamed = [p for p in self._open.values() if p["name"] is None]
                if not unnamed:
                    return
                newest = max(unnamed, key=lambda p: p["start"])
                newest["name"] = username
                on_screen.add(username)

    def open_count(self):
        with self._lock:
            return len(self._open)

    def snapshot(self):
        """[{"username" ("" if unknown), "start_seconds", "end_seconds"}]."""
        with self._lock:
            return [
                {
                    "username": p["name"] or "",
                    "start_seconds": round(p["start"], 1),
                    "end_seconds": p["end"],
                }
                for p in self._panels
            ]

    def debug_state(self):
        with self._lock:
            return {cid: p["name"] for cid, p in self._open.items()}
