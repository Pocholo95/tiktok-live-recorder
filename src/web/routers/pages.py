import shutil
from datetime import datetime, timezone
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from db import (
    channels_repo,
    clip_marks_repo,
    participants_repo,
    profiles_repo,
    recordings_repo,
    settings_repo,
)
from db.database import get_connection
from utils.enums import Mode
from web.deps import get_db, get_supervisor, get_templates

router = APIRouter()

_PHASE_LABELS = {
    "starting": "Iniciando",
    "checking": "Verificando",
    "offline": "Offline",
    "recording": "Grabando",
    "idle": "En espera",
    "error": "Error",
    "stopped": "Pausado",
}


def _live_info(db, channel_id, status):
    """
    What's happening in a channel's ongoing recording, for the dashboard:
    how long it has been running, how big the file is, who is linked in
    right now, and whether the health monitor saw it stall.
    """
    recording = db.execute(
        """
        SELECT * FROM recordings
        WHERE channel_id = ? AND status = 'recording'
        ORDER BY started_at DESC LIMIT 1
        """,
        (channel_id,),
    ).fetchone()
    if recording is None:
        return None

    elapsed = None
    try:
        started = datetime.strptime(recording["started_at"][:19], "%Y-%m-%d %H:%M:%S")
        elapsed = max(
            0,
            (datetime.now(timezone.utc).replace(tzinfo=None) - started).total_seconds(),
        )
    except (TypeError, ValueError):
        pass

    size = None
    try:
        size = Path(recording["file_path"]).stat().st_size
    except (OSError, TypeError):
        pass

    guests = [
        p["username"]
        for p in participants_repo.list_for_recording(db, recording["id"])
        if p["end_seconds"] is None
    ]
    flags = status.get("flags") or {}
    stalled_since = flags.get("stalled_since")
    waiting_until = flags.get("waiting_return_until")
    return {
        "recording_id": recording["id"],
        "elapsed_seconds": elapsed,
        "size_bytes": size,
        "guests": list(dict.fromkeys(guests)),
        "waiting_seconds": max(0, int(waiting_until - time.time()))
        if waiting_until
        else None,
        "stalled_minutes": int((time.time() - stalled_since) / 60)
        if stalled_since is not None
        else None,
    }


def _interval_now(db, row):
    """What the smart interval is doing right now, for the dashboard."""
    from core import schedule as schedule_core
    from supervisor import schedules

    if not row["smart_interval"]:
        return None
    minutes, reason = schedules.effective_interval(db, row)
    learned = schedules.get(db, row["username"])
    return {
        "minutes": minutes,
        "reason": reason,
        "lives": learned["lives"],
        "days": learned["days"],
        "min_lives": schedule_core.MIN_LIVES,
        "min_days": schedule_core.MIN_DAYS,
    }


def _channel_view(row, supervisor, db=None):
    status = supervisor.get_status(row["id"]) or {}
    default_phase = "stopped" if not row["enabled"] else "starting"
    phase = status.get("phase", default_phase)
    return {
        "id": row["id"],
        "username": row["username"],
        "mode": Mode(row["mode"]).name.title(),
        "automatic_interval": row["automatic_interval"],
        "proxy": row["proxy"],
        "enabled": bool(row["enabled"]),
        "notify": bool(row["use_telegram"]),
        "smart": bool(row["smart_interval"]),
        "interval_now": _interval_now(db, row) if db is not None else None,
        "phase": phase,
        "phase_label": _PHASE_LABELS.get(phase, phase),
        "detail": status.get("detail"),
        "live": _live_info(db, row["id"], status)
        if db is not None and phase == "recording"
        else None,
    }


def _render_channels(request, db, supervisor, templates, template_name):
    channels = [
        _channel_view(row, supervisor, db) for row in channels_repo.list_all(db)
    ]
    return templates.TemplateResponse(request, template_name, {"channels": channels})


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db=Depends(get_db),
    supervisor=Depends(get_supervisor),
    templates=Depends(get_templates),
):
    return _render_channels(request, db, supervisor, templates, "dashboard.html")


@router.get("/partials/channels", response_class=HTMLResponse)
def channels_partial(
    request: Request,
    db=Depends(get_db),
    supervisor=Depends(get_supervisor),
    templates=Depends(get_templates),
):
    return _render_channels(
        request, db, supervisor, templates, "partials/channel_rows.html"
    )


