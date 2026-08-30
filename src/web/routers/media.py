from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from db import recordings_repo
from web.deps import get_db

router = APIRouter()

_PLACEHOLDER_THUMBNAIL = (
    Path(__file__).resolve().parent.parent / "static" / "placeholder.svg"
)

_MEDIA_TYPES = {"mkv": "video/x-matroska", "flv": "video/x-flv"}


@router.get("/media/{recording_id}")
def get_media(recording_id: int, db=Depends(get_db)):
    """
    Serves a recording by DB id only - never a client-supplied path, so
    there's no path-traversal surface. FileResponse already handles HTTP
    Range requests, which is what lets <video> scrub/seek in the browser.
    """
    recording = recordings_repo.get(db, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    path = Path(recording["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Recording file not found on disk")

    media_type = _MEDIA_TYPES.get(recording["format"], "video/mp4")
    return FileResponse(path, media_type=media_type)


@router.get("/media/{recording_id}/download")
def download_media(recording_id: int, db=Depends(get_db)):
    """Same file as /media/{id}, but forces a "Save As" download with a
    real filename instead of playing inline."""
    recording = recordings_repo.get(db, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    path = Path(recording["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Recording file not found on disk")

    return FileResponse(path, filename=path.name)


@router.get("/media/{recording_id}/thumbnail")
def get_thumbnail(recording_id: int, db=Depends(get_db)):
    recording = recordings_repo.get(db, recording_id)
    thumbnail_path = recording["thumbnail_path"] if recording else None

    if thumbnail_path and Path(thumbnail_path).is_file():
        return FileResponse(thumbnail_path, media_type="image/jpeg")

    if _PLACEHOLDER_THUMBNAIL.is_file():
        return FileResponse(_PLACEHOLDER_THUMBNAIL, media_type="image/svg+xml")

    raise HTTPException(status_code=404, detail="No thumbnail available")
