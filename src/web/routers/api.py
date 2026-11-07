"""
Small JSON API used by the library page (convert command) and by the
auto-convert script that runs on the user's PC (tools/convert-pending.ps1).
"""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from db import recordings_repo
from web import convert
from web.deps import get_db
from web.formatting import basename

router = APIRouter()

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "tools" / "convert-pending.ps1"


def _recording_json(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "file_name": basename(row["file_path"]),
        "output_name": convert.output_name(basename(row["file_path"])),
        "format": row["format"],
        "status": row["status"],
        "size_bytes": row["file_size_bytes"],
        "duration_seconds": row["duration_seconds"],
        "started_at": row["started_at"],
    }


@router.get("/api/recordings")
def list_recordings(state: str | None = None, db=Depends(get_db)):
    """`state=raw` lists finished recordings still waiting to be converted."""
    rows = recordings_repo.list_filtered(db, state=state)
    # Oldest first: the script works through the backlog in recording order.
    rows = sorted(rows, key=lambda r: r["started_at"] or "")
    return {"recordings": [_recording_json(r) for r in rows]}


@router.get("/api/recordings/{recording_id}")
def get_recording(recording_id: int, db=Depends(get_db)):
    row = recordings_repo.get(db, recording_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    return _recording_json(row)


@router.get("/api/convert/args")
def convert_args(
    encoder: str | None = None, size: str | None = None, quality: str | None = None
):
    """
    ffmpeg arguments with `{input}` / `{output}` placeholders, for the
    script to substitute its local paths into.
    """
    options = convert.normalize(encoder, size, quality)
    return {
        "options": options,
        "args": convert.ffmpeg_args("{input}", "{output}", options),
    }


@router.get("/api/convert/command")
def convert_command(
    recording_id: int,
    encoder: str | None = None,
    size: str | None = None,
    quality: str | None = None,
    scope: str = "file",
    db=Depends(get_db),
):
    row = recordings_repo.get(db, recording_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    options = convert.normalize(encoder, size, quality)
    return {
        "command": convert.command_text(
            basename(row["file_path"]),
            options,
            scope="folder" if scope == "folder" else "file",
        )
    }


def public_base_url(request):
    """
    The address clients outside the container use to reach the hub.
    Behind a reverse proxy that terminates HTTPS the app itself only sees
    plain http, so: HUB_PUBLIC_URL if set, else the proxy's
    X-Forwarded-Proto / X-Forwarded-Host, else the request's own URL.
    (Getting this wrong once produced an http:// script whose uploads were
    silently redirected and dropped.)
    """
    configured = os.environ.get("HUB_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    base = str(request.base_url).rstrip("/")
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if proto or host:
        scheme, _, rest = base.partition("://")
        netloc, _, path = rest.partition("/")
        base = f"{proto or scheme}://{host or netloc}" + (f"/{path}" if path else "")
    return base


@router.get("/tools/convert-pending.ps1")
def download_convert_script(request: Request):
    """The auto-convert script, pre-filled with this hub's address."""
    hub_url = public_base_url(request)
    script = _SCRIPT_PATH.read_text(encoding="utf-8").replace("__HUB_URL__", hub_url)
    # UTF-8 with BOM: Windows PowerShell 5.1 reads BOM-less files as ANSI,
    # which would garble the Spanish messages.
    return PlainTextResponse(
        "\ufeff" + script,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="convert-pending.ps1"'},
    )