@router.post("/channels", response_class=HTMLResponse)
def create_channel(
    request: Request,
    username: str = Form(...),
    mode: str = Form("automatic"),
    automatic_interval: int = Form(5),
    proxy: str = Form(""),
    use_telegram: bool = Form(False),
    db=Depends(get_db),
    supervisor=Depends(get_supervisor),
    templates=Depends(get_templates),
):
    username = username.strip().lstrip("@")
    mode_enum = Mode.FOLLOWERS if mode == "followers" else Mode.AUTOMATIC

    if username:
        channels_repo.insert(
            db,
            username=username,
            mode=mode_enum,
            automatic_interval=max(1, automatic_interval),
            proxy=proxy.strip() or None,
            use_telegram=use_telegram,
        )

    return _render_channels(
        request, db, supervisor, templates, "partials/channel_rows.html"
    )


@router.post("/channels/{channel_id}/toggle", response_class=HTMLResponse)
def toggle_channel(
    request: Request,
    channel_id: int,
    db=Depends(get_db),
    supervisor=Depends(get_supervisor),
    templates=Depends(get_templates),
):
    row = channels_repo.get(db, channel_id)
    if row is not None:
        channels_repo.set_enabled(db, channel_id, not row["enabled"])
        row = channels_repo.get(db, channel_id)

    return templates.TemplateResponse(
        request,
        "partials/channel_row.html",
        {"channel": _channel_view(row, supervisor, db)},
    )


@router.post("/channels/{channel_id}/interval", response_class=HTMLResponse)
def update_channel_interval(
    request: Request,
    channel_id: int,
    minutes: str = Form(""),
    db=Depends(get_db),
    supervisor=Depends(get_supervisor),
    templates=Depends(get_templates),
):
    # Not a supervisor restart trigger: the worker re-reads it before each
    # wait, so this never cuts an ongoing recording.
    try:
        value = int(minutes)
    except ValueError:
        value = None
    if value is not None and 1 <= value <= 1440:
        channels_repo.update(db, channel_id, automatic_interval=value)

    row = channels_repo.get(db, channel_id)
    if row is None:
        return PlainTextResponse("")
    return templates.TemplateResponse(
        request,
        "partials/channel_row.html",
        {"channel": _channel_view(row, supervisor, db)},
    )


@router.post("/channels/{channel_id}/smart", response_class=HTMLResponse)
def toggle_channel_smart(
    request: Request,
    channel_id: int,
    db=Depends(get_db),
    supervisor=Depends(get_supervisor),
    templates=Depends(get_templates),
):
    # Not a restart trigger: the worker reads it before every wait.
    row = channels_repo.get(db, channel_id)
    if row is not None:
        channels_repo.update(
            db, channel_id, smart_interval=int(not row["smart_interval"])
        )
        row = channels_repo.get(db, channel_id)
    if row is None:
        return PlainTextResponse("")
    return templates.TemplateResponse(
        request,
        "partials/channel_row.html",
        {"channel": _channel_view(row, supervisor, db)},
    )


@router.delete("/channels/{channel_id}")
def delete_channel(channel_id: int, db=Depends(get_db)):
    channels_repo.delete(db, channel_id)
    return PlainTextResponse("")


def _disk_usage(request):
    """Free/total bytes of the recordings volume, or None if unreadable."""
    try:
        usage = shutil.disk_usage(request.app.state.output_dir)
    except OSError:
        return None
    return {"free": usage.free, "total": usage.total}


def _group_guests(participants):
    """One entry per guest, with all their time ranges, by first appearance."""
    groups = {}
    for p in participants:
        group = groups.setdefault(
            p["username"], {"username": p["username"], "ranges": [], "seconds": 0.0}
        )
        group["ranges"].append({"start": p["start_seconds"], "end": p["end_seconds"]})
        if p["end_seconds"] is not None:
            group["seconds"] += p["end_seconds"] - p["start_seconds"]
    return list(groups.values())


def _parse_int(value):
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


