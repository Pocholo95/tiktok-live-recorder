import json
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from core import stream_stats
from db import profiles_repo, recordings_repo
from db.database import get_connection
from supervisor import originals, storyboards
from supervisor.thumbnails import generate_thumbnail
from utils.logger_manager import logger
from web.deps import get_db

router = APIRouter()

_PLACEHOLDER_THUMBNAIL = (
    Path(__file__).resolve().parent.parent / "static" / "placeholder.svg"
)

_MEDIA_TYPES = {
    "mkv": "video/x-matroska",
    "flv": "video/x-flv",
    "ts": "video/mp2t",
}

# Codecs every current desktop/mobile browser can play inside an MP4.
_BROWSER_VIDEO_CODECS = {"h264", "av1", "vp9"}


def _lookup(request, recording_id):
    """
    The recording row, read with a connection that is closed right away.
    Endpoints that stream a file use this instead of `Depends(get_db)`,
    whose connection stays open until the whole response is sent - for a
    video that's minutes, and every open one also holds hub.db file
    descriptors.
    """
    conn = get_connection(request.app.state.db_path)
    try:
        return recordings_repo.get(conn, recording_id)
    finally:
        conn.close()


@router.get("/media/{recording_id}")
def get_media(recording_id: int, request: Request):
    """
    Serves a recording by DB id only - never a client-supplied path, so
    there's no path-traversal surface. FileResponse already handles HTTP
    Range requests, which is what lets <video> scrub/seek in the browser.
    """
    recording = _lookup(request, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    path = Path(recording["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Recording file not found on disk")

    media_type = _MEDIA_TYPES.get(recording["format"], "video/mp4")
    return FileResponse(path, media_type=media_type)


@router.get("/media/{recording_id}/download")
def download_media(recording_id: int, request: Request):
    """Same file as /media/{id}, but forces a "Save As" download with a
    real filename instead of playing inline."""
    recording = _lookup(request, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    path = Path(recording["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Recording file not found on disk")

    return FileResponse(path, filename=path.name)


@router.get("/media/{recording_id}/original")
def download_original(recording_id: int, request: Request):
    """The raw .ts a conversion replaced, while it's still kept."""
    path = originals.available(_lookup(request, recording_id))
    if path is None:
        raise HTTPException(status_code=404, detail="Original not kept")
    return FileResponse(path, filename=path.name)


@router.get("/media/{recording_id}/clip")
def download_clip(recording_id: int, start: float, end: float, db=Depends(get_db)):
    """
    Cuts [start, end] out of the recording and streams the result straight
    to the client as ffmpeg produces it - never written to disk. Stream
    copy (no re-encode) keeps this fast; `frag_keyframe+empty_moov` is what
    lets ffmpeg write a valid mp4 to a pipe at all (a plain mp4 needs to
    seek back and finish the moov atom at the end, which a pipe can't do).
    """
    if start < 0 or end <= start:
        raise HTTPException(status_code=400, detail="Invalid start/end range")

    recording = recordings_repo.get(db, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    path = Path(recording["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Recording file not found on disk")

    ffmpeg_path = os.environ.get("FFMPEG_PATH", "ffmpeg")
    cmd = [
        ffmpeg_path,
        "-ss",
        str(start),
        "-to",
        str(end),
        "-i",
        str(path),
        "-c",
        "copy",
        "-movflags",
        "frag_keyframe+empty_moov",
        "-f",
        "mp4",
        "pipe:1",
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def stream():
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            process.stdout.close()
            process.wait()

    filename = f"{path.stem}_clip_{int(start)}-{int(end)}.mp4"
    return StreamingResponse(
        stream(),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/media/{recording_id}/storyboard")
def get_storyboard(recording_id: int, db=Depends(get_db)):
    """Seek-bar preview grid (see supervisor/storyboards.py); 404 until built."""
    recording = recordings_repo.get(db, recording_id)
    meta = storyboards.load_meta(recording["file_path"]) if recording else None
    if meta is None:
        return JSONResponse(
            {"detail": "No storyboard yet"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(meta, headers={"Cache-Control": "no-store"})


@router.get("/media/{recording_id}/storyboard/{sheet}.jpg")
def get_storyboard_sheet(recording_id: int, sheet: int, request: Request):
    recording = _lookup(request, recording_id)
    if recording is None or not 1 <= sheet <= 999:
        raise HTTPException(status_code=404, detail="Not found")
    path = storyboards.storyboard_dir(recording["file_path"]) / f"sheet_{sheet:03d}.jpg"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"}
    )


@router.get("/media/{recording_id}/thumbnail")
def get_thumbnail(recording_id: int, request: Request):
    recording = _lookup(request, recording_id)
    thumbnail_path = recording["thumbnail_path"] if recording else None

    if thumbnail_path and Path(thumbnail_path).is_file():
        return FileResponse(thumbnail_path, media_type="image/jpeg")

    if _PLACEHOLDER_THUMBNAIL.is_file():
        return FileResponse(_PLACEHOLDER_THUMBNAIL, media_type="image/svg+xml")

    raise HTTPException(status_code=404, detail="No thumbnail available")


def _probe(path):
    """ffprobe summary of a media file, or None if it isn't readable media."""
    ffprobe_path = os.environ.get("FFPROBE_PATH", "ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name:stream=codec_type,codec_name",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def _finalize_upload(db_path, recording_id, part_path, final_path):
    """
    Validate the uploaded MP4 and swap it in for the raw recording. Runs in
    a worker thread (ffprobe + thumbnail are blocking) with its own DB
    connection. Returns (status_code, message).
    """
    info = _probe(part_path)
    video_codecs = [
        s.get("codec_name")
        for s in (info or {}).get("streams", [])
        if s.get("codec_type") == "video"
    ]
    format_name = (info or {}).get("format", {}).get("format_name", "")
    if not video_codecs or "mp4" not in format_name:
        part_path.unlink(missing_ok=True)
        return 400, "El archivo no es un MP4 con video válido."
    if video_codecs[0] not in _BROWSER_VIDEO_CODECS:
        part_path.unlink(missing_ok=True)
        return 400, (
            f"El video es {video_codecs[0]}, que el navegador no reproduce. "
            "Usá H.264 o AV1."
        )

    try:
        duration = round(float(info["format"]["duration"]))
    except (KeyError, TypeError, ValueError):
        duration = None

    conn = get_connection(db_path)
    try:
        recording = recordings_repo.get(conn, recording_id)
        if recording is None:
            part_path.unlink(missing_ok=True)
            return 404, "La grabación ya no existe."

        raw_path = Path(recording["file_path"])
        os.replace(part_path, final_path)
        thumbnail_path = generate_thumbnail(str(final_path))
        recordings_repo.replace_with_converted(
            conn,
            recording_id,
            file_path=str(final_path),
            file_size_bytes=final_path.stat().st_size,
            duration_seconds=duration,
            thumbnail_path=thumbnail_path,
        )
        # The raw file is kept for a few days (Settings) in case the
        # conversion turns out broken; see supervisor/originals.py.
        originals.after_conversion(conn, recording_id, raw_path, final_path)
    finally:
        conn.close()

    logger.info(f"Recording #{recording_id} replaced with converted {final_path}")
    return 200, "Listo"


@router.put("/recordings/{recording_id}/converted")
async def upload_converted(recording_id: int, request: Request):
    """
    Receives the MP4 converted offline (on the user's GPU) as the raw
    request body and swaps it in for the raw .ts/.flv. The body is streamed
    straight to a `.part` file next to the recording - never buffered in
    memory or a temp dir - and only replaces the raw file once ffprobe
    confirms it's a browser-playable MP4. Any failure leaves the raw
    recording untouched.
    """
    db_path = request.app.state.db_path

    def lookup():
        conn = get_connection(db_path)
        try:
            return recordings_repo.get(conn, recording_id)
        finally:
            conn.close()

    recording = await run_in_threadpool(lookup)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    if recording["status"] != "completed":
        raise HTTPException(status_code=409, detail="La grabación todavía no terminó.")

    raw_path = Path(recording["file_path"])
    final_path = raw_path.with_suffix(".mp4")
    part_path = raw_path.with_suffix(".mp4.part")

    try:
        with open(part_path, "wb") as out:
            async for chunk in request.stream():
                out.write(chunk)
    except Exception:
        part_path.unlink(missing_ok=True)
        raise

    if part_path.stat().st_size == 0:
        part_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="El archivo llegó vacío.")

    status_code, message = await run_in_threadpool(
        _finalize_upload, db_path, recording_id, part_path, final_path
    )
    if status_code == 200:
        # A re-upload replaces the old one. The converting PC normally
        # uploads the new one right after (PUT .../storyboard); the hub's
        # worker only builds it if none arrives within its grace period.
        storyboards.remove(final_path)
    return JSONResponse({"detail": message}, status_code=status_code)


_MAX_STORYBOARD_ZIP = storyboards.MAX_SHEETS * storyboards.MAX_SHEET_BYTES


@router.put("/recordings/{recording_id}/storyboard")
async def upload_storyboard(recording_id: int, interval: int, request: Request):
    """
    Seek-bar thumbnails made by the PC that converted the recording (same
    ffmpeg run), as a zip of `sheet_NNN.jpg`. Only for a converted MP4;
    validated before replacing anything (see storyboards.install_uploaded).
    """
    db_path = request.app.state.db_path

    def lookup():
        conn = get_connection(db_path)
        try:
            return recordings_repo.get(conn, recording_id)
        finally:
            conn.close()

    recording = await run_in_threadpool(lookup)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    if recording["format"] != "mp4" or recording["status"] != "completed":
        raise HTTPException(status_code=409, detail="La grabación no es un MP4 listo.")

    video_path = Path(recording["file_path"])
    zip_path = video_path.with_suffix(".storyboard.zip.part")
    received = 0
    try:
        with open(zip_path, "wb") as out:
            async for chunk in request.stream():
                received += len(chunk)
                if received > _MAX_STORYBOARD_ZIP:
                    raise HTTPException(status_code=413, detail="Demasiado grande.")
                out.write(chunk)
        meta, problem = await run_in_threadpool(
            storyboards.install_uploaded,
            video_path,
            zip_path,
            interval,
            recording["duration_seconds"],
            os.environ.get("FFPROBE_PATH", "ffprobe"),
        )
    finally:
        zip_path.unlink(missing_ok=True)
    if meta is None:
        return JSONResponse({"detail": problem}, status_code=400)
    return JSONResponse({"detail": "Listo", "sheets": meta["sheets"]})


@router.get("/avatars/{username}")
def get_avatar(username: str, db=Depends(get_db)):
    """
    Creator profile picture saved by the recorder. Looked up by username in
    the DB (never a client-supplied path). Misses are cached briefly too, so
    pages that poll (the dashboard) don't re-request every few seconds.
    """
    profile = profiles_repo.get(db, username.lstrip("@"))
    path = Path(profile["avatar_path"]) if profile and profile["avatar_path"] else None
    if path is None or not path.is_file():
        return Response(status_code=404, headers={"Cache-Control": "max-age=600"})
    return FileResponse(
        path,
        media_type=_image_type(path),
        headers={"Cache-Control": "max-age=3600"},
    )


def _image_type(path):
    """TikTok serves avatars as WebP or JPEG regardless of the URL - sniff it."""
    with open(path, "rb") as f:
        head = f.read(12)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[1:4] == b"PNG":
        return "image/png"
    return "image/jpeg"


@router.get("/recordings/{recording_id}/stats")
def download_stream_stats(recording_id: int, db=Depends(get_db)):
    """What arrived from TikTok while recording (core/stream_stats.py)."""
    recording = recordings_repo.get(db, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    path = stream_stats.stats_path(recording["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No stats for this recording")
    return FileResponse(path, media_type="application/json", filename=path.name)


def participants_debug_path(recording_file_path):
    """Raw TikTok link data the guest tracker logged next to a recording."""
    return Path(recording_file_path).with_suffix(".participants.jsonl")


@router.get("/recordings/{recording_id}/participants-debug")
def download_participants_debug(recording_id: int, db=Depends(get_db)):
    recording = recordings_repo.get(db, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    path = participants_debug_path(recording["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No diagnostics for this recording")
    return FileResponse(path, media_type="application/x-ndjson", filename=path.name)
