import threading
import time
from pathlib import Path

from core import participants
from core.tiktok_recorder import TikTokRecorder
from db import channels_repo, participants_repo, recordings_repo, settings_repo
from notify import discord_webhook
from supervisor import alerts, profiles, schedules
from supervisor.thumbnails import generate_thumbnail
from utils.enums import Mode
from utils.logger_manager import logger
from utils.recorder_config import RecorderConfig
from utils.utils import read_cookies

PROFILE_REFRESH_SECONDS = 6 * 3600

RECONNECT_GRACE_KEY = "reconnect_grace_seconds"
RECONNECT_GRACE_DEFAULT = 300
RECONNECT_GRACE_MAX = 1800


def reconnect_grace_seconds(conn):
    """How long to keep a recording open after the live ends (Settings)."""
    try:
        value = int(
            settings_repo.get(conn, RECONNECT_GRACE_KEY, RECONNECT_GRACE_DEFAULT)
        )
    except (TypeError, ValueError):
        value = RECONNECT_GRACE_DEFAULT
    return max(0, min(RECONNECT_GRACE_MAX, value))


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
        self._recording_user = self.username
        self._output_dir = output_dir
        self._profile_saved_at = 0.0

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
            use_telegram=bool(channel_row["use_telegram"]),
            stop_event=self._stop_event,
            on_status=self._on_status,
            participants_interval=self._participants_interval,
            automatic_interval_getter=self._automatic_interval,
            reconnect_grace_getter=self._reconnect_grace,
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
        # These don't touch the DB - handle them without opening a
        # connection (they fire far more often than the recording ones).
        if phase == "checking":
            self._status_store.set(self.channel_id, phase="checking")
            return

        if phase == "offline":
            self._status_store.set(self.channel_id, phase="offline")
            return

        if phase == "waiting_return":
            # Live looked over; the recorder keeps the file open for a while
            # in case the creator comes back (dashboard + health monitor).
            self._status_store.set_flag(
                self.channel_id, "waiting_return_until", data.get("until")
            )
            return

        if phase == "waiting_return_done":
            self._status_store.set_flag(self.channel_id, "waiting_return_until", None)
            return

        if phase == "profile":
            # Arrives on every check (every few minutes); the avatar barely
            # changes, so refresh it at most a few times a day.
            if time.time() - self._profile_saved_at >= PROFILE_REFRESH_SECONDS:
                self._profile_saved_at = time.time()
                profiles.save_profile_in_background(
                    self._db_path,
                    self._get_connection,
                    self._output_dir,
                    data.get("user", self.username),
                    data.get("avatar_url"),
                    data.get("nickname"),
                )
            return

        conn = self._get_connection(self._db_path)
        try:
            if phase in ("recording_started", "recording_finished"):
                schedules.invalidate(data.get("user", self.username))

            if phase == "recording_started":
                self._recording_started_at = time.time()
                self._recording_user = data.get("user", self.username)
                self._current_recording_id = recordings_repo.start(
                    conn,
                    channel_id=self.channel_id,
                    username=self._recording_user,
                    file_path=data["file_path"],
                    format="ts",
                )
                webhook = self._notify_webhook(conn)
                if webhook:
                    discord_webhook.live_started(
                        webhook, self._recording_user, self._current_recording_id
                    )
                self._status_store.set(
                    self.channel_id, phase="recording", detail=data.get("file_path")
                )

            elif phase == "session_problem":
                self._alert_session_problem(conn, data)

            elif phase == "participants":
                if self._current_recording_id is not None:
                    participants_repo.replace_for_recording(
                        conn, self._current_recording_id, data.get("participants") or []
                    )

            elif phase == "recording_finished":
                self._finish_recording(conn, **data)

            elif phase == "recording_error":
                self._fail_recording(conn, data.get("error", "unknown error"))
        finally:
            conn.close()

    def _automatic_interval(self):
        conn = self._get_connection(self._db_path)
        try:
            row = channels_repo.get(conn, self.channel_id)
            if row is None:
                return None
            # Smart interval (opt-in per channel): faster in the creator's
            # usual hours, slower otherwise - never stops checking.
            minutes, _reason = schedules.effective_interval(conn, row)
            return minutes
        finally:
            conn.close()

    def _reconnect_grace(self):
        conn = self._get_connection(self._db_path)
        try:
            return reconnect_grace_seconds(conn)
        finally:
            conn.close()

    def _participants_interval(self):
        conn = self._get_connection(self._db_path)
        try:
            return settings_repo.get(conn, participants.SETTING_KEY)
        finally:
            conn.close()

    def _alert_session_problem(self, conn, data):
        user = data.get("user", self.username)
        if data.get("has_session"):
            alerts.record_session_problem(conn, user)
            alerts.send(
                conn,
                "session_rejected",
                "Tu sesión de TikTok no está funcionando",
                f"TikTok no dejó leer el live de @{user} (tiene restricción de "
                "edad) aunque hay una sesión configurada: probablemente venció. "
                "Se sigue grabando, pero sin los nombres de los invitados. "
                "Actualizá `sessionid_ss` en Ajustes → Cookies.",
                throttle_seconds=12 * 3600,
            )
        else:
            alerts.send(
                conn,
                f"age_restricted_no_session:{self.channel_id}",
                f"Live de @{user} con restricción de edad",
                "Se está grabando, pero sin sesión de TikTok no se pueden ver "
                "los invitados. Configurá `sessionid_ss` en Ajustes → Cookies.",
                throttle_seconds=24 * 3600,
            )

    def _notify_webhook(self, conn):
        """
        Discord webhook URL if this channel has notices on (its bell, stored
        in the legacy `use_telegram` column), else None. Read live from the
        DB, not cached at construction, so toggling it never needs a worker
        restart mid-recording.
        """
        row = channels_repo.get(conn, self.channel_id)
        if not (row and row["use_telegram"]):
            return None
        return settings_repo.get(conn, discord_webhook.SETTING_KEY)

    def _finish_recording(self, conn, *, file_path, format="ts", **_ignored):
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

        webhook = self._notify_webhook(conn)
        if webhook:
            discord_webhook.live_finished(
                webhook,
                self._recording_user,
                self._current_recording_id,
                duration_seconds=duration_seconds,
                size_bytes=file_size_bytes,
            )

        self._current_recording_id = None
        self._recording_started_at = None
        self._status_store.set(self.channel_id, phase="idle")

    def _fail_recording(self, conn, error_message):
        if self._current_recording_id is not None:
            recordings_repo.mark_failed(
                conn, self._current_recording_id, error_message=error_message
            )

        webhook = self._notify_webhook(conn)
        if webhook:
            discord_webhook.live_failed(
                webhook, self._recording_user, error_message, self._current_recording_id
            )

        self._current_recording_id = None
        self._recording_started_at = None
        self._status_store.set(self.channel_id, phase="error", detail=error_message)
