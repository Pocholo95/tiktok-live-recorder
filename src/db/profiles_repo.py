def get(conn, username):
    return conn.execute(
        "SELECT * FROM profiles WHERE username = ?", (username,)
    ).fetchone()


def all_by_username(conn):
    return {row["username"]: row for row in conn.execute("SELECT * FROM profiles")}


def upsert(conn, username, *, nickname=None, avatar_path=None):
    conn.execute(
        """
        INSERT INTO profiles (username, nickname, avatar_path, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(username) DO UPDATE SET
            nickname = COALESCE(excluded.nickname, profiles.nickname),
            avatar_path = COALESCE(excluded.avatar_path, profiles.avatar_path),
            updated_at = datetime('now')
        """,
        (username, nickname, avatar_path),
    )
    conn.commit()
