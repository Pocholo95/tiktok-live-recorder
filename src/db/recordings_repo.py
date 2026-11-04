def start(conn, *, channel_id, username, file_path, format="flv"):
    cursor = conn.execute(
        """
        INSERT INTO recordings (channel_id, username, file_path, format, status)
        VALUES (?, ?, ?, ?, 'recording')
        """,
        (channel_id, username, file_path, format),
    )
    conn.commit()
    return cursor.lastrowid


def mark_completed(
    conn,
    recording_id,
    *,
    file_path,
    format,
    duration_seconds=None,
    file_size_bytes=None,
    thumbnail_path=None,
):
    conn.execute(
        """
        UPDATE recordings
        SET status = 'completed',
            file_path = ?,
            format = ?,
            ended_at = datetime('now'),
            duration_seconds = ?,
            file_size_bytes = ?,
            thumbnail_path = ?
        WHERE id = ?
        """,
        (
            file_path,
            format,
            duration_seconds,
            file_size_bytes,
            thumbnail_path,
            recording_id,
        ),
    )
    conn.commit()


def mark_failed(conn, recording_id, *, error_message):
    conn.execute(
        """
        UPDATE recordings
        SET status = 'failed', ended_at = datetime('now'), error_message = ?
        WHERE id = ?
        """,
        (error_message, recording_id),
    )
    conn.commit()


# A recording is "ready" once the offline-converted MP4 has been uploaded
# back; anything else that finished is still the raw capture (.ts, or .flv
# from before the recorder wrote TS) waiting to be converted.
_STATE_FILTERS = {
    "raw": "status = 'completed' AND format != 'mp4'",
    "ready": "status = 'completed' AND format = 'mp4'",
    "recording": "status = 'recording'",
    "failed": "status = 'failed'",
}


def list_filtered(conn, *, channel_id=None, date_from=None, date_to=None, state=None):
    query = "SELECT * FROM recordings WHERE 1=1"
    params = []

    if state in _STATE_FILTERS:
        query += f" AND {_STATE_FILTERS[state]}"

    if channel_id is not None:
        query += " AND channel_id = ?"
        params.append(channel_id)
    if date_from is not None:
        query += " AND started_at >= ?"
        params.append(date_from)
    if date_to is not None:
        query += " AND started_at <= ?"
        params.append(date_to)

    query += " ORDER BY started_at DESC"
    return conn.execute(query, params).fetchall()


def get(conn, recording_id):
    return conn.execute(
        "SELECT * FROM recordings WHERE id = ?", (recording_id,)
    ).fetchone()


def delete(conn, recording_id):
    conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
    conn.commit()


def state_counts(conn):
    """How many recordings are in each library state (see _STATE_FILTERS)."""
    counts = {}
    for state, condition in _STATE_FILTERS.items():
        counts[state] = conn.execute(
            f"SELECT COUNT(*) FROM recordings WHERE {condition}"
        ).fetchone()[0]
    counts["all"] = conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
    counts["raw_bytes"] = conn.execute(
        "SELECT COALESCE(SUM(file_size_bytes), 0) FROM recordings "
        f"WHERE {_STATE_FILTERS['raw']}"
    ).fetchone()[0]
    return counts


def replace_with_converted(
    conn, recording_id, *, file_path, file_size_bytes, duration_seconds, thumbnail_path
):
    """Point a raw recording at its uploaded MP4 (the raw file is deleted by the caller)."""
    conn.execute(
        """
        UPDATE recordings
        SET file_path = ?,
            format = 'mp4',
            file_size_bytes = ?,
            duration_seconds = COALESCE(?, duration_seconds),
            thumbnail_path = COALESCE(?, thumbnail_path)
        WHERE id = ?
        """,
        (file_path, file_size_bytes, duration_seconds, thumbnail_path, recording_id),
    )
    conn.commit()


_SUMMARY_COLUMNS = f"""
    COUNT(*) AS total,
    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
    SUM(CASE WHEN {_STATE_FILTERS["raw"]} THEN 1 ELSE 0 END) AS raw_count,
    SUM(CASE WHEN status = 'recording' THEN 1 ELSE 0 END) AS live_count,
    COALESCE(SUM(duration_seconds), 0) AS total_seconds,
    COALESCE(SUM(file_size_bytes), 0) AS total_bytes,
    COALESCE(SUM(CASE WHEN {_STATE_FILTERS["raw"]} THEN file_size_bytes END), 0)
        AS raw_bytes,
    MAX(started_at) AS last_started_at
"""


def totals(conn):
    """Library-wide numbers for the stats header."""
    return conn.execute(f"SELECT {_SUMMARY_COLUMNS} FROM recordings").fetchone()


def creator_summaries(conn, username=None):
    """Per-username stats (all creators, or just `username`), newest first."""
    query = f"SELECT username, {_SUMMARY_COLUMNS} FROM recordings"
    params = []
    if username is not None:
        query += " WHERE username = ?"
        params.append(username)
    query += " GROUP BY username ORDER BY last_started_at DESC"
    return conn.execute(query, params).fetchall()


def list_for_username(conn, username):
    return conn.execute(
        "SELECT * FROM recordings WHERE username = ? ORDER BY started_at DESC",
        (username,),
    ).fetchall()
