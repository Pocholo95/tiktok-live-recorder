"""
Best-effort tracking of who is on screen with the creator during a live.

TikTok's room info (`webcast/room/info`) lists the other people linked into
the stream: a co-host creator (`link_mic.rival_anchor_id`) and multi-guest
guests (`link_mic.linked_user_list` / `show_user_list`). Only numeric user
ids are given; `https://www.tiktok.com/share/user/<id>` redirects to
`/@<username>`, which turns them into handles without any signing.

Age-restricted lives only return room info with a logged-in session
(cookies.json `sessionid_ss`). Everything here is optional: any failure -
no session, expired session, TikTok changes - just means no names are
recorded. It never raises into the recorder and never stops a recording.
"""

import re
import threading
import time

from utils.logger_manager import logger

POLL_SECONDS = 30
MIN_POLL_SECONDS = 10
MAX_POLL_SECONDS = 600
SETTING_KEY = "participants_poll_seconds"


def clamp_interval(value):
    """Valid poll interval in seconds, falling back to the default."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return POLL_SECONDS
    return max(MIN_POLL_SECONDS, min(MAX_POLL_SECONDS, seconds))


_ID_KEYS = ("id", "id_str", "user_id", "userId", "linker_id", "uid")
_USER_ID = re.compile(r"^\d{6,20}$")
_HANDLE_FROM_URL = re.compile(r"tiktok\.com/@([A-Za-z0-9._]+)")


def _ids_in(value):
    """Every user-id-looking value under the known id keys, at any depth."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _ID_KEYS and _USER_ID.match(str(item)) and str(item) != "0":
                found.add(str(item))
            else:
                found |= _ids_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= _ids_in(item)
    return found


def extract_participant_ids(room_data):
    """Ids of everyone linked into the live, minus the owner."""
    owner = room_data.get("owner") or {}
    owner_id = str(owner.get("id_str") or owner.get("id") or "")
    link_mic = room_data.get("link_mic") or {}

    ids = set()
    rival = str(link_mic.get("rival_anchor_id") or "0")
    if rival != "0":
        ids.add(rival)
    for key in ("linked_user_list", "show_user_list"):
        ids |= _ids_in(link_mic.get(key) or [])
    ids.discard(owner_id)
    return ids


class UsernameResolver:
    """Numeric user id -> @handle via TikTok's share redirect, cached."""

    def __init__(self, http_client):
        self._http = http_client
        self._cache = {}

    def resolve(self, user_id):
        if user_id in self._cache:
            return self._cache[user_id]
        response = self._http.get(
            f"https://www.tiktok.com/share/user/{user_id}", allow_redirects=False
        )
        location = response.headers.get("location") or response.headers.get(
            "Location", ""
        )
        match = _HANDLE_FROM_URL.search(location)
        username = match.group(1) if match else None
        if username:
            self._cache[user_id] = username
        return username


class ParticipantTracker:
    """
    Polls room info while a recording runs and keeps, per participant, the
    time ranges (seconds since recording start) they were linked in.
    `on_change(snapshot)` gets the full list whenever it changes:
    [{"username", "start_seconds", "end_seconds" (None while present)}].
    `interval` is seconds, or a callable returning them - re-read before
    every wait, so a change in Settings applies to lives already recording.
    """

    def __init__(self, api, room_id, on_change, interval=POLL_SECONDS, clock=None):
        self._api = api
        self._room_id = room_id
        self._on_change = on_change
        self._interval = interval
        self._clock = clock or time.monotonic
        self._resolver = UsernameResolver(api.http_client)
        self._started = self._clock()
        self._stop = threading.Event()
        self._intervals = []  # dicts, in order of first appearance
        self._open = {}  # username -> its open interval dict
        self._warned = False
        self._thread = threading.Thread(
            target=self._run, name="participants", daemon=True
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.ident is not None:  # only if start() actually ran
            self._thread.join(timeout=10)
        if self._open:
            now = self._elapsed()
            for interval in self._open.values():
                interval["end_seconds"] = now
            self._open.clear()
            self._emit()

    def _elapsed(self):
        return round(self._clock() - self._started, 1)

    def _current_interval(self):
        try:
            value = self._interval() if callable(self._interval) else self._interval
        except Exception:
            value = None
        return clamp_interval(value)

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._current_interval())

    def poll_once(self):
        try:
            room_data = self._api.get_room_data(self._room_id)
            if room_data is None:
                raise RuntimeError(
                    "room info unavailable (age-restricted live without a "
                    "valid session cookie?)"
                )
            present = set()
            for user_id in extract_participant_ids(room_data):
                username = self._resolver.resolve(user_id)
                if username:
                    present.add(username)
        except Exception as e:
            if not self._warned:
                logger.warning(f"Guest names unavailable for this live: {e}")
                self._warned = True
            return
        self._update(present)

    def _update(self, present):
        now = self._elapsed()
        changed = False
        for username in present - set(self._open):
            interval = {"username": username, "start_seconds": now, "end_seconds": None}
            self._intervals.append(interval)
            self._open[username] = interval
            changed = True
        for username in set(self._open) - present:
            self._open.pop(username)["end_seconds"] = now
            changed = True
        if changed:
            self._emit()

    def _emit(self):
        try:
            self._on_change([dict(i) for i in self._intervals])
        except Exception as e:
            logger.warning(f"Could not save guest names: {e}")
