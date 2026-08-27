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


def list_filtered(conn, *, channel_id=None, date_from=None, date_to=None):
    query = "SELECT * FROM recordings WHERE 1=1"
    params = []

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