@router.get("/library", response_class=HTMLResponse)
def library(
    request: Request,
    recording_id: str | None = None,
    channel_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    no_clips: str | None = None,
    state: str | None = None,
    db=Depends(get_db),
    templates=Depends(get_templates),
):
    # Query params come from plain HTML forms/links, where an unset <select>
    # or a cleared field arrives as an empty string, not an absent param -
    # int-typed FastAPI params would 422 on "" instead of treating it as None.
    recording_id = _parse_int(recording_id)
    channel_id = _parse_int(channel_id)
    no_clips_only = no_clips not in (None, "", "0", "false")
    state = state if state in ("raw", "ready", "recording", "failed") else None

    channels = channels_repo.list_all(db)
    recordings = recordings_repo.list_filtered(
        db,
        channel_id=channel_id,
        date_from=date_from or None,
        date_to=f"{date_to} 23:59:59" if date_to else None,
        state=state,
    )

    counts = clip_marks_repo.count_for_recordings(db, [r["id"] for r in recordings])
    guests = participants_repo.usernames_for_recordings(
        db, [r["id"] for r in recordings]
    )
    if no_clips_only:
        recordings = [r for r in recordings if counts.get(r["id"], 0) == 0]

    groups = []
    current_date = None
    for row in recordings:
        date_str = (row["started_at"] or "")[:10]
        entry = dict(row)
        entry["clip_count"] = counts.get(row["id"], 0)
        entry["guests"] = guests.get(row["id"], [])
        if date_str != current_date:
            current_date = date_str
            groups.append({"date": date_str, "recordings": []})
        groups[-1]["recordings"].append(entry)

    selected_recording = (
        recordings_repo.get(db, recording_id) if recording_id is not None else None
    )
    if selected_recording is None and recordings:
        selected_recording = recordings[0]

    marks = (
        clip_marks_repo.list_for_recording(db, selected_recording["id"])
        if selected_recording is not None
        else []
    )

    # Picking a recording only swaps the player panel (#library-main), so the
    # sidebar's scroll position survives; changing a filter swaps the whole
    # two-column panel since the recording list itself changes. htmx sends
    # the target element's id in HX-Target, telling us which one to render.
    # Returning the full page (nav + <html>) for either would nest a
    # document inside a document.
    # Current filters as a query string, so every link (recording items,
    # state tabs) keeps the rest of the filters intact.
    filter_params = {
        "channel_id": channel_id,
        "date_from": date_from,
        "date_to": date_to,
        "no_clips": "1" if no_clips_only else None,
        "state": state,
    }
    filter_params = {k: v for k, v in filter_params.items() if v not in (None, "")}

    def query(**overrides):
        params = {**filter_params, **overrides}
        return urlencode({k: v for k, v in params.items() if v not in (None, "")})

    counts = recordings_repo.state_counts(db)
    state_tabs = [
        {
            "key": key,
            "label": label,
            "count": counts[count_key],
            "query": query(state=key),
        }
        for key, label, count_key in (
            (None, "Todas", "all"),
            ("raw", "Por convertir", "raw"),
            ("ready", "Listas", "ready"),
            ("recording", "Grabando", "recording"),
            ("failed", "Fallidas", "failed"),
        )
        if key in (None, "raw", "ready") or counts[count_key] or state == key
    ]

    participants = [
        dict(p)
        for p in (
            participants_repo.list_for_recording(db, selected_recording["id"])
            if selected_recording is not None
            else []
        )
    ]

    hx_target = request.headers.get("hx-target")
    if hx_target == "library-main":
        template_name = "partials/library_main.html"
    elif request.headers.get("hx-request") == "true":
        template_name = "partials/library_content.html"
    else:
        template_name = "library.html"
    return templates.TemplateResponse(
        request,
        template_name,
        {
            "channels": channels,
            "groups": groups,
            "selected_recording": selected_recording,
            "marks": marks,
            "participants": participants,
            "guest_groups": _group_guests(participants),
            "selected_channel_id": channel_id,
            "date_from": date_from,
            "date_to": date_to,
            "no_clips_only": no_clips_only,
            "state": state,
            "off_hours": _is_off_hours(db, selected_recording),
            "counts": counts,
            "totals": recordings_repo.totals(db),
            "disk": _disk_usage(request),
            "state_tabs": state_tabs,
            "item_query": query(),
        },
    )


@router.delete("/recordings/{recording_id}")
def delete_recording(recording_id: int, db=Depends(get_db)):
    recording = recordings_repo.get(db, recording_id)
    if recording is not None:
        if recording["file_path"]:
            Path(recording["file_path"]).unlink(missing_ok=True)
        if recording["thumbnail_path"]:
            Path(recording["thumbnail_path"]).unlink(missing_ok=True)
        if recording["file_path"]:
            Path(recording["file_path"]).with_suffix(".participants.jsonl").unlink(
                missing_ok=True
            )
        recordings_repo.delete(db, recording_id)

    return PlainTextResponse("")


