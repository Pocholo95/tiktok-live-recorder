CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    mode INTEGER NOT NULL DEFAULT 1,
    automatic_interval INTEGER NOT NULL DEFAULT 5,
    proxy TEXT,
    bitrate TEXT,
    use_telegram INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    username TEXT NOT NULL,
    file_path TEXT NOT NULL,
    format TEXT NOT NULL DEFAULT 'flv',
    status TEXT NOT NULL DEFAULT 'recording',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    duration_seconds INTEGER,
    file_size_bytes INTEGER,
    thumbnail_path TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_recordings_channel ON recordings(channel_id);
CREATE INDEX IF NOT EXISTS idx_recordings_started_at ON recordings(started_at);

-- Only the in/out timestamps are stored - the clip itself is generated on
-- demand and streamed straight to the browser, never written to disk.
CREATE TABLE IF NOT EXISTS clip_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    label TEXT,
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_clip_marks_recording ON clip_marks(recording_id);
