import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.participants import (  # noqa: E402
    ParticipantTracker,
    UsernameResolver,
    extract_participant_ids,
)


def test_extract_ids_from_cohost_and_guest_lists():
    room = {
        "owner": {"id_str": "111111111111"},
        "link_mic": {
            "rival_anchor_id": 222222222222,
            "linked_user_list": [
                {"user": {"id_str": "333333333333", "nickname": "guest"}},
                {"user_id": "111111111111"},  # the owner is never a guest
            ],
            "show_user_list": [{"id": 444444444444}],
            "battle_scores": [{"user_id": 555555555555}],  # not "on screen" data
        },
    }
    assert extract_participant_ids(room) == {
        "222222222222",
        "333333333333",
        "444444444444",
    }


def test_extract_ignores_empty_link_mic():
    room = {"owner": {"id_str": "1"}, "link_mic": {"rival_anchor_id": 0}}
    assert extract_participant_ids(room) == set()
    assert extract_participant_ids({}) == set()


class _Http:
    def __init__(self, locations):
        self.locations = locations
        self.calls = 0

    def get(self, url, allow_redirects=True):
        self.calls += 1
        location = self.locations.get(url.rsplit("/", 1)[-1], "")

        class _Response:
            headers = {"location": location}

        return _Response()


def test_resolver_follows_share_redirect_and_caches():
    http = _Http({"42": "https://www.tiktok.com/@some.user_1?_r=1"})
    resolver = UsernameResolver(http)

    assert resolver.resolve("42") == "some.user_1"
    assert resolver.resolve("42") == "some.user_1"
    assert http.calls == 1


def test_resolver_returns_none_for_unknown_ids():
    http = _Http({"1": "https://www.tiktok.com/404?fromUrl=x"})
    assert UsernameResolver(http).resolve("1") is None


class _Api:
    def __init__(self, rooms, locations):
        self.rooms = list(rooms)
        self.http_client = _Http(locations)

    def get_room_data(self, room_id):
        room = self.rooms.pop(0)
        if isinstance(room, Exception):
            raise room
        return room


def _room(*guest_ids):
    return {
        "owner": {"id_str": "1"},
        "link_mic": {"linked_user_list": [{"user_id": g} for g in guest_ids]},
    }


_HANDLES = {
    "200000000000": "https://www.tiktok.com/@ana",
    "300000000000": "https://www.tiktok.com/@beto",
}


def _run_polls(tracker, now, times):
    for t in times:
        now[0] = t
        tracker.poll_once()


def test_tracker_closes_only_after_two_misses_and_merges_short_gaps():
    now = [0.0]
    snapshots = []
    api = _Api(
        [
            _room("200000000000"),  # t=10 ana joins
            _room("200000000000", "300000000000"),  # t=40 beto joins
            _room("300000000000"),  # t=70 ana missing (1st miss)
            _room("300000000000"),  # t=100 ana missing (2nd) -> left at 70
            _room("300000000000"),  # t=400
            _room("200000000000", "300000000000"),  # t=430 ana back, gap 360 s
        ],
        _HANDLES,
    )
    tracker = ParticipantTracker(api, "room", snapshots.append, clock=lambda: now[0])

    _run_polls(tracker, now, (10, 40, 70, 100, 400, 430))
    now[0] = 460
    tracker.stop()

    assert snapshots[-1] == [
        {"username": "ana", "start_seconds": 10, "end_seconds": 70},
        {"username": "beto", "start_seconds": 40, "end_seconds": 460},
        {"username": "ana", "start_seconds": 430, "end_seconds": 460},
    ]


def test_single_missing_poll_does_not_split_a_stay():
    now = [0.0]
    snapshots = []
    api = _Api([_room("200000000000"), _room(), _room("200000000000")], _HANDLES)
    tracker = ParticipantTracker(api, "room", snapshots.append, clock=lambda: now[0])

    _run_polls(tracker, now, (0, 30, 60))
    now[0] = 90
    tracker.stop()

    assert snapshots[-1] == [{"username": "ana", "start_seconds": 0, "end_seconds": 90}]


def test_reported_case_short_swap_stays_one_row_per_person():
    # Real report: @favoritamariiaofi 12:11-13:42, @favoritarespaldo
    # 13:42-14:43, @favoritamariiaofi 14:43-19:08 although she never left.
    now = [0.0]
    snapshots = []
    rooms = (
        [_room("200000000000")] * 4  # mariia (as "ana") 0-90
        + [_room("300000000000")] * 2  # TikTok points at respaldo for ~1 min
        + [_room("200000000000")] * 3
    )
    tracker = ParticipantTracker(
        _Api(rooms, _HANDLES), "room", snapshots.append, clock=lambda: now[0]
    )

    _run_polls(tracker, now, range(0, 270, 30))
    now[0] = 270
    tracker.stop()

    rows = snapshots[-1]
    assert [r["username"] for r in rows] == ["ana", "beto"]
    assert rows[0] == {"username": "ana", "start_seconds": 0, "end_seconds": 270}


def test_diagnostics_written_only_when_linked_ids_change(tmp_path):
    import json

    now = [0.0]
    debug = tmp_path / "rec.participants.jsonl"
    api = _Api(
        [_room("200000000000"), _room("200000000000"), _room("300000000000")],
        _HANDLES,
    )
    tracker = ParticipantTracker(
        api, "room", lambda s: None, clock=lambda: now[0], debug_path=str(debug)
    )

    _run_polls(tracker, now, (0, 30, 60))

    lines = [
        json.loads(line) for line in debug.read_text(encoding="utf-8").splitlines()
    ]
    assert [line["ids"] for line in lines] == [["200000000000"], ["300000000000"]]
    assert lines[1]["resolved"] == {"300000000000": "beto"}
    assert "link_mic" in lines[0]


def test_tracker_failures_never_raise_and_keep_state():
    now = [0.0]
    snapshots = []
    api = _Api(
        [
            _room("200000000000"),
            RuntimeError("TikTok said no"),
            None,
            _room("200000000000"),
        ],
        _HANDLES,
    )
    tracker = ParticipantTracker(api, "room", snapshots.append, clock=lambda: now[0])

    for t in (5, 35, 65, 95):
        now[0] = t
        tracker.poll_once()  # must not raise on the error / None polls

    # Failed polls don't count as "ana left".
    assert snapshots[-1] == [
        {"username": "ana", "start_seconds": 5, "end_seconds": None}
    ]


def test_tracker_without_any_guest_emits_nothing():
    snapshots = []
    tracker = ParticipantTracker(_Api([_room()], {}), "room", snapshots.append)
    tracker.poll_once()
    tracker.stop()
    assert snapshots == []


def test_interval_is_read_live_and_clamped():
    from core.participants import clamp_interval

    assert clamp_interval(None) == 30
    assert clamp_interval("abc") == 30
    assert clamp_interval("2") == 10
    assert clamp_interval(10_000) == 600

    value = ["45"]
    tracker = ParticipantTracker(
        _Api([], {}), "room", lambda s: None, interval=lambda: value[0]
    )
    assert tracker._current_interval() == 45
    value[0] = "120"  # changed in Settings while recording
    assert tracker._current_interval() == 120

    def broken():
        raise RuntimeError("db locked")

    assert (
        ParticipantTracker(
            _Api([], {}), "r", lambda s: None, interval=broken
        )._current_interval()
        == 30
    )
