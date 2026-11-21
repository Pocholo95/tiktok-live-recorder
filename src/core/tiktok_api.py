import html
import json
import re
from urllib.parse import urlparse

from http_utils.http_client import HttpClient
from utils.enums import StatusCode, TikTokError
from utils import session_log
from utils.logger_manager import logger
from utils.custom_exceptions import (
    LiveBlockedError,
    UserLiveError,
    TikTokRecorderError,
    LiveNotFound,
    TikRecUnavailableError,
)


# user/room API statusCode for an @ that doesn't exist.
USER_NOT_FOUND = 19881007
# Room `status`: 2 = live, 3 = live but paused by the creator, 4 = ended.
# A pause must not end the recording (upstream #463/#483).
LIVE_ROOM_STATUSES = frozenset({"2", "3"})
# Stream connection: (connect, read) seconds. A CDN connection that stays
# open but stops sending data raises after the read timeout, and the
# recorder reconnects instead of hanging forever (upstream #478).
STREAM_TIMEOUT = (10, 30)


def _is_hls(url):
    return urlparse(url).path.lower().endswith((".m3u8", ".m3u"))


def _sdk_params(entry):
    """Quality facts TikTok publishes per variant: bitrate, size, codec."""
    raw = (entry.get("main") or {}).get("sdk_params")
    try:
        params = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        params = {}
    width = height = 0
    match = re.match(r"(\d+)x(\d+)", str(params.get("resolution") or ""))
    if match:
        width, height = int(match.group(1)), int(match.group(2))
    try:
        vbitrate = int(params.get("vbitrate") or 0)
    except (TypeError, ValueError):
        vbitrate = 0
    return {
        "vbitrate": vbitrate,
        "width": width,
        "height": height,
        "codec": str(params.get("VCodec") or params.get("v_codec") or ""),
    }


