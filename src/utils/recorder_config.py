from dataclasses import dataclass
from threading import Event
from typing import Callable

from utils.enums import Mode


@dataclass
class RecorderConfig:
    mode: Mode
    url: str | None = None
    user: str | None = None
    room_id: str | None = None
    automatic_interval: int = 5
    cookies: dict | None = None
    proxy: str | None = None
    output: str | None = None
    duration: int | None = None
    use_telegram: bool = False
    bitrate: str | None = None
    ffmpeg_path: str | None = None
    # Set by the hub's supervisor to run a recorder as a supervised, stoppable
    # worker (thread-safe cooperative stop + status reporting). Left as None
    # for plain CLI usage, where the process itself is the unit of control.
    stop_event: Event | None = None
    on_status: Callable[..., None] | None = None
