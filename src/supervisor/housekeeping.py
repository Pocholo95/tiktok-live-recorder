"""
Hourly chores: delete expired kept originals (supervisor/originals.py) and
make the daily hub.db backup (supervisor/backups.py). Each runs on its own;
a failure is logged and retried next hour.
"""

import threading

from supervisor import backups, originals
from utils.logger_manager import logger

CHECK_SECONDS = 3600


class Housekeeping:
    def __init__(self, db_path, get_connection, output_dir):
        self._db_path = db_path
        self._get_connection = get_connection
        self._output_dir = output_dir
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, name="housekeeping", daemon=True).start()

    def stop(self):
        self._stop.set()

    def run_once(self):
        try:
            conn = self._get_connection(self._db_path)
            try:
                originals.cleanup(conn, self._output_dir)
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Kept originals cleanup failed: {e}")
        try:
            backups.run_if_due(self._db_path, backups.backup_dir(self._output_dir))
        except Exception as e:
            logger.warning(f"Database backup failed: {e}")

    def _run(self):
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(CHECK_SECONDS)
