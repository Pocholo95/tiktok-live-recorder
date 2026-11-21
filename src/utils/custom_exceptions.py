class TikTokRecorderError(Exception):
    """Base exception for all TikTok Recorder errors."""

    def __init__(self, message):
        super().__init__(message)


class UserLiveError(TikTokRecorderError):
    """Error related to user live status."""

    def __init__(self, message):
        super().__init__(message)


class LiveBlockedError(UserLiveError):
    """
    TikTok won't let us watch: `reason` "restricted" (room info 4003110 -
    age/region restriction and no working session) or "waf" (the hub's IP
    is blocked). Unlike plain UserLiveError this is NOT "not live": the
    creator may well be live and we're losing it.
    """

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


class IPBlockedByWAF(TikTokRecorderError):
    """Raised when IP is blocked by WAF."""

    def __init__(self, message="IP blocked by WAF"):
        super().__init__(message)


class LiveNotFound(TikTokRecorderError):
    """Raised when a live stream is not found."""

    pass


class ArgsParseError(TikTokRecorderError):
    """Raised for argument parsing errors."""

    pass


class NetworkError(TikTokRecorderError):
    """Raised for network-related errors."""

    pass


class TikRecUnavailableError(TikTokRecorderError):
    """Raised when the tikrec signing service is unreachable or returns an unexpected response."""

    pass
