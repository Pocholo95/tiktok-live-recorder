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

import json
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


# A guest must be missing from this many consecutive polls before they
# count as gone: TikTok's room info flickers (e.g. during layout changes).
MISSES_TO_LEAVE = 2
# A guest who reappears within this many seconds of leaving continues the
# same time range instead of starting a new row.
MERGE_GAP_SECONDS = 120

_DEBUG_DROP_KEYS = ("avatar", "url_list", "icon", "image", "badge")


def _debug_trim(value):
    """Room info minus picture URLs, to keep the diagnostic file small."""
    if isinstance(value, dict):
        return {
            k: _debug_trim(v)
            for k, v in value.items()
            if not any(drop in k.lower() for drop in _DEBUG_DROP_KEYS)
        }
    if isinstance(value, list):
        return [_debug_trim(v) for v in value]
    return value


class ParticipantTracker:
    """
    Polls room info while a recording runs and keeps, per participant, the
    time ranges (seconds since recording start) they were linked in.
    `on_change(snapshot)` gets the full list whenever it changes:
    [{"username", "start_seconds", "end_seconds" (None while present)}].
    `interval` is seconds, or a callable returning them - re-read before
    every wait, so a change in Settings applies to lives already recording.
    `debug_path`, if given, gets a JSON line with TikTok's raw link data
    every time the set of linked ids changes (for diagnosing odd lists).
    """

    def __init__(
        self,
        api,
        room_id,
        on_change,
        interval=POLL_SECONDS,
        clock=None,
        debug_path=None,
    ):
        self._api = api
        self._room_id = room_id
        self._on_change = on_change
        self._interval = interval
        self._clock = clock or time.monotonic
        self._debug_path = debug_path
        self._resolver = UsernameResolver(api.http_client)
        self._started = self._clock()
        self._stop = threading.Event()
        self._intervals = []  # dicts, in order of first appearance
        self._open = {}  # username -> its open interval dict
        self._misses = {}  # username -> (consecutive misses, first miss time)
        self._last_ids = None
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
            for username, interval in self._open.items():
                # Someone already missing at the end left when first missed.
                misses = self._misses.get(username)
                interval["end_seconds"] = misses[1] if misses else now
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
            ids = extract_participant_ids(room_data)
            resolved = {}
            for user_id in ids:
                username = self._resolver.resolve(user_id)
                if username:
                    resolved[user_id] = username
        except Exception as e:
            if not self._warned:
                logger.warning(f"Guest names unavailable for this live: {e}")
                self._warned = True
            return
        self._write_debug(room_data, ids, resolved)
        self._update(set(resolved.values()))

    def _write_debug(self, room_data, ids, resolved):
        if not self._debug_path or ids == self._last_ids:
            return
        self._last_ids = ids
        record = {
            "elapsed_seconds": self._elapsed(),
            "time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "ids": sorted(ids),
            "resolved": resolved,
            "link_mic": _debug_trim(room_data.get("link_mic")),
            "linker_map": room_data.get("linker_map"),
            "linkmic_layout": room_data.get("linkmic_layout"),
            "with_linkmic": room_data.get("with_linkmic"),
        }
        try:
            with open(self._debug_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            logger.warning(f"Could not write guest diagnostics: {e}")
            self._debug_path = None

    def _update(self, present):
        now = self._elapsed()
        changed = False

        for username in present:
            self._misses.pop(username, None)
            if username in self._open:
                continue
            previous = self._last_interval(username)
            if (
                previous is not None
                and previous["end_seconds"] is not None
                and now - previous["end_seconds"] <= MERGE_GAP_SECONDS
            ):
                previous["end_seconds"] = None  # short absence: same stay
                self._open[username] = previous
            else:
                interval = {
                    "username": username,
                    "start_seconds": now,
                    "end_seconds": None,
                }
                self._intervals.append(interval)
                self._open[username] = interval
            changed = True

        for username in set(self._open) - present:
            count, first_miss = self._misses.get(username, (0, now))
            count += 1
            if count >= MISSES_TO_LEAVE:
                self._open.pop(username)["end_seconds"] = first_miss
                self._misses.pop(username, None)
                changed = True
            else:
                self._misses[username] = (count, first_miss)

        if changed:
            self._emit()

    def _last_interval(self, username):
        for interval in reversed(self._intervals):
            if interval["username"] == username:
                return interval
        return None

    def _emit(self):
        try:
            self._on_change([dict(i) for i in self._intervals])
        except Exception as e:
            logger.warning(f"Could not save guest names: {e}")
