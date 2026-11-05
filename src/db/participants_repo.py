def replace_for_recording(conn, recording_id, participants):
    """Swap in the tracker's latest full snapshot for one recording."""
    conn.execute(
        "DELETE FROM recording_participants WHERE recording_id = ?", (recording_id,)
    )
    conn.executemany(
        """
        INSERT INTO recording_participants
            (recording_id, username, start_seconds, end_seconds)
        VALUES (?, ?, ?, ?)
        """,
        [
            (recording_id, p["username"], p["start_seconds"], p.get("end_seconds"))
            for p in participants
        ],
    )
    conn.commit()


def list_for_recording(conn, recording_id):
    return conn.execute(
        """
        SELECT * FROM recording_participants
        WHERE recording_id = ? ORDER BY start_seconds, id
        """,
        (recording_id,),
    ).fetchall()


def usernames_for_recordings(conn, recording_ids):
    """recording_id -> distinct usernames in order of appearance (list views)."""
    if not recording_ids:
        return {}
    placeholders = ",".join("?" * len(recording_ids))
    rows = conn.execute(
        f"""
        SELECT recording_id, username, MIN(start_seconds) AS first_seen
        FROM recording_participants
        WHERE recording_id IN ({placeholders})
        GROUP BY recording_id, username
        ORDER BY first_seen
        """,
        list(recording_ids),
    ).fetchall()
    result = {}
    for row in rows:
        result.setdefault(row["recording_id"], []).append(row["username"])
    return result
