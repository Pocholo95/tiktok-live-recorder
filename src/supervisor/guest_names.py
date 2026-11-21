"""
One-off fix of guest NAMES on recordings made before panels were named by
their cid (2026-11-21, see core/layout.py).

Those recordings got their guest times from the video's panels but their
names from the recorded room's own info, which names one linked creator at
a time and rotates - so rows ended up "Sin identificar" or with the wrong
person (recording 43: miranda_fernandezzz's panel was called "flt65").
Their `.participants.jsonl` logged each panel's cid on every poll where the
set changed, and a cid is the room id of that guest's own live: TikTok
still answers who hosted it after the live ended.

Each saved row is matched to the cid that newly appeared in the first
"panels" line at/after the row's start; when several appeared together, to
the one that left when the row ended (ties are left alone). Runs
at startup in the background; recordings done are listed in Settings key
DONE_KEY. Recordings from before the panels existed (no cids saved) can't
be fixed.
"""

import json
from pathlib import Path

from db import participants_repo, recordings_repo, settings_repo
from utils.logger_manager import logger

DONE_KEY = "guest_names_by_cid_done"


def panel_lines(debug_path):
    """[(video seconds, {cid: old name or None})] from a .participants.jsonl."""
    lines = []
    try:
        with open(debug_path, encoding="utf-8") as f:
            for raw in f:
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                panels = record.get("panels")
                if not isinstance(panels, dict):
                    continue
                names = {}
                for cid, value in panels.items():
                    if isinstance(value, dict):  # newer format: {name, by, box}
                        value = value.get("name")
                    names[str(cid)] = value or None
                lines.append((float(record.get("elapsed_seconds") or 0), names))
    except OSError:
        return []
    return lines


def _gone_at(cid, lines, since):
    """Video time of the first logged line after `since` without `cid`
    (None if it never left). Lines are only written when the set changes,
    so this is when the next change showed it gone."""
    for at, panels in lines:
        if at > since and cid not in panels:
            return at
    return None


def match_rows(rows, lines):
    """
    {row index: cid} for the rows that can be told apart. A row belongs to
    a cid that newly appeared in the first logged line at/after its start;
    when several appeared together, to the one that left when the row
    ended (the old names can't decide: they are what's wrong).
    """
    matches = {}
    for index, row in enumerate(rows):
        start = row["start_seconds"] or 0
        end = row["end_seconds"]
        previous = {}
        for at, panels in lines:
            if at < start - 0.5:
                previous = panels
                continue
            if end is not None and at > end + 0.5:
                break
            new = [cid for cid in panels if cid not in previous]
            if len(new) == 1:
                matches[index] = new[0]
            elif new:

                def distance(cid, at=at):
                    gone = _gone_at(cid, lines, at)
                    if end is None:
                        return 0 if gone is None else 1
                    return abs((gone if gone is not None else float("inf")) - end)

                ranked = sorted(new, key=distance)
                if distance(ranked[0]) < distance(ranked[1]):
                    matches[index] = ranked[0]
            break
    return matches


def rename(conn, recording, lookup):
    """Renames one recording's guests. Returns (renamed rows, lookups
    attempted, lookups answered)."""
    debug_path = Path(recording["file_path"]).with_suffix(".participants.jsonl")
    lines = panel_lines(debug_path)
    rows = [
        dict(r) for r in participants_repo.list_for_recording(conn, recording["id"])
    ]
    if not lines or not rows:
        return 0, 0, 0
    matches = match_rows(rows, lines)
    owners = {}
    for cid in dict.fromkeys(matches.values()):
        try:
            owners[cid] = lookup(cid)
        except Exception:
            owners[cid] = None
    renamed = 0
    for index, cid in matches.items():
        owner = owners.get(cid)
        if owner and rows[index]["username"] != owner:
            rows[index]["username"] = owner
            renamed += 1
    if renamed:
        participants_repo.replace_for_recording(conn, recording["id"], rows)
    answered = sum(1 for owner in owners.values() if owner)
    return renamed, len(owners), answered


def _done(conn):
    try:
        return set(json.loads(settings_repo.get(conn, DONE_KEY, "[]")))
    except (TypeError, ValueError):
        return set()


def fix_existing(conn, lookup):
    done = _done(conn)
    summary = {"recordings": 0, "renamed": 0}
    for recording in recordings_repo.list_filtered(conn):
        if recording["id"] in done or recording["status"] == "recording":
            continue
        renamed, asked, answered = rename(conn, recording, lookup)
        # Retry on a later boot only if TikTok answered nothing at all
        # (network down?); a room that refuses (age-restricted) won't change.
        if asked and not answered:
            continue
        done.add(recording["id"])
        if renamed:
            summary["recordings"] += 1
            summary["renamed"] += renamed
    settings_repo.set(conn, DONE_KEY, json.dumps(sorted(done)))
    return summary


def run_at_startup(db_path, get_connection, make_api):
    """Background entry point. `make_api()` builds a TikTokAPI (with the
    hub's cookies, so age-restricted rooms can answer too)."""
    conn = get_connection(db_path)
    try:
        api = None

        def lookup(cid):
            nonlocal api
            if api is None:
                api = make_api()
            return api.get_room_owner(cid)

        summary = fix_existing(conn, lookup)
        if summary["renamed"]:
            logger.info(
                f"Guest names: renamed {summary['renamed']} row(s) in "
                f"{summary['recordings']} recording(s) from their panel room ids"
            )
    except Exception as e:
        logger.warning(f"Renaming old guests failed: {e}")
    finally:
        conn.close()
