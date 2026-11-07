"""
Problem alerts to Discord: things that silently go wrong while the hub runs
unattended (TikTok session no longer accepted, a channel stuck in error, a
recording whose file stopped growing).

Separate from the per-channel live notices (the bell): these go to the same
webhook whenever "Avisos de problemas" is on in Settings (default on).
Each alert kind has its own throttle, stored in the `settings` table so it
survives restarts and never floods the channel.
"""

import time

from db import settings_repo
from notify import discord_webhook

ENABLED_KEY = "problem_alerts_enabled"
SESSION_PROBLEM_KEY = "session_problem"  # JSON-free: "<epoch>|<username>"


def enabled(conn):
    return settings_repo.get(conn, ENABLED_KEY, "1") != "0"


def send(conn, kind, title, description, throttle_seconds, now=None):
    """
    Sends one problem alert unless alerts are off, no webhook is set, or the
    same `kind` was sent less than `throttle_seconds` ago. Returns True if
    it was sent. `kind` should identify the problem instance (e.g. include
    the channel or recording id) so different problems don't mute each other.
    """
    now = now if now is not None else time.time()
    webhook = settings_repo.get(conn, discord_webhook.SETTING_KEY)
    if not webhook or not enabled(conn):
        return False

    last_key = f"alert_last:{kind}"
    last = settings_repo.get(conn, last_key)
    try:
        last = float(last) if last is not None else None
    except ValueError:
        last = None
    if last is not None and now - last < throttle_seconds:
        return False

    settings_repo.set(conn, last_key, str(now))
    discord_webhook.send_in_background(
        webhook, discord_webhook.problem_embed(title, description)
    )
    return True


def record_session_problem(conn, username, now=None):
    """Remembered for the Settings page ("TikTok rejected the session ...")."""
    now = now if now is not None else time.time()
    settings_repo.set(conn, SESSION_PROBLEM_KEY, f"{now}|{username}")


def last_session_problem(conn):
    """(epoch seconds, username) of the last rejection, or None."""
    value = settings_repo.get(conn, SESSION_PROBLEM_KEY)
    if not value or "|" not in value:
        return None
    when, username = value.split("|", 1)
    try:
        return float(when), username
    except ValueError:
        return None


def clear_session_problem(conn):
    settings_repo.set(conn, SESSION_PROBLEM_KEY, None)