@router.get("/recordings/{recording_id}/clip")
def clip_editor_redirect(recording_id: int):
    # The clip editor is now embedded in /library itself (selecting a
    # recording from the sidebar shows it) - kept as a redirect so old
    # bookmarks/links still land somewhere useful.
    return RedirectResponse(url=f"/library?recording_id={recording_id}")


@router.post("/recordings/{recording_id}/clip-marks", response_class=HTMLResponse)
def create_clip_mark(
    request: Request,
    recording_id: int,
    start: float = Form(...),
    end: float = Form(...),
    label: str = Form(""),
    db=Depends(get_db),
    templates=Depends(get_templates),
):
    error = None
    if end > start >= 0:
        clip_marks_repo.insert(
            db,
            recording_id=recording_id,
            start_seconds=start,
            end_seconds=end,
            label=label.strip() or None,
        )
    else:
        error = (
            f"No se guardó: el rango {start:.1f}s-{end:.1f}s no es válido "
            "(el fin debe ser mayor al inicio)."
        )

    marks = clip_marks_repo.list_for_recording(db, recording_id)
    return templates.TemplateResponse(
        request,
        "partials/clip_marks.html",
        {"recording_id": recording_id, "marks": marks, "error": error},
    )


@router.delete("/clip-marks/{mark_id}")
def delete_clip_mark(mark_id: int, db=Depends(get_db)):
    clip_marks_repo.delete(db, mark_id)
    return PlainTextResponse("")


def _mask(value):
    value = str(value)
    return f"••••{value[-4:]}" if len(value) > 4 else "••••"


def _settings_context(saved=None):
    from utils.utils import read_cookies, read_telegram_config

    try:
        cookies = read_cookies()
    except Exception:
        cookies = {}
    try:
        telegram = read_telegram_config()
    except Exception:
        telegram = {}

    return {
        "saved": saved,
        "session_status": _mask(cookies["sessionid_ss"])
        if cookies.get("sessionid_ss")
        else None,
        "tt_target_idc": cookies.get("tt-target-idc", ""),
        "api_id_status": _mask(telegram["api_id"]) if telegram.get("api_id") else None,
        "api_hash_status": _mask(telegram["api_hash"])
        if telegram.get("api_hash")
        else None,
        "chat_id": telegram.get("chat_id", ""),
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, saved: str | None = None, templates=Depends(get_templates)
):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {**_settings_context(saved), **_discord_context(request)},
    )


def _settings_error(request, templates, message):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {**_settings_context(), **_discord_context(request), "error": message},
    )


@router.post("/settings/cookies", response_class=HTMLResponse)
def update_cookies(
    request: Request,
    sessionid_ss: str = Form(""),
    tt_target_idc: str = Form(""),
    templates=Depends(get_templates),
):
    from utils.utils import read_cookies, write_cookies

    try:
        current = read_cookies()
    except Exception:
        current = {}

    if sessionid_ss.strip():
        current["sessionid_ss"] = sessionid_ss.strip()
    if tt_target_idc.strip():
        current["tt-target-idc"] = tt_target_idc.strip()

    try:
        write_cookies(current)
    except OSError as e:
        return _settings_error(
            request,
            templates,
            f"No se pudo guardar cookies.json: {e}. "
            "¿Está montado como volumen de solo lectura (:ro)?",
        )

    if sessionid_ss.strip():
        from supervisor import alerts

        conn = get_connection(request.app.state.db_path)
        try:
            alerts.clear_session_problem(conn)
        finally:
            conn.close()

    return RedirectResponse(url="/settings?saved=cookies", status_code=303)


@router.post("/settings/telegram", response_class=HTMLResponse)
def update_telegram(
    request: Request,
    api_id: str = Form(""),
    api_hash: str = Form(""),
    chat_id: str = Form(""),
    templates=Depends(get_templates),
):
    from utils.utils import read_telegram_config, write_telegram_config

    try:
        current = read_telegram_config()
    except Exception:
        current = {}

    if api_id.strip():
        current["api_id"] = int(api_id) if api_id.strip().isdigit() else api_id.strip()
    if api_hash.strip():
        current["api_hash"] = api_hash.strip()
    if chat_id.strip():
        stripped = chat_id.strip()
        current["chat_id"] = (
            int(stripped) if stripped.lstrip("-").isdigit() else stripped
        )

    try:
        write_telegram_config(current)
    except OSError as e:
        return _settings_error(
            request,
            templates,
            f"No se pudo guardar telegram.json: {e}. "
            "¿Está montado como volumen de solo lectura (:ro)?",
        )

    return RedirectResponse(url="/settings?saved=telegram", status_code=303)


