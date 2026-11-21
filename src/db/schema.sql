CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    mode INTEGER NOT NULL DEFAULT 1,
    automatic_interval INTEGER NOT NULL DEFAULT 5,
    proxy TEXT,
    bitrate TEXT,
    use_telegram INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    smart_interval INTEGER NOT NULL DEFAULT 0,
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
    error_message TEXT,
    -- Raw .ts kept for a while after conversion (supervisor/originals.py).
    original_path TEXT,
    original_kept_at TEXT
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

-- Small key/value store for hub-wide settings that must survive container
-- rebuilds (lives in hub.db under OUTPUT_DIR, unlike the mounted JSON files).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Creator profile data captured from TikTok's user/room lookup. The avatar
-- is downloaded under OUTPUT_DIR/avatars (TikTok's CDN URLs expire).
CREATE TABLE IF NOT EXISTS profiles (
    username TEXT PRIMARY KEY,
    nickname TEXT,
    avatar_path TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Who was linked into the live with the creator (co-hosts, multi-guest
-- guests) and when, in seconds since the recording started. Best-effort:
-- captured from room info while recording; end_seconds NULL = still there
-- when the data was last saved.
CREATE TABLE IF NOT EXISTS recording_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds REAL
);

CREATE INDEX IF NOT EXISTS idx_recording_participants_recording
    ON recording_participants(recording_id);

-- Every live ever recorded, kept when its recording is deleted: learned
-- schedules (supervisor/schedules.py) read this, not `recordings`, so
-- deleting recordings you don't need doesn't make the hub forget that the
-- creator was live (user report 2026-11-21: "aprendiendo horarios" went
-- back after a cleanup). Mirrored from `recordings` by the triggers below
-- (no DELETE trigger, on purpose); backfilled at startup.
CREATE TABLE IF NOT EXISTS live_history (
    recording_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds INTEGER,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_live_history_username ON live_history(username);

CREATE TRIGGER IF NOT EXISTS live_history_insert AFTER INSERT ON recordings
BEGIN
    INSERT OR REPLACE INTO live_history
        (recording_id, username, started_at, ended_at, duration_seconds, status)
    VALUES (NEW.id, NEW.username, NEW.started_at, NEW.ended_at,
            NEW.duration_seconds, NEW.status);
END;

CREATE TRIGGER IF NOT EXISTS live_history_update
AFTER UPDATE OF username, started_at, ended_at, duration_seconds, status ON recordings
BEGIN
    INSERT OR REPLACE INTO live_history
        (recording_id, username, started_at, ended_at, duration_seconds, status)
    VALUES (NEW.id, NEW.username, NEW.started_at, NEW.ended_at,
            NEW.duration_seconds, NEW.status);
END;
