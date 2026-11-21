import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import stream_watchdog as wd  # noqa: E402
from core.stream_stats import AUDIO, VIDEO, StreamStats  # noqa: E402


class _Feed:
    """Feeds frames into StreamStats every 0.5 s of video and runs the
    watchdog after each one, like the recorder does after each chunk."""

    def __init__(self, tmp_path):
        self.stats = StreamStats(tmp_path / "s.stats.json")
        self.watchdog = wd.StreamWatchdog(self.stats)
        self.ms = 0
        self.events = []

    def run(self, seconds, audio=True, video=True, audio_size=200):
        for _ in range(int(seconds * 2)):
            if video:
                self.stats.frame(VIDEO, self.ms, 3000)
            if audio:
                self.stats.frame(AUDIO, self.ms + 10, audio_size)
            event = self.watchdog.check()
            if event:
                self.events.append((round(self.ms / 1000), event["action"]))
            self.ms += 500
        return self


def test_missing_audio_reconnects_with_backoff_then_gives_up(tmp_path):
    feed = _Feed(tmp_path).run(10).run(800, audio=False)

    assert feed.events == [
        (10 + wd.AUDIO_MISSING_SECONDS, "reconnect"),
        (10 + 45 + 120, "reconnect"),
        (10 + 45 + 120 + 300, "reconnect"),
        (10 + 45 + 120 + 300 + 300, "give_up"),
    ]

    feed.events.clear()
    feed.run(5)
    assert feed.events == [(810, "recovered")]
    logged = [e["action"] for e in feed.stats.to_dict()["watchdog"]]
    assert logged == ["reconnect"] * 3 + ["give_up", "recovered"]


def test_audio_back_after_a_reconnect_ends_the_episode(tmp_path):
    feed = _Feed(tmp_path).run(10).run(50, audio=False).run(30)
    assert [a for _, a in feed.events] == ["reconnect", "recovered"]

    # A new episode starts from scratch.
    feed.events.clear()
    feed.run(50, audio=False)
    assert [a for _, a in feed.events] == ["reconnect"]


def test_muted_mic_and_short_gaps_never_trip_it(tmp_path):
    feed = _Feed(tmp_path).run(300, audio_size=6)  # silent AAC frames
    feed.run(30, audio=False).run(30)  # shorter than the threshold
    assert feed.events == []


def test_missing_video_waits_longer(tmp_path):
    feed = _Feed(tmp_path).run(10).run(130, video=False)
    assert feed.events == [(10 + wd.VIDEO_MISSING_SECONDS, "reconnect")]


def test_broken_stats_never_raise(tmp_path):
    class Broken:
        media_seconds = 0

        def missing_seconds(self, kind):
            raise RuntimeError("boom")

    assert wd.StreamWatchdog(Broken()).check() is None