def _discord_context(request):
    from notify import discord_webhook

    from core import participants
    from supervisor import alerts

    conn = get_connection(request.app.state.db_path)
    try:
        url = settings_repo.get(conn, discord_webhook.SETTING_KEY)
        poll = settings_repo.get(conn, participants.SETTING_KEY)
        problem_alerts = alerts.enabled(conn)
        from supervisor.worker import reconnect_grace_seconds

        grace_seconds = reconnect_grace_seconds(conn)
        session_problem = alerts.last_session_problem(conn)
    finally:
        conn.close()
    from web import convert

    return {
        "convert_encoders": convert.ENCODERS,
        "convert_sizes": convert.SIZES,
        "convert_qualities": convert.QUALITIES,
        "convert_defaults": convert.DEFAULTS,
        "discord_status": _mask(url) if url else None,
        "problem_alerts": problem_alerts,
        "grace_minutes": round(grace_seconds / 60, 1),
        "session_problem": (
            {
                "when": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.gmtime(session_problem[0])
                ),
                "username": session_problem[1],
            }
            if session_problem
            else None
        ),
        "participants_poll_seconds": participants.clamp_interval(poll),
        "participants_min": participants.MIN_POLL_SECONDS,
        "participants_max": participants.MAX_POLL_SECONDS,
    }


@router.post("/settings/participants", response_class=HTMLResponse)
def update_participants_interval(
    request: Request,
    seconds: str = Form(""),
    db=Depends(get_db),
    templates=Depends(get_templates),
):
    from core import participants

    try:
        value = int(seconds)
    except ValueError:
        value = None
    if value is None or not (
        participants.MIN_POLL_SECONDS <= value <= participants.MAX_POLL_SECONDS
    ):
        return _settings_error(
            request,
            templates,
            f"El intervalo tiene que ser un número entre "
            f"{participants.MIN_POLL_SECONDS} y {participants.MAX_POLL_SECONDS} segundos.",
        )
    settings_repo.set(db, participants.SETTING_KEY, str(value))
    return RedirectResponse(url="/settings?saved=participants", status_code=303)


@router.post("/settings/reconnect-grace", response_class=HTMLResponse)
def update_reconnect_grace(
    request: Request,
    minutes: str = Form(""),
    db=Depends(get_db),
    templates=Depends(get_templates),
):
    from supervisor.worker import RECONNECT_GRACE_KEY, RECONNECT_GRACE_MAX

    try:
        seconds = round(float(minutes.replace(",", ".")) * 60)
    except ValueError:
        seconds = -1
    if not 0 <= seconds <= RECONNECT_GRACE_MAX:
        return _settings_error(
            request,
            templates,
            f"La espera tiene que ser entre 0 y {RECONNECT_GRACE_MAX // 60} minutos.",
        )
    settings_repo.set(db, RECONNECT_GRACE_KEY, str(seconds))
    return RedirectResponse(url="/settings?saved=reconnect-grace", status_code=303)


@router.post("/settings/problem-alerts", response_class=HTMLResponse)
def update_problem_alerts(enabled: str = Form(""), db=Depends(get_db)):
    from supervisor import alerts

    settings_repo.set(db, alerts.ENABLED_KEY, "1" if enabled else "0")
    return RedirectResponse(url="/settings?saved=problem-alerts", status_code=303)


@router.post("/settings/discord", response_class=HTMLResponse)
def update_discord(
    request: Request,
    webhook_url: str = Form(""),
    clear: str = Form(""),
    db=Depends(get_db),
    templates=Depends(get_templates),
):
    from notify import discord_webhook

    if clear:
        settings_repo.set(db, discord_webhook.SETTING_KEY, None)
        return RedirectResponse(url="/settings?saved=discord-cleared", status_code=303)

    webhook_url = webhook_url.strip()
    if webhook_url:
        if not discord_webhook.is_valid_url(webhook_url):
            return _settings_error(
                request,
                templates,
                "Esa URL no parece un webhook de Discord "
                "(tiene que empezar con https://discord.com/api/webhooks/).",
            )
        settings_repo.set(db, discord_webhook.SETTING_KEY, webhook_url)

    return RedirectResponse(url="/settings?saved=discord", status_code=303)


@router.post("/settings/discord/test", response_class=HTMLResponse)
def test_discord(
    request: Request, db=Depends(get_db), templates=Depends(get_templates)
):
    from notify import discord_webhook

    error = discord_webhook.send(
        settings_repo.get(db, discord_webhook.SETTING_KEY),
        discord_webhook.test_embed(),
    )
    if error:
        return _settings_error(request, templates, error)
    return RedirectResponse(url="/settings?saved=discord-test", status_code=303)


