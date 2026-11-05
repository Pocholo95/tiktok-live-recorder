"""
Saves creator profile data (avatar picture, display name) that the
recorder picks up for free from TikTok's user/room lookup.

TikTok's avatar URLs are signed and expire, so the picture is downloaded
to OUTPUT_DIR/avatars/<username>.jpg instead of storing the URL. Runs on a
daemon thread and never raises - a missing avatar only means the UI keeps
showing the initial-letter placeholder.
"""

import os
import re
import threading
from pathlib import Path

import requests

from db import profiles_repo
from utils.logger_manager import logger

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_MAX_AVATAR_BYTES = 2 * 1024 * 1024


def avatar_path_for(output_dir, username):
    return Path(output_dir) / "avatars" / f"{_SAFE_NAME.sub('_', username)}.jpg"


def _download(url, destination):
    response = requests.get(
        url,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.tiktok.com/"},
    )
    response.raise_for_status()
    if not response.headers.get("Content-Type", "").startswith("image/"):
        raise ValueError(f"not an image ({response.headers.get('Content-Type')})")
    if len(response.content) > _MAX_AVATAR_BYTES:
        raise ValueError("avatar too large")

    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(".part")
    part.write_bytes(response.content)
    os.replace(part, destination)


def save_profile(db_path, get_connection, output_dir, username, avatar_url, nickname):
    avatar_path = None
    if avatar_url:
        destination = avatar_path_for(output_dir, username)
        try:
            _download(avatar_url, destination)
            avatar_path = str(destination)
        except Exception as e:
            logger.warning(f"Could not download avatar for @{username}: {e}")

    conn = get_connection(db_path)
    try:
        profiles_repo.upsert(
            conn, username, nickname=nickname or None, avatar_path=avatar_path
        )
    finally:
        conn.close()


def save_profile_in_background(*args):
    threading.Thread(
        target=save_profile, args=args, name="profile-save", daemon=True
    ).start()
