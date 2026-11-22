import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.layout import LayoutTracker  # noqa: E402
from supervisor import names_overlay  # noqa: E402
from test_guest_times import _pts_bytes  # noqa: E402

HOST_CID = "7000000000000000001"
HOST_UID = f"{HOST_CID}_1_0"


def _app_data(*guests):
    """guests: (cid, x, y) - the host panel is always on the left."""
    grids = [
        {"cid": HOST_CID, "uid_str": HOST_UID, "x": 0, "y": 0.25, "w": 0.5, "h": 0.5}
    ]
    for cid, x, y in guests:
        grids.append(
            {"cid": cid, "uid_str": f"{cid}_2_0", "x": x, "y": y, "w": 0.5, "h": 0.25}
        )
    return {"grids": grids, "anchor_link_mic_id": HOST_UID}


def _ts_frame(pts, app_data, pid=256):
    outer = json.dumps({"app_data": json.dumps(app_data), "ts": 1795226380303})
    body = b"\x00\x00\x01\x06" + outer.encode()
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + _pts_bytes(pts) + body
    pes += b"\xff" * (-len(pes) % 184)
    packets = bytearray()
    for i in range(0, len(pes), 184):
        flags = 0x40 if i == 0 else 0x00
        packets += bytes([0x47, flags | (pid >> 8), pid & 0xFF, 0x10])
        packets += pes[i : i + 184]
    return bytes(packets)


def _participants(path, names):
    line = {
        "elapsed_seconds": 1,
        "panels": {cid: {"name": n} for cid, n in names.items()},
    }
    path.with_suffix(".participants.jsonl").write_text(json.dumps(line) + "\n")


def test_labels_follow_panels_when_they_move():
    timeline = [
        {"t": 10, "grids": [{"cid": HOST_CID, "host": True, "box": [0, 0.25, 0.5, 0.5]},
                            {"cid": "A", "host": False, "box": [0.5, 0.25, 0.5, 0.25]},
                            {"cid": "B", "host": False, "box": [0.5, 0.5, 0.5, 0.25]}]},
        # A and B swap places without anyone joining or leaving.
        {"t": 20, "grids": [{"cid": HOST_CID, "host": True, "box": [0, 0.25, 0.5, 0.5]},
                            {"cid": "A", "host": False, "box": [0.5, 0.5, 0.5, 0.25]},
                            {"cid": "B", "host": False, "box": [0.5, 0.25, 0.5, 0.25]}]},
        {"t": 30, "grids": []},
    ]  # fmt: skip
    text, count = names_overlay.build_ass(
        timeline, {"A": "ana", "B": "beto"}, "host", width=1000, height=2000
    )
    assert count == 6
    assert "PlayResX: 1000" in text and "PlayResY: 2000" in text
    assert "0:00:10.00,0:00:20.00,Name,,0,0,0,,{\\pos(514,514)}@ana" in text
    assert "0:00:20.00,0:00:30.00,Name,,0,0,0,,{\\pos(514,1014)}@ana" in text
    assert "0:00:20.00,0:00:30.00,Name,,0,0,0,,{\\pos(514,514)}@beto" in text
    assert "{\\pos(14,514)}@host" in text


def test_no_label_without_a_name_or_while_solo():
    timeline = [
        {"t": 0, "grids": [{"cid": HOST_CID, "host": True, "box": [0, 0, 1, 1]}]},
        {"t": 5, "grids": [{"cid": HOST_CID, "host": True, "box": [0, 0, 0.5, 1]},
                           {"cid": "X", "host": False, "box": [0.5, 0, 0.5, 1]}]},
    ]  # fmt: skip
    text, count = names_overlay.build_ass(timeline, {}, "host{bad}")
    assert count == 1  # only the host; X has no known name
    assert "@hostbad" in text and "@X" not in text


def test_scans_an_old_raw_once_and_caches_it(tmp_path):
    raw = tmp_path / "TK_host.ts"
    frames = []
    for second in range(30):
        pts = 90_000 * (second + 1)
        if second < 10:
            frames.append(_ts_frame(pts, _app_data(("A", 0.5, 0.25), ("B", 0.5, 0.5))))
        elif second < 20:
            frames.append(_ts_frame(pts, _app_data(("A", 0.5, 0.5), ("B", 0.5, 0.25))))
    raw.write_bytes(b"".join(frames))  # then 10 s with no layout: solo
    _participants(raw, {"A": "ana", "B": "beto"})
    row = {"file_path": str(raw), "status": "completed", "username": "host"}

    text, count = names_overlay.overlay_for(row)

    lines = names_overlay.read_timeline(raw.with_suffix(".layout.jsonl"))
    assert [line["t"] for line in lines] == [0, 10, 20]
    assert lines[-1]["grids"] == []
    assert count == 6 and "@ana" in text and "@host" in text
    raw.write_bytes(b"")  # a second request reads the cache, not the raw
    assert names_overlay.overlay_for(row)[1] == 6


def test_nothing_for_recordings_still_running_or_already_mp4(tmp_path):
    mp4 = tmp_path / "x.mp4"
    mp4.write_bytes(b"")
    assert names_overlay.overlay_for(
        {"file_path": str(mp4), "status": "completed", "username": "h"}
    ) == (names_overlay.build_ass([], {}, "h")[0], 0)
    assert not mp4.with_suffix(".layout.jsonl").exists()


def test_recorder_writes_every_layout_change(tmp_path):
    path = tmp_path / "rec.layout.jsonl"
    layout = LayoutTracker(timeline_path=str(path))
    layout.observe(1.0, _app_data(("A", 0.5, 0.25)))
    layout.observe(2.0, _app_data(("A", 0.5, 0.25)))  # same: no line
    layout.observe(3.0, _app_data(("A", 0.5, 0.5)))  # moved: new line
    layout.tick(20.0)  # layout stopped: solo again
    layout.tick(30.0)
    lines = names_overlay.read_timeline(path)
    assert [line["t"] for line in lines] == [1.0, 3.0, 4.0]
    assert lines[1]["grids"][1]["box"] == [0.5, 0.5, 0.5, 0.25]
    assert [g["host"] for g in lines[0]["grids"]] == [True, False]  # sorted by cid
