import time
from http.client import HTTPException
from pathlib import Path
from threading import Thread

from requests import RequestException

from core.flv_stream import TsRemuxer
from core.participants import ParticipantTracker
from core.tiktok_api import TikTokAPI
from utils.custom_exceptions import LiveNotFound, TikTokRecorderError, UserLiveError
from utils.enums import Error, Mode, TikTokError, TimeOut
from utils.logger_manager import logger
from utils.recorder_config import RecorderConfig


class TikTokRecorder:
    def __init__(self, config: RecorderConfig):
        self.tiktok = TikTokAPI(proxy=config.proxy, cookies=config.cookies)

        self.url = config.url
        self.user = config.user
        self.room_id = config.room_id
        self.mode = config.mode
        self._automatic_interval = config.automatic_interval
        self._automatic_interval_getter = config.automatic_interval_getter
        self.duration = config.duration
        self.output = config.output
        self.bitrate = config.bitrate
        self.ffmpeg_path = config.ffmpeg_path
        self.use_telegram = config.use_telegram
        self._proxy = config.proxy
        self._cookies = config.cookies
        self._stop_event = config.stop_event
        self._on_status = config.on_status or (lambda *args, **kwargs: None)
        # Guest names are a hub feature (they're shown in the library); the
        # plain CLI has nowhere to put them, so don't poll TikTok for them.
        self._track_participants = config.on_status is not None
        self._participants_interval = config.participants_interval

    @property
    def automatic_interval(self):
        """
        Minutes between live checks. In the hub this is re-read from the
        channel's DB row before every wait, so editing it in the dashboard
        takes effect without restarting the worker (or cutting a recording).
        """
        if self._automatic_interval_getter is not None:
            try:
                value = int(self._automatic_interval_getter())
                if value >= 1:
                    return value
            except Exception:
                pass
        return self._automatic_interval

    @automatic_interval.setter
    def automatic_interval(self, value):
        self._automatic_interval = value

    def _stopped(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    def _wait(self, seconds: float) -> None:
        """Sleep, but wake immediately if a supervisor requests a stop."""
        if self._stop_event is not None:
            self._stop_event.wait(seconds)
        else:
            time.sleep(seconds)

    def _setup(self):
        """Resolve user/room data and validate prerequisites via network calls."""
        if self.mode == Mode.FOLLOWERS:
            self.check_country_blacklisted()

            self.sec_uid = self.tiktok.get_sec_uid()
            if self.sec_uid is None:
                raise TikTokRecorderError("Failed to retrieve sec_uid.")

            logger.info("Followers mode activated\n")
        else:
            if self.url:
                self.user, self.room_id = self.tiktok.get_room_and_user_from_url(
                    self.url
                )

            if not self.user:
                self.user = self.tiktok.get_user_from_room_id(self.room_id)

            if not self.room_id:
                self.room_id = self.tiktok.get_room_id_from_user(self.user)

            self.check_country_blacklisted()

            logger.info(f"USERNAME: {self.user}" + ("\n" if not self.room_id else ""))
            if self.room_id:
                logger.info(
                    f"ROOM_ID:  {self.room_id}"
                    + ("\n" if not self.tiktok.is_room_alive(self.room_id) else "")
                )

        # If proxy was used for the initial checks, switch to a direct connection
        # for the actual stream download to avoid proxy bottlenecks
        if self._proxy:
            self.tiktok = TikTokAPI(proxy=None, cookies=self._cookies)

    def run(self):
        """
        Resolves prerequisites and runs the recorder in the selected mode.

        If the mode is MANUAL, it checks if the user is currently live and
        if so, starts recording.

        If the mode is AUTOMATIC, it continuously checks if the user is live
        and if not, waits for the specified timeout before rechecking.
        If the user is live, it starts recording.

        if the mode is FOLLOWERS, it continuously checks the followers of
        the authenticated user. If any follower is live, it starts recording
        their live stream in a separate process.
        """
        self._setup()

        if self.mode == Mode.MANUAL:
            self.manual_mode()

        elif self.mode == Mode.AUTOMATIC:
            self.automatic_mode()

        elif self.mode == Mode.FOLLOWERS:
            self.followers_mode()

    def manual_mode(self):
        if not self.tiktok.is_room_alive(self.room_id):
            raise UserLiveError(f"@{self.user}: {TikTokError.USER_NOT_CURRENTLY_LIVE}")

        self.start_recording(self.user, self.room_id)

    def automatic_mode(self):
        while not self._stopped():
            try:
                self._on_status("checking", user=self.user)
                self.room_id = self.tiktok.get_room_id_from_user(self.user)
                self._report_profile()
                self.manual_mode()

            except (UserLiveError, LiveNotFound) as ex:
                logger.info(ex)
                logger.info(
                    f"Waiting {self.automatic_interval} minutes before recheck\n"
                )
                self._on_status("offline", user=self.user)
                self._wait(self.automatic_interval * TimeOut.ONE_MINUTE)

            except (ConnectionError, RequestException, HTTPException):
                logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                self._wait(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

    def _report_profile(self):
        info = getattr(self.tiktok, "last_user_info", None) or {}
        avatar_url = (
            info.get("avatarMedium")
            or info.get("avatarThumb")
            or info.get("avatarLarger")
        )
        if avatar_url or info.get("nickname"):
            self._on_status(
                "profile",
                user=self.user,
                avatar_url=avatar_url,
                nickname=info.get("nickname"),
            )

    def followers_mode(self):
        active_recordings = {}  # follower -> Thread

        while not self._stopped():
            try:
                self._on_status("checking", user=self.user)
                followers = self.tiktok.get_followers_list(self.sec_uid)

                for follower in followers:
                    if follower in active_recordings:
                        if not active_recordings[follower].is_alive():
                            logger.info(f"Recording of @{follower} finished.")
                            del active_recordings[follower]
                        else:
                            continue

                    try:
                        room_id = self.tiktok.get_room_id_from_user(follower)

                        if not room_id or not self.tiktok.is_room_alive(room_id):
                            continue

                        logger.info(f"@{follower} is live. Starting recording...")

                        thread = Thread(
                            target=self.start_recording,
                            args=(follower, room_id),
                            daemon=True,
                        )
                        thread.start()
                        active_recordings[follower] = thread

                        self._wait(2.5)

                    except TikTokRecorderError as e:
                        logger.error(f"Error while processing @{follower}: {e}")
                        continue

                    except Exception as e:
                        logger.error(
                            f"Unexpected error processing @{follower}: {e}",
                            exc_info=True,
                        )
                        continue

                print()
                logger.info(
                    f"Waiting {self.automatic_interval} minutes for the next check..."
                )
                self._on_status("offline", user=self.user)
                self._wait(self.automatic_interval * TimeOut.ONE_MINUTE)

            except (UserLiveError, LiveNotFound) as ex:
                logger.info(ex)
                logger.info(
                    f"Waiting {self.automatic_interval} minutes before recheck\n"
                )
                self._on_status("offline", user=self.user)
                self._wait(self.automatic_interval * TimeOut.ONE_MINUTE)

            except (ConnectionError, RequestException, HTTPException):
                logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                self._wait(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

    def _build_output_path(self, user: str) -> str:
        filename = (
            f"TK_{user}_{time.strftime('%Y.%m.%d_%H-%M-%S', time.localtime())}.ts"
        )
        if self.output:
            return str(Path(self.output) / filename)
        return filename

    def start_recording(self, user, room_id):
        """
        Start recording live
        """
        live_urls = self.tiktok.get_live_url_candidates(room_id, user=user)
        if not live_urls:
            raise LiveNotFound(TikTokError.RETRIEVE_LIVE_URL)

        output = self._build_output_path(user)
        self._on_status("recording_started", user=user, file_path=output)

        tracker = self._start_participant_tracker(user, room_id, output)
        try:
            self._record_stream(user, room_id, live_urls, output)
        finally:
            # Stopped before "recording_finished" so the final guest time
            # ranges still land on this recording.
            if tracker is not None:
                tracker.stop()
        self._on_status("recording_finished", user=user, file_path=output, format="ts")

    def _start_participant_tracker(self, user, room_id, output):
        """Best-effort: any failure only means no guest names for this live."""
        if not self._track_participants:
            return None
        try:
            tracker = ParticipantTracker(
                TikTokAPI(proxy=None, cookies=self._cookies),
                room_id,
                lambda snapshot: self._on_status(
                    "participants", user=user, participants=snapshot
                ),
                debug_path=str(Path(output).with_suffix(".participants.jsonl")),
                on_access_denied=lambda: self._on_status(
                    "session_problem",
                    user=user,
                    has_session=bool((self._cookies or {}).get("sessionid_ss")),
                ),
                **(
                    {"interval": self._participants_interval}
                    if self._participants_interval
                    else {}
                ),
            )
            tracker.start()
            return tracker
        except Exception as e:
            logger.warning(f"Guest name tracking not started: {e}")
            return None

    def _record_stream(self, user, room_id, live_urls, output):
        min_stream_bytes = 4096
        for index, live_url in enumerate(live_urls, start=1):
            if self.duration:
                logger.info(
                    f"Started recording for {self.duration} seconds "
                    f"(stream {index}/{len(live_urls)})"
                )
            else:
                logger.info(f"Started recording (stream {index}/{len(live_urls)})...")

            bytes_written = 0
            connections = 0

            logger.info("[PRESS CTRL + C ONCE TO STOP]")
            # Raw FLV is never written to disk: it's normalized (reconnects
            # and resolution changes spliced into one timeline) and remuxed
            # to MPEG-TS on the fly - see core/flv_stream.py. No transcode
            # happens on this machine; the MP4 is made offline on a GPU.
            remuxer = TsRemuxer(output, ffmpeg_path=self.ffmpeg_path)
            try:
                stop_recording = False
                stream_ended = False
                while not stop_recording:
                    try:
                        if not self.tiktok.is_room_alive(room_id):
                            logger.info("User is no longer live. Stopping recording.")
                            break

                        if self._stopped():
                            logger.info("Stop requested. Stopping recording.")
                            break

                        if connections:
                            logger.info("Stream connection dropped, reconnecting...")
                            remuxer.new_connection()
                        connections += 1

                        start_time = time.time()
                        for chunk in self.tiktok.download_live_stream(live_url):
                            remuxer.write(chunk)
                            bytes_written += len(chunk)

                            elapsed_time = time.time() - start_time
                            if self.duration and elapsed_time >= self.duration:
                                stop_recording = True
                                break

                            if self._stopped():
                                logger.info("Stop requested. Stopping recording.")
                                stop_recording = True
                                break
                        else:
                            stream_ended = True

                        if stream_ended and bytes_written < min_stream_bytes:
                            break

                    except ConnectionError:
                        if self.mode == Mode.AUTOMATIC:
                            logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                            self._wait(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

                    except (RequestException, HTTPException) as ex:
                        logger.warning(f"Network hiccup, retrying: {ex}")
                        self._wait(2)

                    except KeyboardInterrupt:
                        logger.info("Recording stopped by user.")
                        stop_recording = True

                    except Exception as ex:
                        logger.error(
                            f"Unexpected error during recording: {ex}",
                            exc_info=True,
                        )
                        stop_recording = True
            finally:
                remuxer.close()

            if bytes_written >= min_stream_bytes:
                break

            # The remuxer appends, so a dud candidate's few bytes must not
            # end up in front of the next candidate's stream.
            Path(output).unlink(missing_ok=True)
            logger.warning(
                f"Stream {index}/{len(live_urls)} returned only {bytes_written} bytes. "
                "Trying another CDN/quality..."
            )
        else:
            Path(output).unlink(missing_ok=True)
            raise LiveNotFound(TikTokError.RETRIEVE_LIVE_URL)

        logger.info(f"Recording finished: {Path(output).resolve()}\n")

    def check_country_blacklisted(self):
        is_blacklisted = self.tiktok.is_country_blacklisted()
        if not is_blacklisted:
            return False

        if self.room_id is None:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED)

        if self.mode == Mode.AUTOMATIC:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_AUTO_MODE)

        elif self.mode == Mode.FOLLOWERS:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_FOLLOWERS_MODE)

        return is_blacklisted
