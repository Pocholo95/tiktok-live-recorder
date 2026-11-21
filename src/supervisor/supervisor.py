import hashlib
import threading
import time

from db import channels_repo
from db.database import get_connection
from supervisor import alerts
from supervisor.status import ChannelStatusStore
from supervisor.worker import Worker as _Worker
from utils.logger_manager import logger

# Fields that, if changed, should restart a channel's worker (e.g. a new
# proxy) rather than leaving the old one running with stale config.
# automatic_interval and use_telegram (the Discord notices bell) are
# deliberately absent: the worker re-reads both from the DB when it needs
# them, so editing either never interrupts a recording.
_TRACKED_FIELDS = (
    "username",
    "mode",
    "proxy",
)


# A worker that dies is restarted after these delays (one per consecutive
# crash, the last repeats): a crash loop used to restart every 5 s, hammering
# TikTok and flipping the status between "starting" and "error" so the
# 30-minute error alert never fired. A run of HEALTHY_RUN_SECONDS resets it.
RESTART_DELAYS = (0, 30, 120, 300, 900)
HEALTHY_RUN_SECONDS = 600
# Consecutive crashes that send a Discord problem alert.
CRASH_ALERT_AFTER = 3


def _row_fingerprint(row):
    values = "|".join(str(row[field]) for field in _TRACKED_FIELDS)
    return hashlib.sha256(values.encode()).hexdigest()


class ChannelSupervisor:
    """
    Reconciles the `channels` table against a set of running worker
    threads: starts one per enabled channel, stops/restarts on
    disable/delete/config-change, and prunes workers that died on their own.
    """

    def __init__(self, db_path, output_dir, reconcile_interval=5, clock=time.time):
        self.db_path = db_path
        self.output_dir = output_dir
        self.reconcile_interval = reconcile_interval
        self.status = ChannelStatusStore()
        self._clock = clock

        self._workers = {}  # channel_id -> (Worker, fingerprint)
        self._started_at = {}  # channel_id -> when its current worker started
        # channel_id -> {"count": consecutive crashes, "retry_at": time}
        self._crashes = {}
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="channel-supervisor", daemon=True
        )

    def start(self):
        self._thread.start()

    def stop(self, wait=True):
        self._stop_event.set()

        for worker, _fingerprint in list(self._workers.values()):
            worker.stop()

        if wait:
            for worker, _fingerprint in list(self._workers.values()):
                worker.join(timeout=30)

        self._thread.join(timeout=self.reconcile_interval + 5)

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self._reconcile_once()
            except Exception:
                logger.error("Supervisor reconcile failed", exc_info=True)
            self._stop_event.wait(self.reconcile_interval)

    def _reconcile_once(self):
        conn = get_connection(self.db_path)
        try:
            enabled_rows = {row["id"]: row for row in channels_repo.list_enabled(conn)}
        finally:
            conn.close()

        for channel_id, (worker, fingerprint) in list(self._workers.items()):
            row = enabled_rows.get(channel_id)
            if row is None or _row_fingerprint(row) != fingerprint:
                worker.stop()
                worker.join(timeout=30)
                del self._workers[channel_id]
                self._crashes.pop(channel_id, None)  # deliberate restart
                self.status.discard(channel_id)

        now = self._clock()
        for channel_id, (worker, _fingerprint) in list(self._workers.items()):
            if not worker.is_alive():
                del self._workers[channel_id]
                self._worker_died(channel_id, enabled_rows.get(channel_id), now)

        for channel_id in list(self._crashes):
            if channel_id not in enabled_rows:
                del self._crashes[channel_id]

        for channel_id, row in enabled_rows.items():
            if channel_id in self._workers:
                continue
            crash = self._crashes.get(channel_id)
            if crash and now < crash["retry_at"]:
                continue
            worker = _Worker(
                row, self.db_path, get_connection, self.output_dir, self.status
            )
            worker.start()
            self._workers[channel_id] = (worker, _row_fingerprint(row))
            self._started_at[channel_id] = now

    def _worker_died(self, channel_id, row, now):
        ran = now - self._started_at.pop(channel_id, now)
        previous = self._crashes.get(channel_id)
        count = (
            1
            if previous is None or ran >= HEALTHY_RUN_SECONDS
            else previous["count"] + 1
        )
        delay = RESTART_DELAYS[min(count, len(RESTART_DELAYS)) - 1]
        self._crashes[channel_id] = {"count": count, "retry_at": now + delay}
        if count < 2:
            return
        status = self.status.get(channel_id) or {}
        reason = status.get("detail") or "error desconocido"
        user = row["username"] if row is not None else f"canal {channel_id}"
        logger.warning(
            f"@{user} worker died {count} times in a row ({reason}); "
            f"restarting in {delay} s"
        )
        self.status.set(
            channel_id,
            phase="error",
            detail=f"{reason} · se cayó {count} veces seguidas, reintenta en "
            + (f"{delay} s" if delay < 60 else f"{delay // 60} min"),
        )
        if count == CRASH_ALERT_AFTER:
            conn = get_connection(self.db_path)
            try:
                alerts.send(
                    conn,
                    f"worker_crashing:{channel_id}",
                    f"El canal @{user} se cae una y otra vez",
                    f"Se cayó {count} veces seguidas sin poder revisar si está "
                    f"en vivo. Último error: {reason}. El hub sigue "
                    "reintentando cada vez más espaciado; si no se arregla solo, "
                    "mirá el log del contenedor.",
                    throttle_seconds=6 * 3600,
                )
            except Exception as e:
                logger.warning(f"Could not send the crash alert: {e}")
            finally:
                conn.close()

    def get_status(self, channel_id):
        return self.status.get(channel_id)

    def get_all_statuses(self):
        return self.status.all()
