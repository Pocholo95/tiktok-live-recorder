"""
Facts about the TikTok session, to find out why it "expires" almost daily
(user report 2026-11-21; it's a secondary account used only by the hub).

Two suspects: (1) false alarms - room info's 4003110 also means a region
restriction, not only a dead session; (2) TikTok rotating session cookies
in its responses while the hub only ever keeps the `sessionid_ss` it was
given (each worker has its own HTTP session, and nothing is persisted).

So the hub appends to `<OUTPUT_DIR>/session-events.jsonl`:
- {"event": "set_cookie", "host", "path", "names"} - cookie NAMES TikTok set
  (never values), once per host+path+names per hour;
- {"event": "refused", "user", "room_id", "where", "has_session",
  "tiktok_message"} - every 4003110 (with or without a session), with
  whatever TikTok says (so far only an empty "prompts").
- {"event": "session_saved"} when a new session is saved in Settings, and
  {"event": "cookies_reloaded", "user"} when each running channel picks it
  up - so the time from saving to the first refusal is the session's real
  lifetime.
Only written when OUTPUT_DIR is set (the hub); the CLI writes nothing.
"""

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

FILE_NAME = "session-events.jsonl"
MAX_BYTES = 2 * 1024 * 1024  # rotate to .1 beyond this
_REPEAT_SECONDS = 3600

_lock = threading.Lock()
_last_seen = {}


def log_path():
    output_dir = os.environ.get("OUTPUT_DIR")
    return Path(output_dir) / FILE_NAME if output_dir else None


def _append(record):
    path = log_path()
    if path is None:
        return
    record = {"time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), **record}
    try:
        with _lock:
            if path.exists() and path.stat().st_size > MAX_BYTES:
                os.replace(path, path.with_suffix(".jsonl.1"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # diagnostics must never break a request


def cookie_names(response):
    """Names of the cookies a response sets (curl_cffi or requests)."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return []
    values = []
    for getter in ("get_list", "get_all"):
        get = getattr(headers, getter, None)
        if get is not None:
            try:
                values = list(get("set-cookie") or [])
                break
            except Exception:
                values = []
    if not values:
        single = headers.get("set-cookie") if hasattr(headers, "get") else None
        values = [single] if single else []
    names = []
    for value in values:
        name = str(value).split("=", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return sorted(names)


def note_response(url, response):
    """Log which cookies TikTok set on this response (names only)."""
    try:
        names = cookie_names(response)
        if not names:
            return
        parsed = urlparse(str(url))
        key = (parsed.netloc, parsed.path, tuple(names))
        now = time.monotonic()
        with _lock:
            if now - _last_seen.get(key, -_REPEAT_SECONDS) < _REPEAT_SECONDS:
                return
            _last_seen[key] = now
        _append(
            {
                "event": "set_cookie",
                "host": parsed.netloc,
                "path": parsed.path,
                "names": names,
            }
        )
    except Exception:
        pass


def note_refusal(user, room_id, where, has_session, tiktok_data):
    """A 4003110 while a session is configured, with TikTok's message."""
    data = tiktok_data if isinstance(tiktok_data, dict) else {}
    message = {
        k: str(data[k])[:300]
        for k in ("message", "prompts", "tips", "description")
        if data.get(k)
    }
    _append(
        {
            "event": "refused",
            "user": user,
            "room_id": str(room_id),
            "where": where,
            "has_session": bool(has_session),
            "tiktok_message": message,
        }
    )


def note_event(event, **fields):
    """Other session facts: "session_saved" (Settings), "cookies_reloaded"
    (a running channel picked the new cookies up)."""
    _append({"event": event, **fields})
