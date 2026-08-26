from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from db import channels_repo, recordings_repo
from utils.enums import Mode
from web.deps import get_db, get_supervisor, get_templates

router = APIRouter()


def _channel_view(row, supervisor):
    status = supervisor.get_status(row["id"]) or {}
    default_phase = "stopped" if not row["enabled"] else "starting"
    return {
        "id": row["id"],
        "username": row["username"],
        "mode": Mode(row["mode"]).name.title(),
        "automatic_interval": row["automatic_interval"],
        "proxy": row["proxy"],
        "bitrate": row["bitrate"],
        "enabled": bool(row["enabled"]),
        "phase": status.get("phase", default_phase),
        "detail": status.get("detail"),
    }


def _render_channels(request, db, supervisor, templates, template_name):
    channels = [_channel_view(row, supervisor) for row in channels_repo.list_all(db)]
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
    bitrate: str = Form(""),
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
            bitrate=bitrate.strip() or None,
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
        {"channel": _channel_view(row, supervisor)},
    )


@router.delete("/channels/{channel_id}")
def delete_channel(channel_id: int, db=Depends(get_db)):
    channels_repo.delete(db, channel_id)
    return PlainTextResponse("")


@router.get("/library", response_class=HTMLResponse)
def library(request: Request, db=Depends(get_db), templates=Depends(get_templates)):
    channels = channels_repo.list_all(db)
    recordings = recordings_repo.list_filtered(db)
    return templates.TemplateResponse(
        request, "library.html", {"channels": channels, "recordings": recordings}
    )


@router.get("/partials/recordings", response_class=HTMLResponse)
def recordings_partial(
    request: Request,
    channel_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db=Depends(get_db),
    templates=Depends(get_templates),
):
    recordings = recordings_repo.list_filtered(
        db,
        channel_id=channel_id,
        date_from=date_from or None,
        date_to=f"{date_to} 23:59:59" if date_to else None,
    )
    return templates.TemplateResponse(
        request, "partials/recording_cards.html", {"recordings": recordings}
    )


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
        request, "settings.html", _settings_context(saved)
    )


@router.post("/settings/cookies")
def update_cookies(sessionid_ss: str = Form(""), tt_target_idc: str = Form("")):
    from utils.utils import read_cookies, write_cookies

    try:
        current = read_cookies()
    except Exception:
        current = {}

    if sessionid_ss.strip():
        current["sessionid_ss"] = sessionid_ss.strip()
    if tt_target_idc.strip():
        current["tt-target-idc"] = tt_target_idc.strip()

    write_cookies(current)
    return RedirectResponse(url="/settings?saved=cookies", status_code=303)


@router.post("/settings/telegram")
def update_telegram(
    api_id: str = Form(""), api_hash: str = Form(""), chat_id: str = Form("")
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

    write_telegram_config(current)
    return RedirectResponse(url="/settings?saved=telegram", status_code=303)