@router.post("/channels/{channel_id}/notify", response_class=HTMLResponse)
def toggle_channel_notify(
    request: Request,
    channel_id: int,
    db=Depends(get_db),
    supervisor=Depends(get_supervisor),
    templates=Depends(get_templates),
):
    row = channels_repo.get(db, channel_id)
    if row is not None:
        channels_repo.update(db, channel_id, use_telegram=int(not row["use_telegram"]))
        row = channels_repo.get(db, channel_id)

    return templates.TemplateResponse(
        request,
        "partials/channel_row.html",
        {"channel": _channel_view(row, supervisor, db)},
    )


def _creator_view(summary, channels_by_name, supervisor):
    entry = dict(summary)
    channel = channels_by_name.get(summary["username"])
    entry["channel"] = _channel_view(channel, supervisor) if channel else None
    return entry


def _is_off_hours(db, recording):
    """True when a creator with a learned schedule went live at an unusual hour."""
    from core import schedule as schedule_core
    from supervisor import schedules

    if recording is None:
        return False
    start = schedule_core.parse_utc(recording["started_at"])
    learned = schedules.get(db, recording["username"])
    return bool(
        start and learned["learned"] and not schedule_core.is_usual(learned, start)
    )


def _schedule_view(db, username):
    from core import schedule as schedule_core
    from supervisor import schedules

    learned = schedules.get(db, username)
    return {
        **learned,
        "probs": [round(p, 3) for p in learned["probs"]],
        "threshold": learned.get("threshold", schedule_core.USUAL_THRESHOLD),
        "min_lives": schedule_core.MIN_LIVES,
        "min_days": schedule_core.MIN_DAYS,
    }


@router.get("/creators", response_class=HTMLResponse)
def creators_index(
    request: Request,
    db=Depends(get_db),
    supervisor=Depends(get_supervisor),
    templates=Depends(get_templates),
):
    channels_by_name = {row["username"]: row for row in channels_repo.list_all(db)}
    creators = [
        _creator_view(s, channels_by_name, supervisor)
        for s in recordings_repo.creator_summaries(db)
    ]
    # Channels that are tracked but haven't produced a recording yet still
    # deserve a card, so the page doubles as an overview of everything.
    seen = {c["username"] for c in creators}
    for name, channel in channels_by_name.items():
        if name not in seen:
            creators.append(
                {
                    "username": name,
                    "total": 0,
                    "raw_count": 0,
                    "live_count": 0,
                    "total_seconds": 0,
                    "total_bytes": 0,
                    "last_started_at": None,
                    "channel": _channel_view(channel, supervisor),
                }
            )
    return templates.TemplateResponse(
        request,
        "creators.html",
        {
            "creators": creators,
            "profiles": profiles_repo.all_by_username(db),
            "totals": recordings_repo.totals(db),
            "disk": _disk_usage(request),
        },
    )


@router.get("/creators/{username}", response_class=HTMLResponse)
def creator_detail(
    request: Request,
    username: str,
    db=Depends(get_db),
    supervisor=Depends(get_supervisor),
    templates=Depends(get_templates),
):
    username = username.lstrip("@")
    channels_by_name = {row["username"]: row for row in channels_repo.list_all(db)}
    summaries = recordings_repo.creator_summaries(db, username)
    if not summaries and username not in channels_by_name:
        raise HTTPException(status_code=404, detail="Creador no encontrado")

    channel = channels_by_name.get(username)
    summary = dict(summaries[0]) if summaries else None
    recordings = [dict(r) for r in recordings_repo.list_for_username(db, username)]
    counts = clip_marks_repo.count_for_recordings(db, [r["id"] for r in recordings])
    for r in recordings:
        r["clip_count"] = counts.get(r["id"], 0)

    completed = [r for r in recordings if r["duration_seconds"]]
    average_seconds = (
        sum(r["duration_seconds"] for r in completed) / len(completed)
        if completed
        else 0
    )
    return templates.TemplateResponse(
        request,
        "creator.html",
        {
            "username": username,
            "profile": profiles_repo.get(db, username),
            "schedule": _schedule_view(db, username),
            "summary": summary,
            "channel": _channel_view(channel, supervisor) if channel else None,
            "recordings": recordings,
            "average_seconds": average_seconds,
        },
    )