class TikTokAPI:
    def __init__(self, proxy, cookies):
        self.BASE_URL = "https://www.tiktok.com"
        self.WEBCAST_URL = "https://webcast.tiktok.com"
        self.API_URL = "https://www.tiktok.com/api-live/user/room/"
        self.EULER_API = "https://tiktok.eulerstream.com"
        self.TIKREC_API = "https://tikrec.com"
        self.last_user_info = {}
        self.last_room_status_code = None
        self._has_session = bool((cookies or {}).get("sessionid_ss"))
        # Filled by get_live_urls: what each candidate URL is (quality,
        # resolution, bitrate, codec) and its backup-CDN twin.
        self.stream_info = {}
        self._alternates = {}

        self.http_client = HttpClient(proxy, cookies).req
        self._http_client_stream = HttpClient(proxy, cookies).req_stream

    def _is_authenticated(self) -> bool:
        response = self.http_client.get(f"{self.BASE_URL}/foryou")
        response.raise_for_status()

        content = response.text
        return "login-title" not in content

    def is_country_blacklisted(self) -> bool:
        """
        Checks if the user is in a blacklisted country that requires login
        """
        response = self.http_client.get(f"{self.BASE_URL}/live", allow_redirects=False)

        return response.status_code == StatusCode.REDIRECT

    def is_room_alive(self, room_id: str) -> bool:
        """
        Checking whether the user is live.
        """
        if not room_id:
            raise UserLiveError(TikTokError.USER_NOT_CURRENTLY_LIVE)

        alive_data = self.http_client.get(
            f"{self.WEBCAST_URL}/webcast/room/check_alive/"
            f"?aid=1988&region=CH&room_ids={room_id}&user_is_login=true"
        ).json()

        data_list = alive_data.get("data")
        if (
            not isinstance(data_list, list)
            or not data_list
            or not isinstance(data_list[0], dict)
            or not data_list[0].get("alive", False)
        ):
            return False

        room_info = self.http_client.get(
            f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}"
        ).json()

        status_code = room_info.get("status_code", 0)
        if status_code == 4003110:
            return True

        if status_code != 0:
            return False

        room_data = room_info.get("data") or {}
        room_status = room_data.get("status")
        if room_status is not None and str(room_status) not in LIVE_ROOM_STATUSES:
            return False

        stream_url = room_data.get("stream_url") or {}
        sdk_stream_data = (
            (stream_url.get("live_core_sdk_data") or {})
            .get("pull_data", {})
            .get("stream_data")
        )

        return bool(
            sdk_stream_data
            or stream_url.get("flv_pull_url")
            or stream_url.get("hls_pull_url")
            or stream_url.get("hls_pull_url_map")
            or stream_url.get("rtmp_pull_url")
        )

    def get_room_data(self, room_id):
        """Room info `data` dict, or None if TikTok refused it (e.g. 4003110)."""
        room_info = self.http_client.get(
            f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}"
        ).json()
        # Kept so callers can tell "access denied" (4003110: age-restricted
        # live without a working session) from other failures.
        self.last_room_status_code = room_info.get("status_code", 0)
        if self.last_room_status_code == 4003110:
            session_log.note_refusal(
                None,
                room_id,
                "room_info",
                getattr(self, "_has_session", False),
                room_info.get("data"),
            )
        if self.last_room_status_code != 0:
            return None
        return room_info.get("data") or None

    def get_room_owner(self, room_id):
        """@handle of whoever hosts `room_id`, or None (unknown room, or
        TikTok refused it - e.g. 4003110 for an age-restricted live without
        a working session). Doesn't touch `last_room_status_code`, which
        belongs to the recorded room."""
        try:
            info = self.http_client.get(
                f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}"
            ).json()
        except Exception:
            return None
        if info.get("status_code", 0) != 0:
            return None
        owner = (info.get("data") or {}).get("owner") or {}
        return owner.get("display_id") or None

    def get_sec_uid(self):
        """
        Returns the sec_uid of the authenticated user.
        """
        response = self.http_client.get(f"{self.BASE_URL}/foryou")

        sec_uid = re.search('"secUid":"(.*?)",', response.text)
        if sec_uid:
            sec_uid = sec_uid.group(1)

        return sec_uid

    def get_user_from_room_id(self, room_id) -> str:
        """
        Given a room_id, I get the username
        """
        data = self.http_client.get(
            f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}"
        ).json()

        if "Follow the creator to watch their LIVE" in json.dumps(data):
            raise UserLiveError(TikTokError.ACCOUNT_PRIVATE_FOLLOW)

        if "This account is private" in data:
            raise UserLiveError(TikTokError.ACCOUNT_PRIVATE)

        display_id = data.get("data", {}).get("owner", {}).get("display_id")
        if display_id is None:
            raise TikTokRecorderError(TikTokError.USERNAME_ERROR)

        return display_id

    def get_room_and_user_from_url(self, live_url: str):
        """
        Given a url, get user and room_id.
        """
        response = self.http_client.get(live_url, allow_redirects=False)
        content = response.text

        if response.status_code == StatusCode.REDIRECT:
            raise UserLiveError(TikTokError.COUNTRY_BLACKLISTED)

        if response.status_code == StatusCode.MOVED:  # MOBILE URL
            matches = re.findall("com/@(.*?)/live", content)
            if len(matches) < 1:
                raise LiveNotFound(TikTokError.INVALID_TIKTOK_LIVE_URL)

            user = matches[0]

        # https://www.tiktok.com/@<username>/live
        match = re.match(r"https?://(?:www\.)?tiktok\.com/@([^/]+)/live", live_url)
        if match:
            user = match.group(1)

        room_id = self.get_room_id_from_user(user)

        return user, room_id

    def _old_get_room_id_from_user(self, user: str) -> str:
        params = {"uniqueId": user, "giftInfo": "false"}

        response = self.http_client.get(
            f"{self.EULER_API}/webcast/room_info",
            params=params,
            headers={"x-api-key": ""},
        )

        if response.status_code != 200:
            raise UserLiveError(TikTokError.ROOM_ID_ERROR)

        data = response.json()

        room_id = data.get("data", {}).get("room_info", {}).get("id")
        if not room_id:
            raise UserLiveError(TikTokError.ROOM_ID_ERROR)

        return room_id

    def _tikrec_get_room_id_signed_url(self, user: str) -> str:
        try:
            response = self.http_client.get(
                f"{self.TIKREC_API}/tiktok/room/api/sign",
                params={"unique_id": user},
            )
            response.raise_for_status()
        except Exception as e:
            raise TikRecUnavailableError(
                f"tikrec signing service is unreachable: {e}"
            ) from e

        try:
            data = response.json()
        except ValueError as e:
            raise TikRecUnavailableError(
                "tikrec signing service returned an invalid response "
                "(expected JSON, got something else — the service may be down)."
            ) from e

        signed_path = data.get("signed_path")
        if not signed_path:
            raise TikRecUnavailableError(
                "tikrec signing service did not return a signed_path "
                "(the service may be down or overloaded)."
            )

        return f"{self.BASE_URL}{signed_path}"

    def _unsigned_user_room(self, user: str) -> dict | None:
        """
        `data` of TikTok's user/room API asked WITHOUT a signature (what
        streamlink does; checked 2026-11-21) - roomId, live status and the
        profile. None if TikTok didn't answer it properly (WAF page, error
        code, unexpected shape), so the caller can try the signed route.
        """
        try:
            response = self.http_client.get(
                self.API_URL.rstrip("/"),
                params={
                    "aid": "1988",
                    "sourceType": "54",
                    "staleTime": "600000",
                    "uniqueId": user,
                },
            )
            if response.status_code != 200:
                return None
            data = response.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("statusCode") == USER_NOT_FOUND:
            return {"user": {}}  # a clear answer: no such user, no room
        if data.get("statusCode", 0) != 0:
            return None
        room_data = data.get("data")
        user_info = (
            (room_data or {}).get("user") if isinstance(room_data, dict) else None
        )
        if not isinstance(user_info, dict) or "roomId" not in user_info:
            return None
        return room_data

    def get_room_id_from_user(self, user: str) -> str | None:
        """
        Given a username, get the room_id. Asks TikTok directly first; only
        if that fails, the signed route via tikrec.com (the original
        project's signing service, often down), then Euler Stream.
        """
        room_data = self._unsigned_user_room(user)
        if room_data is not None:
            self._unsigned_failures = 0
            user_info = room_data.get("user") or {}
            # Same response carries the profile (avatar, display name) - keep
            # it so the hub can show creator pictures without another request.
            self.last_user_info = user_info
            return user_info.get("roomId")

        failures = getattr(self, "_unsigned_failures", 0) + 1
        self._unsigned_failures = failures
        if failures == 1 or failures % 20 == 0:
            logger.warning(
                f"TikTok refused the unsigned room lookup for @{user} "
                f"({failures} in a row); using tikrec."
            )
        return self._signed_room_id_from_user(user)

    def _signed_room_id_from_user(self, user: str) -> str | None:
        try:
            signed_url = self._tikrec_get_room_id_signed_url(user)
        except TikRecUnavailableError as e:
            logger.warning(
                f"[!] tikrec is unavailable ({e}). "
                "Falling back to unsigned API — recording continues but may be less reliable."
            )
            return self._old_get_room_id_from_user(user)

        response = self.http_client.get(signed_url)
        content = response.text

        if not content or "Please wait" in content:
            raise LiveBlockedError(TikTokError.WAF_BLOCKED, "waf")

        data = response.json()
        user_info = (data.get("data") or {}).get("user", {}) or {}
        self.last_user_info = user_info
        return user_info.get("roomId")

    def get_followers_list(self, sec_uid) -> list:
        """
        Returns all followers for the authenticated user by paginating
        """
        followers = []
        cursor = 0
        has_more = True

        ms_token = self.http_client.get(
            f"{self.BASE_URL}/api/user/list/?"
            "WebIdLastTime=1747672102&aid=1988&app_language=it-IT&app_name=tiktok_web&"
            "browser_language=it-IT&browser_name=Mozilla&browser_online=true&"
            "browser_platform=Linux%20x86_64&"
            "browser_version=5.0%20%28X11%3B%20Linux%20x86_64%29%20AppleWebKit%2F537.36%20%28KHTML%2C%20like%20Gecko%29%20Chrome%2F140.0.0.0%20Safari%2F537.36&"
            "channel=tiktok_web&cookie_enabled=true&count=5&data_collection_enabled=true&"
            "device_id=7506194516308166166&device_platform=web_pc&focus_state=true&"
            "from_page=user&history_len=3&is_fullscreen=false&is_page_visible=true&"
            "maxCursor=0&minCursor=0&odinId=7246312836442604570&os=linux&priority_region=IT&"
            "referer=&region=IT&root_referer=https%3A%2F%2Fwww.tiktok.com%2Flive&scene=21&"
            "screen_height=1080&screen_width=1920&tz_name=Europe%2FRome&user_is_login=true&"
            "verifyFp=verify_mh4yf0uq_rdjp1Xwt_OoTk_4Jrf_AS8H_sp31opbnJFre&webcast_language=it-IT&"
            "msToken=GphHoLvRR4QxA5AWVwDkrs3AbumoK5H8toE8LVHtj6cce3ToGdXhMfvDWzOXG-0GXUWoaGVHrwGNA4k_NnjuFFnHgv2S5eMjsvtkAhwMPa13xLmvP7tumx0KreFjPwTNnOj-BvAkPdO5Zrev3hoFBD9lHVo=&X-Bogus=&X-Gnarly="
        ).cookies["msToken"]

        while has_more:
            url = (
                "https://www.tiktok.com/api/user/list/?"
                "WebIdLastTime=1747672102&aid=1988&app_language=it-IT&app_name=tiktok_web"
                "&browser_language=it-IT&browser_name=Mozilla&browser_online=true"
                "&browser_platform=Linux%20x86_64&browser_version=5.0%20%28X11%3B%20Linux%20x86_64%29%20AppleWebKit%2F537.36%20%28KHTML%2C%20like%20Gecko%29%20Chrome%2F140.0.0.0%20Safari%2F537.36&channel=tiktok_web&"
                "cookie_enabled=true&count=5&data_collection_enabled=true&device_id=7506194516308166166"
                "&device_platform=web_pc&focus_state=true&from_page=user&history_len=3&"
                f"is_fullscreen=false&is_page_visible=true&maxCursor={cursor}&minCursor={cursor}&"
                "odinId=7246312836442604570&os=linux&priority_region=IT&referer=&"
                "region=IT&scene=21&screen_height=1080&screen_width=1920"
                "&tz_name=Europe%2FRome&user_is_login=true&"
                f"secUid={sec_uid}&verifyFp=verify_mh4yf0uq_rdjp1Xwt_OoTk_4Jrf_AS8H_sp31opbnJFre&"
                f"webcast_language=it-IT&msToken={ms_token}&X-Bogus=&X-Gnarly="
            )

            response = self.http_client.get(url)

            if response.status_code != StatusCode.OK:
                raise TikTokRecorderError("Failed to retrieve followers list.")

            if not response.content:
                raise TikTokRecorderError("Empty response from TikTok followers API.")

            data = response.json()
            user_list = data.get("userList", [])

            for user in user_list:
                username = user.get("user", {}).get("uniqueId")
                if username:
                    followers.append(username)

            has_more = data.get("hasMore", False)
            new_cursor = data.get("minCursor", 0)

            if new_cursor == cursor:
                break

            cursor = new_cursor

        if not followers:
            raise TikTokRecorderError("Followers list is empty.")

        return followers

    def _get_stream_url_from_page(self, user: str) -> str | None:
        """
        Fallback: fetch the live page HTML and extract the stream URL directly.
        Used when the webcast API returns status code 4003110 (WAF/access restriction).
        """
        try:
            live_page_url = f"{self.BASE_URL}/@{user}/live"
            response = self.http_client.get(live_page_url)
            content = response.text

            flv_matches = re.findall(r'https?://[^\s"\'<>]+\.flv[^\s"\'<>]*', content)
            if flv_matches:
                # Prefer original (_or4) or SD quality
                for url in flv_matches:
                    url = html.unescape(url.rstrip("\\"))
                    if "_or4" in url or "_sd" in url:
                        logger.info(f"Found stream URL from page: {url[:80]}...")
                        return url
                return html.unescape(flv_matches[0].rstrip("\\"))

            hls_matches = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', content)
            if hls_matches:
                return html.unescape(hls_matches[0].rstrip("\\"))

            return None
        except Exception as e:
            logger.warning(f"Failed to extract stream URL from page: {e}")
            return None

    def _add_live_url_candidate(self, candidates: list[str], url: str | None) -> None:
        # Only FLV: the recorder splices FLV tags (core/flv_stream.py); an
        # HLS playlist in the list ended the recording with "not FLV".
        if url and url not in candidates and not _is_hls(url):
            candidates.append(url)

    def alternate_url(self, url):
        """The same variant on TikTok's backup CDN (or back to the main
        one), or None. Used when a connection keeps losing audio/video."""
        return getattr(self, "_alternates", {}).get(url)

    def get_live_urls(self, room_id: str, user: str = None) -> list[str]:
        """
        Return candidate CDN URLs (flv or m3u8) for the streaming.
        If the API returns status code 4003110 and a username is provided,
        falls back to scraping the live page directly.
        """
        data = self.http_client.get(
            f"{self.WEBCAST_URL}/webcast/room/info/?aid=1988&room_id={room_id}"
        ).json()

        if "This account is private" in data:
            raise UserLiveError(TikTokError.ACCOUNT_PRIVATE)

        status_code = data.get("status_code", 0)

        if status_code == 4003110:
            session_log.note_refusal(
                user,
                room_id,
                "live_urls",
                getattr(self, "_has_session", False),
                data.get("data"),
            )
            if user:
                logger.info(
                    "API blocked by WAF (4003110). Trying fallback: extract stream URL from live page..."
                )
                fallback_url = self._get_stream_url_from_page(user)
                if fallback_url and not _is_hls(fallback_url):
                    return [fallback_url]

            raise LiveBlockedError(TikTokError.LIVE_RESTRICTION, "restricted")

        room_data = data.get("data") or {}
        room_status = room_data.get("status")
        if room_status is not None and str(room_status) not in LIVE_ROOM_STATUSES:
            raise UserLiveError(TikTokError.USER_NOT_CURRENTLY_LIVE)

        stream_url = room_data.get("stream_url", {})

        sdk_data_str = (
            stream_url.get("live_core_sdk_data", {})
            .get("pull_data", {})
            .get("stream_data")
        )
        candidates = []
        if not sdk_data_str:
            logger.warning(
                "No SDK stream data found. Falling back to legacy URLs. Consider contacting the developer to update the code."
            )
            flv_pull_url = stream_url.get("flv_pull_url", {})
            for key in ("FULL_HD1", "HD1", "SD2", "SD1"):
                self._add_live_url_candidate(candidates, flv_pull_url.get(key))
            self._add_live_url_candidate(candidates, stream_url.get("hls_pull_url"))
            self._add_live_url_candidate(candidates, stream_url.get("rtmp_pull_url"))
            return candidates

        # Extract stream options
        sdk_data = json.loads(sdk_data_str).get("data", {})
        qualities = (
            stream_url.get("live_core_sdk_data", {})
            .get("pull_data", {})
            .get("options", {})
            .get("qualities", [])
        )
        if not qualities:
            logger.warning("No qualities found in the stream data. Returning None.")
            return candidates
        level_map = {q["sdk_key"]: q["level"] for q in qualities}

        # Best real quality first: published bitrate, then resolution, then
        # TikTok's level (some variants publish no bitrate). The audio-only
        # variant ("ao", `only_audio=1`) always goes last.
        def rank(key):
            facts = _sdk_params(sdk_data[key])
            audio_only = key == "ao" or "only_audio" in str(
                (sdk_data[key].get("main") or {}).get("flv", "")
            )
            return (
                not audio_only,
                facts["vbitrate"],
                facts["width"] * facts["height"],
                level_map.get(key, -1),
            )

        self.stream_info = {}
        self._alternates = {}
        for sdk_key in sorted(sdk_data.keys(), key=rank, reverse=True):
            entry = sdk_data[sdk_key]
            main_url = (entry.get("main") or {}).get("flv")
            backup_url = (entry.get("backup") or {}).get("flv")
            facts = {"quality": sdk_key, **_sdk_params(entry)}
            if main_url:
                self.stream_info[main_url] = {**facts, "cdn": "main"}
            if backup_url:
                self.stream_info[backup_url] = {**facts, "cdn": "backup"}
            if main_url and backup_url and main_url != backup_url:
                self._alternates[main_url] = backup_url
                self._alternates[backup_url] = main_url
            self._add_live_url_candidate(candidates, main_url)

        flv_pull_url = stream_url.get("flv_pull_url", {})
        for key in ("FULL_HD1", "HD1", "SD2", "SD1"):
            self._add_live_url_candidate(candidates, flv_pull_url.get(key))
        self._add_live_url_candidate(candidates, stream_url.get("hls_pull_url"))
        self._add_live_url_candidate(candidates, stream_url.get("rtmp_pull_url"))

        return candidates

    def get_live_url(self, room_id: str, user: str = None) -> str | None:
        """Return the first candidate CDN URL for the streaming."""
        live_urls = self.get_live_urls(room_id, user=user)
        if live_urls:
            return live_urls[0]
        return None

    def get_live_url_candidates(self, room_id: str, user: str = None) -> list[str]:
        """Return candidate CDN URLs for the streaming."""
        return self.get_live_urls(room_id, user=user)

    def download_live_stream(self, live_url: str):
        """Generator that returns the live stream for a given room_id."""
        stream = self._http_client_stream.get(
            live_url, stream=True, timeout=STREAM_TIMEOUT
        )
        try:
            for chunk in stream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            # The recorder may stop reading mid-stream (stop, or the stream
            # watchdog asking for a fresh connection): free the socket now.
            stream.close()
