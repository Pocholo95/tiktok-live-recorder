import hashlib
import threading

from db import channels_repo
from db.database import get_connection
from supervisor.status import ChannelStatusStore
from supervisor.worker import Worker as _Worker
from utils.logger_manager import logger

# Fields that, if changed, should restart a channel's worker (e.g. a new
# interval or proxy) rather than leaving the old one running with stale config.
_TRACKED_FIELDS = (
    "username",
    "mode",
    "automatic_interval",
    "proxy",
    "bitrate",
    "use_telegram",
)


def _row_fingerprint(row):
    values = "|".join(str(row[field]) for field in _TRACKED_FIELDS)
    return hashlib.sha256(values.encode()).hexdigest()


class ChannelSupervisor:
    """
    Reconciles the `channels` table against a set of running worker
    threads: starts one per enabled channel, stops/restarts on
    disable/delete/config-change, and prunes workers that died on their own.
    """

    def __init__(self, db_path, output_dir, reconcile_interval=5):
        self.db_path = db_path
        self.output_dir = output_dir
        self.reconcile_interval = reconcile_interval
        self.status = ChannelStatusStore()

        self._workers = {}  # channel_id -> (Worker, fingerprint)
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
                self.status.discard(channel_id)

        for channel_id, (worker, _fingerprint) in list(self._workers.items()):
            if not worker.is_alive():
                del self._workers[channel_id]

        for channel_id, row in enabled_rows.items():
            if channel_id in self._workers:
                continue
            worker = _Worker(
                row, self.db_path, get_connection, self.output_dir, self.status
            )
            worker.start()
            self._workers[channel_id] = (worker, _row_fingerprint(row))

    def get_status(self, channel_id):
        return self.status.get(channel_id)

    def get_all_statuses(self):
        return self.status.all()
