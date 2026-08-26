import threading
import time
from pathlib import Path

from core.tiktok_recorder import TikTokRecorder
from db import recordings_repo
from supervisor.thumbnails import generate_thumbnail
from utils.enums import Mode
from utils.logger_manager import logger
from utils.recorder_config import RecorderConfig
from utils.utils import read_cookies


class Worker:
    """
    Runs one channel's recorder as a supervised, stoppable background
    thread, and turns its on_status callbacks into DB writes + live status
    updates. This is `automatic_mode`/`followers_mode` running unattended
    instead of owning the whole process, per the hub's channel supervisor.
    """

    def __init__(self, channel_row, db_path, get_connection, output_dir, status_store):
        self.channel_id = channel_row["id"]
        self.username = channel_row["username"]

        self._db_path = db_path
        self._get_connection = get_connection
        self._status_store = status_store
        self._current_recording_id = None
        self._recording_started_at = None

        self._stop_event = threading.Event()
        config = RecorderConfig(
            mode=Mode(channel_row["mode"]),
            user=self.username,
            automatic_interval=channel_row["automatic_interval"],
            cookies=read_cookies(),
            proxy=channel_row["proxy"],
            output=str(output_dir),
            bitrate=channel_row["bitrate"],
            use_telegram=bool(channel_row["use_telegram"]),
            stop_event=self._stop_event,
            on_status=self._on_status,
        )
        self._recorder = TikTokRecorder(config)
        self._thread = threading.Thread(
            target=self._run, name=f"channel-{self.channel_id}", daemon=True
        )

    def start(self):
        self._status_store.set(self.channel_id, phase="starting")
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def join(self, timeout=None):
        self._thread.join(timeout)

    def is_alive(self):
        return self._thread.is_alive()

    def _run(self):
        try:
            self._recorder.run()
        except Exception as exc:
            logger.error(
                f"Worker for channel {self.channel_id} (@{self.username}) crashed: {exc}",
                exc_info=True,
            )
            self._status_store.set(self.channel_id, phase="error", detail=str(exc))
        else:
            # `TikTokRecorder.run()` returns normally even after an internal
            # recording_error was already reported via `_on_status` - don't
            # clobber that with a stale "idle".
            current = self._status_store.get(self.channel_id)
            if current is None or current["phase"] != "error":
                self._status_store.set(self.channel_id, phase="idle")

    def _on_status(self, phase, **data):
        conn = self._get_connection(self._db_path)

        if phase == "checking":
            self._status_store.set(self.channel_id, phase="checking")

        elif phase == "waiting":
            self._status_store.set(self.channel_id, phase="waiting")

        elif phase == "recording_started":
            self._recording_started_at = time.time()
            self._current_recording_id = recordings_repo.start(
                conn,
                channel_id=self.channel_id,
                username=data.get("user", self.username),
                file_path=data["file_path"],
                format="flv",
            )
            self._status_store.set(
                self.channel_id, phase="recording", detail=data.get("file_path")
            )

        elif phase == "converting":
            self._status_store.set(
                self.channel_id, phase="converting", detail=data.get("file_path")
            )

        elif phase == "recording_finished":
            self._finish_recording(conn, **data)

        elif phase == "recording_error":
            self._fail_recording(conn, data.get("error", "unknown error"))

    def _finish_recording(self, conn, *, file_path, format="mp4", **_ignored):
        duration_seconds = None
        if self._recording_started_at is not None:
            duration_seconds = round(time.time() - self._recording_started_at)

        file_size_bytes = None
        try:
            file_size_bytes = Path(file_path).stat().st_size
        except OSError:
            pass

        thumbnail_path = generate_thumbnail(file_path)

        if self._current_recording_id is not None:
            recordings_repo.mark_completed(
                conn,
                self._current_recording_id,
                file_path=file_path,
                format=format,
                duration_seconds=duration_seconds,
                file_size_bytes=file_size_bytes,
                thumbnail_path=thumbnail_path,
            )

        self._current_recording_id = None
        self._recording_started_at = None
        self._status_store.set(self.channel_id, phase="idle")

    def _fail_recording(self, conn, error_message):
        if self._current_recording_id is not None:
            recordings_repo.mark_failed(
                conn, self._current_recording_id, error_message=error_message
            )

        self._current_recording_id = None
        self._recording_started_at = None
        self._status_store.set(self.channel_id, phase="error", detail=error_message)
