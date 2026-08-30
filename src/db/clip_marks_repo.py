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
