"""
Watches the running channels for problems nobody would notice on an
unattended server, and raises Discord problem alerts (supervisor/alerts.py):

- a channel stuck in the "error" phase for ERROR_MINUTES,
- a recording whose file hasn't grown for STALL_MINUTES (the stream
  connection is open but TikTok stopped sending data, ffmpeg hung, disk...).

Stalls are also flagged on the live status (`flags["stalled_since"]`) so the
dashboard can show them. It only observes and alerts; it never touches the
recording itself.
"""

import os
import threading
import time

from db import channels_repo
from supervisor import alerts
from utils.logger_manager import logger

CHECK_SECONDS = 60
ERROR_MINUTES = 30
STALL_MINUTES = 5


class HealthMonitor:
    def __init__(
        self,
        db_path,
        get_connection,
        status_store,
        interval=CHECK_SECONDS,
        clock=time.time,
        size_of=os.path.getsize,
    ):
        self._db_path = db_path
        self._get_connection = get_connection
        self._status = status_store
        self._interval = interval
        self._clock = clock
        self._size_of = size_of
        self._growth = {}  # channel_id -> (path, last size, time it last grew)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="health", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                self.check_once()
            except Exception as e:
                logger.warning(f"Health check failed: {e}")

    def check_once(self):
        now = self._clock()
        statuses = self._status.all()
        conn = self._get_connection(self._db_path)
        try:
            names = {row["id"]: row["username"] for row in channels_repo.list_all(conn)}
            for channel_id, status in statuses.items():
                user = names.get(channel_id, f"canal {channel_id}")
                if status["phase"] == "error":
                    self._check_error(conn, channel_id, user, status, now)
                if status["phase"] == "recording":
                    self._check_growth(conn, channel_id, user, status, now)
                else:
                    self._growth.pop(channel_id, None)
        finally:
            conn.close()

    def _check_error(self, conn, channel_id, user, status, now):
        minutes = (now - status["since"]) / 60
        if minutes < ERROR_MINUTES:
            return
        # One alert per error episode: the episode is identified by its start.
        alerts.send(
            conn,
            f"channel_error:{channel_id}:{int(status['since'])}",
            f"@{user} lleva {int(minutes)} min con error",
            f"El canal no está grabando ni revisando lives. Último error: "
            f"{status.get('detail') or 'desconocido'}",
            throttle_seconds=365 * 24 * 3600,
            now=now,
        )

    def _check_growth(self, conn, channel_id, user, status, now):
        path = status.get("detail")
        try:
            size = self._size_of(path) if path else None
        except OSError:
            size = None

        previous = self._growth.get(channel_id)
        if previous is None or previous[0] != path or size != previous[1]:
            self._growth[channel_id] = (path, size, now)
            self._status.set_flag(channel_id, "stalled_since", None)
            return

        last_growth = previous[2]
        if now - last_growth < STALL_MINUTES * 60:
            return
        self._status.set_flag(channel_id, "stalled_since", last_growth)
        alerts.send(
            conn,
            f"stalled:{channel_id}:{path}",
            f"La grabación de @{user} dejó de crecer",
            f"El archivo no cambia hace {int((now - last_growth) / 60)} min, "
            "aunque el canal figura grabando. Puede que TikTok haya dejado de "
            "mandar video o que algo se haya trabado; revisá el canal.",
            throttle_seconds=6 * 3600,
            now=now,
        )
