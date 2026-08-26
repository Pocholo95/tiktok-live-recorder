_UPDATABLE_FIELDS = {
    "username",
    "mode",
    "automatic_interval",
    "proxy",
    "bitrate",
    "use_telegram",
    "enabled",
}


def insert(
    conn,
    *,
    username,
    mode,
    automatic_interval=5,
    proxy=None,
    bitrate=None,
    use_telegram=False,
    enabled=True,
):
    cursor = conn.execute(
        """
        INSERT INTO channels
            (username, mode, automatic_interval, proxy, bitrate, use_telegram, enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            username,
            int(mode),
            automatic_interval,
            proxy,
            bitrate,
            int(use_telegram),
            int(enabled),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def list_all(conn):
    return conn.execute("SELECT * FROM channels ORDER BY created_at").fetchall()


def list_enabled(conn):
    return conn.execute(
        "SELECT * FROM channels WHERE enabled = 1 ORDER BY created_at"
    ).fetchall()


def get(conn, channel_id):
    return conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()


def update(conn, channel_id, **fields):
    if not fields:
        return

    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"Cannot update unknown channel fields: {sorted(unknown)}")

    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE channels SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        (*fields.values(), channel_id),
    )
    conn.commit()


def set_enabled(conn, channel_id, enabled):
    update(conn, channel_id, enabled=int(enabled))


def delete(conn, channel_id):
    conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()
