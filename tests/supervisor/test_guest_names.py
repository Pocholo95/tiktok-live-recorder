import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import participants_repo, recordings_repo, settings_repo  # noqa: E402
from db.database import get_connection, init_db  # noqa: E402
from supervisor import guest_names  # noqa: E402


def _row(start, end, username):
    return {"start_seconds": start, "end_seconds": end, "username": username}


# Shaped like recording 43: two panels appear in the same poll and the old
# names were swapped; one panel leaves and comes back with the same cid.
LINES = [
    {"elapsed_seconds": 31.4, "panels": {"A": "flt65"}},
    {"elapsed_seconds": 141.6, "panels": {}},
    {"elapsed_seconds": 202.0, "panels": {"A": "julyyrg22"}},
    {"elapsed_seconds": 231.8, "panels": {"A": "julyyrg22", "B": None, "C": "flt65"}},
    {"elapsed_seconds": 262.3, "panels": {"A": "julyyrg22", "B": "flt65"}},
    {"elapsed_seconds": 1321.7, "panels": {}},
    {"elapsed_seconds": 2986.2, "panels": {"D": None, "E": "julyyrg22"}},
    {
        "elapsed_seconds": 3017.6,
        "panels": {"E": {"name": "x", "by": "poll", "box": [0, 0, 1, 1]}},
    },
    {"elapsed_seconds": 3078.3, "panels": {}},
]
ROWS = [
    _row(0.0, 110.9, "flt65"),
    _row(196.1, 1308.5, "julyyrg22"),
    _row(207.8, 1308.5, "flt65"),  # B: stays ~18 min
    _row(221.8, 235.8, "flt65"),  # C: leaves after a few seconds
    _row(2966.7, 3010.9, ""),  # D: unnamed, really julyyrg22
    _row(2983.7, 3049.1, "julyyrg22"),  # E: really cesar5560
    _row(5000.0, 5100.0, "solo"),  # no line for it: left alone
]
OWNERS = {"A": "ana", "B": "beto", "C": "carla", "D": "julyyrg22", "E": "cesar5560"}


def _write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"elapsed_seconds": 0, "ids": [], "link_mic": {}}) + "\n")
        for line in lines:
            f.write(json.dumps(line) + "\n")
        f.write("not json\n")


def test_rows_match_the_cid_that_appeared_and_left_with_them(tmp_path):
    _write_lines(tmp_path / "r.participants.jsonl", LINES)
    lines = guest_names.panel_lines(tmp_path / "r.participants.jsonl")

    matches = guest_names.match_rows(ROWS, lines)

    assert matches == {0: "A", 1: "A", 2: "B", 3: "C", 4: "D", 5: "E"}


def _db(tmp_path):
    db_path = tmp_path / "hub.db"
    init_db(db_path)
    return db_path, get_connection(db_path)


def _recording(conn, tmp_path, name, rows, lines):
    video = tmp_path / f"{name}.mp4"
    rid = recordings_repo.start(
        conn, channel_id=None, username="h", file_path=str(video)
    )
    recordings_repo.mark_completed(conn, rid, file_path=str(video), format="mp4")
    participants_repo.replace_for_recording(conn, rid, rows)
    if lines is not None:
        _write_lines(video.with_suffix(".participants.jsonl"), lines)
    return rid


def test_fix_existing_renames_once_and_keeps_times(tmp_path):
    _, conn = _db(tmp_path)
    rid = _recording(conn, tmp_path, "TK_a", ROWS, LINES)
    old = _recording(conn, tmp_path, "TK_old", [_row(5, 50, "x")], None)  # no cids
    asked = []

    def lookup(cid):
        asked.append(cid)
        return OWNERS[cid]

    summary = guest_names.fix_existing(conn, lookup)

    rows = participants_repo.list_for_recording(conn, rid)
    assert [r["username"] for r in rows] == [
        "ana", "ana", "beto", "carla", "julyyrg22", "cesar5560", "solo",
    ]  # fmt: skip
    assert [r["start_seconds"] for r in rows] == [r["start_seconds"] for r in ROWS]
    assert summary == {"recordings": 1, "renamed": 6}
    assert sorted(asked) == ["A", "B", "C", "D", "E"]  # one lookup per cid
    assert participants_repo.list_for_recording(conn, old)[0]["username"] == "x"

    asked.clear()
    guest_names.fix_existing(conn, lookup)
    assert asked == []  # done recordings aren't looked at again
    assert rid in json.loads(settings_repo.get(conn, guest_names.DONE_KEY))


def test_refused_lookups_keep_the_old_name_and_retry_if_nothing_answered(tmp_path):
    _, conn = _db(tmp_path)
    rid = _recording(conn, tmp_path, "TK_a", ROWS, LINES)

    def down(cid):
        raise ConnectionError("offline")

    guest_names.fix_existing(conn, down)
    assert participants_repo.list_for_recording(conn, rid)[0]["username"] == "flt65"
    assert rid not in json.loads(settings_repo.get(conn, guest_names.DONE_KEY))

    # Next boot TikTok answers some (age-restricted rooms still refuse).
    guest_names.fix_existing(conn, lambda cid: "dani" if cid == "D" else None)
    names = [r["username"] for r in participants_repo.list_for_recording(conn, rid)]
    assert names[4] == "dani" and names[0] == "flt65"
    assert rid in json.loads(settings_repo.get(conn, guest_names.DONE_KEY))
