"""
Canary for live detection itself.

If TikTok changes an endpoint, every channel just shows "Offline" - nothing
errors, nothing alerts, and lives are lost quietly. So every CHECK_SECONDS
the hub runs the same detection (room-id lookup + is_room_alive) on an
account that is live around the clock (env HUB_CANARY_USER, default
"weathernewslive", a 24/7 weather channel; empty disables it). Seen not
live - or failing - for ALERT_AFTER_SECONDS in a row sends one Discord
problem alert; recovery is logged.
"""

import os
import threading
import time

from supervisor import alerts
from utils.logger_manager import logger

DEFAULT_USER = "weathernewslive"
CHECK_SECONDS = 600
ALERT_AFTER_SECONDS = 30 * 60


def canary_user():
    value = os.environ.get("HUB_CANARY_USER")
    return DEFAULT_USER if value is None else value.strip().lstrip("@")


class DetectionCanary:
    def __init__(self, db_path, get_connection, make_api, user=None, clock=time.time):
        self._db_path = db_path
        self._get_connection = get_connection
        self._make_api = make_api
        self.user = canary_user() if user is None else user
        self._clock = clock
        self._bad_since = None
        self._alerted = False
        self._stop = threading.Event()

    def start(self):
        if not self.user:
            return
        threading.Thread(target=self._run, name="canary", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(CHECK_SECONDS):
            self.check_once()

    def _detects_live(self):
        """(True, None) if detection sees the canary live, else (False, why)."""
        try:
            api = self._make_api()
            room_id = api.get_room_id_from_user(self.user)
            if not room_id:
                return False, "no devolvió sala"
            if not api.is_room_alive(room_id):
                return False, "dice que no está en vivo"
            return True, None
        except Exception as e:
            return False, f"error: {e}"

    def check_once(self):
        ok, why = self._detects_live()
        now = self._clock()
        if ok:
            if self._bad_since is not None:
                logger.info(f"Detection canary @{self.user} is live again")
            self._bad_since = None
            self._alerted = False
            return
        if self._bad_since is None:
            self._bad_since = now
            logger.warning(f"Detection canary @{self.user}: {why}")
        if self._alerted or now - self._bad_since < ALERT_AFTER_SECONDS:
            return
        self._alerted = True
        conn = self._get_connection(self._db_path)
        try:
            alerts.send(
                conn,
                "detection_canary",
                "La detección de lives podría estar rota",
                f"@{self.user} transmite las 24 h, pero hace "
                f"{int((now - self._bad_since) // 60)} min que el hub no lo ve en "
                f"vivo ({why}). Si TikTok cambió algo, tus canales van a figurar "
                "Offline aunque estén en vivo. También puede ser que ese canal "
                "cortó: si lo ves en vivo en TikTok, el problema es del hub.",
                throttle_seconds=6 * 3600,
            )
        finally:
            conn.close()
