def insert(conn, *, recording_id, start_seconds, end_seconds, label=None):
    cursor = conn.execute(
        """
        INSERT INTO clip_marks (recording_id, label, start_seconds, end_seconds)
        VALUES (?, ?, ?, ?)
        """,
        (recording_id, label, start_seconds, end_seconds),
    )
    conn.commit()
    return cursor.lastrowid


def list_for_recording(conn, recording_id):
    return conn.execute(
        "SELECT * FROM clip_marks WHERE recording_id = ? ORDER BY start_seconds",
        (recording_id,),
    ).fetchall()


def get(conn, clip_mark_id):
    return conn.execute(
        "SELECT * FROM clip_marks WHERE id = ?", (clip_mark_id,)
    ).fetchone()


def delete(conn, clip_mark_id):
    conn.execute("DELETE FROM clip_marks WHERE id = ?", (clip_mark_id,))
    conn.commit()


def count_for_recordings(conn, recording_ids):
    """Returns {recording_id: count} for the given ids (missing ids are
    simply absent from the result, i.e. zero marks)."""
    recording_ids = list(recording_ids)
    if not recording_ids:
        return {}

    placeholders = ",".join("?" * len(recording_ids))
    rows = conn.execute(
        f"""
        SELECT recording_id, COUNT(*) AS count
        FROM clip_marks
        WHERE recording_id IN ({placeholders})
        GROUP BY recording_id
        """,
        recording_ids,
    ).fetchall()
    return {row["recording_id"]: row["count"] for row in rows}
