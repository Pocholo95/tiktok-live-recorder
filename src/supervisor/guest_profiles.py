"""
Profile pictures for guests/co-hosts (user request 2026-11-21).

Channel creators get their picture for free from every live check
(supervisor/profiles.py). Guests aren't channels, so their picture is
fetched once per username with TikTok's unsigned user/room API
(`TikTokAPI._unsigned_user_room`, the same request as the room-id lookup)
and saved the same way: `OUTPUT_DIR/avatars/<username>.jpg` + `profiles`
row, served by `/avatars/{username}` - the guests panel and the "En
pantalla" bar already ask for it.

One daemon thread, one request every few seconds, usernames whose profile
is younger than REFRESH_DAYS are skipped. Fed by the worker whenever the
guest list of a recording changes, and at startup with every guest of the
old recordings. Never raises: a missing picture only keeps the initial.
"""

import queue
import threading
import time

from supervisor import profiles
from utils.logger_manager import logger

REFRESH_DAYS = 7
PAUSE_SECONDS = 3


def avatar_url(user_info):
    info = user_info or {}
    return (
        info.get("avatarMedium") or info.get("avatarThumb") or info.get("avatarLarger")
    )


class GuestProfiles:
    def __init__(
        self, db_path, get_connection, output_dir, api_factory, pause=PAUSE_SECONDS
    ):
        self._db_path = db_path
        self._get_connection = get_connection
        self._output_dir = output_dir
        self._api_factory = api_factory
        self._pause = pause
        self._queue = queue.Queue()
        self._seen = set()  # queued or done in this run
        self._lock = threading.Lock()
        self._thread = None

    def want(self, usernames):
        """Queue the usernames whose picture is missing or stale."""
        names = {u.lstrip("@") for u in usernames or () if u}
        with self._lock:
            names -= self._seen
            if not names:
                return
            self._seen |= names
        conn = self._get_connection(self._db_path)
        try:
            fresh = {
                row["username"]
                for row in conn.execute(
                    f"""
                    SELECT username FROM profiles
                    WHERE avatar_path IS NOT NULL
                      AND updated_at >= datetime('now', '-{int(REFRESH_DAYS)} days')
                      AND username IN ({",".join("?" * len(names))})
                    """,
                    sorted(names),
                )
            }
        finally:
            conn.close()
        for name in sorted(names - fresh):
            self._queue.put(name)
        self._start()

    def want_all_known_guests(self):
        """Every guest of the recordings already in the DB (startup)."""
        conn = self._get_connection(self._db_path)
        try:
            names = [
                row["username"]
                for row in conn.execute(
                    "SELECT DISTINCT username FROM recording_participants "
                    "WHERE username != ''"
                )
            ]
        finally:
            conn.close()
        self.want(names)

    def _start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="guest-profiles", daemon=True
                )
                self._thread.start()

    def _run(self):
        while True:
            try:
                username = self._queue.get(timeout=60)
            except queue.Empty:
                return  # restarted by the next want()
            try:
                self.fetch(username)
            except Exception as e:
                logger.warning(f"Guest profile for @{username} failed: {e}")
            time.sleep(self._pause)

    def fetch(self, username):
        room_data = self._api_factory()._unsigned_user_room(username)
        info = (room_data or {}).get("user") or {}
        url = avatar_url(info)
        if not url and not info.get("nickname"):
            return False
        profiles.save_profile(
            self._db_path,
            self._get_connection,
            self._output_dir,
            username,
            url,
            info.get("nickname"),
        )
        return True


_instance = None


def install(instance):
    global _instance
    _instance = instance


def want(usernames):
    """Called by the worker; no-op until serve.py installs the fetcher."""
    if _instance is not None:
        try:
            _instance.want(usernames)
        except Exception as e:
            logger.warning(f"Could not queue guest profiles: {e}")
