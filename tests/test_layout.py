import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # shared test helpers

from core.flv_stream import FlvNormalizer  # noqa: E402
from core.layout import LayoutTracker, extract_app_data, guest_panels  # noqa: E402
from core.participants import ParticipantTracker  # noqa: E402
from test_participants import _HANDLES, _Api, _room  # noqa: E402

HOST = "7710431027072453397_8720653144639331981_0"


def _app_data(*guest_cids):
    grids = [{"id": 1, "p": 0, "cid": HOST.split("_")[0], "uid_str": HOST}]
    for i, cid in enumerate(guest_cids, start=2):
        grids.append(
            {"id": i, "p": i - 1, "cid": cid, "uid_str": f"{cid}_9_0", "x": 0.5,
             "y": 0.25, "w": 0.5, "h": 0.25}
        )  # fmt: skip
    return {"grids": grids, "anchor_link_mic_id": HOST}


def _sei_frame(app_data):
    """Video tag payload as TikTok sends it: NALs with the SEI JSON inside."""
    outer = json.dumps({"app_data": json.dumps(app_data), "ts": 1795226380303})
    return b"\x27\x01\x00\x00\x00" + b"\x00\x00\x00\x40\x06\x05\xff" + outer.encode()


def test_extracts_app_data_from_a_video_frame():
    frame = _sei_frame(_app_data("111", "222")) + b"\x80\x00\x00\x10junk"
    data = extract_app_data(frame)
    assert data["anchor_link_mic_id"] == HOST
    assert extract_app_data(b"\x17\x01no sei here") is None
    assert extract_app_data(b'{"app_data":"{broken') is None


def test_guest_panels_leave_out_the_creator():
    assert set(guest_panels(_app_data("111", "222"))) == {"111", "222"}
    assert guest_panels({"grids": "nope"}) == {}


def test_panels_open_and_close_with_a_short_grace():
    layout = LayoutTracker()
    layout.observe(10, _app_data("111"))
    layout.observe(20, _app_data("111", "222"))
    layout.observe(30, _app_data("222"))  # 111 missing since 30
    layout.observe(33, _app_data("222"))  # only 3 s: still on screen
    assert layout.open_count() == 2
    layout.observe(36, _app_data("222"))  # 6 s missing: left at 30
    assert layout.snapshot() == [
        {"username": "", "start_seconds": 10, "end_seconds": 30},
        {"username": "", "start_seconds": 20, "end_seconds": None},
    ]


def test_silence_means_solo_again():
    layout = LayoutTracker()
    layout.observe(10, _app_data("111"))
    layout.observe(50, _app_data("111"))
    layout.tick(55)
    assert layout.open_count() == 1
    layout.tick(61)
    assert layout.snapshot()[0]["end_seconds"] == 50


def test_names_go_to_the_newest_unnamed_panel():
    layout = LayoutTracker()
    layout.observe(10, _app_data("111"))
    layout.name(["ana"])
    layout.observe(100, _app_data("111", "222"))
    layout.name(["ana"])  # already on screen: nothing changes
    layout.name(["beto"])
    layout.name(["carla"])  # no panel left without a name
    names = {p["start_seconds"]: p["username"] for p in layout.snapshot()}
    assert names == {10: "ana", 100: "beto"}


def test_feed_video_parses_at_most_once_a_second():
    layout = LayoutTracker()
    layout.feed_video(10_000, _sei_frame(_app_data("111")))
    layout.feed_video(10_500, _sei_frame(_app_data("111", "222")))  # skipped
    assert layout.open_count() == 1
    layout.feed_video(11_000, _sei_frame(_app_data("111", "222")))
    assert layout.open_count() == 2
    layout.feed_video(12_000, b"\x27\x01plain frame")
    assert layout.open_count() == 2


def test_normalizer_hands_every_video_frame_to_the_listener():
    from test_flv_stream import FLV_HEADER, _audio, _avc_config, _video

    seen = []
    n = FlvNormalizer(on_video_frame=lambda ts, data: seen.append(ts))
    n.feed(FLV_HEADER + _avc_config() + _video(5000) + _audio(5010) + _video(5033))
    assert seen == [0, 33]

    def broken(ts, data):
        raise ValueError("bad listener")

    n = FlvNormalizer(on_video_frame=broken)
    out = n.feed(FLV_HEADER + _avc_config() + _video(5000) + _video(5033))
    assert out  # the recording goes on


def test_tracker_names_video_panels_from_rotating_polls(tmp_path):
    now = [0.0]
    snapshots = []
    layout = LayoutTracker()
    # Room info names one linked creator per poll, rotating.
    api = _Api(
        [_room("200000000000"), _room("300000000000"), _room("200000000000")],
        _HANDLES,
    )
    tracker = ParticipantTracker(
        api, "room", snapshots.append, clock=lambda: now[0], layout=layout,
        debug_path=str(tmp_path / "d.jsonl"),
    )  # fmt: skip

    layout.observe(5, _app_data("111", "222"))
    for t in (30, 60, 90):  # polls name ana, beto, ana again
        layout.observe(t - 1, _app_data("111", "222"))  # ~every second, really
        now[0] = t
        tracker.poll_once()
    tracker.stop()

    final = snapshots[-1]
    # Both stay on screen the whole time: no come-and-go from the rotation.
    assert sorted(p["username"] for p in final) == ["ana", "beto"]
    assert all(p["start_seconds"] == 5 for p in final)  # from the video
    assert all(p["end_seconds"] == 89 for p in final)  # last layout seen
    lines = (tmp_path / "d.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"panels"' in line for line in lines)


def test_tracker_keeps_unnamed_panels_when_room_info_fails():
    now = [0.0]
    snapshots = []
    layout = LayoutTracker()
    api = _Api([RuntimeError("no session")], _HANDLES)
    tracker = ParticipantTracker(
        api, "room", snapshots.append, clock=lambda: now[0], layout=layout
    )
    layout.observe(5, _app_data("111"))
    layout.observe(29, _app_data("111"))
    now[0] = 30
    tracker.poll_once()
    assert snapshots[-1] == [{"username": "", "start_seconds": 5, "end_seconds": None}]
