"""
A browser seeking a video abandons the previous range request. The hub
must stop reading the file then: with Starlette 1.6 it kept reading every
abandoned response to the end of the MP4, and on the NAS hundreds of
those piled up until the process ran out of file descriptors (1024) and
froze (2026-11-21).
"""

import sys
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from db import recordings_repo  # noqa: E402
from db.database import get_connection  # noqa: E402

CHUNK = 64 * 1024


def _request(app, path, headers):
    sent = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},  # what uvicorn sends
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
        "state": {},
    }

    async def run():
        disconnected = anyio.Event()
        requested = False

        async def receive():
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            bodies = [m for m in sent if m["type"] == "http.response.body"]
            if len(bodies) >= 3:
                disconnected.set()  # the browser moved on after a few chunks
            await anyio.sleep(0.001)

        with anyio.fail_after(20):
            await app(scope, receive, send)

    anyio.run(run)
    return [m for m in sent if m["type"] == "http.response.body"]


def test_abandoned_range_request_stops_reading_the_file(app, tmp_path):
    video = tmp_path / "TK_big.mp4"
    video.write_bytes(b"\0" * (CHUNK * 400))  # 25 MB = 400 chunks
    conn = get_connection(app.state.db_path)
    rid = recordings_repo.start(
        conn, channel_id=None, username="c", file_path=str(video)
    )
    recordings_repo.mark_completed(conn, rid, file_path=str(video), format="mp4")

    for headers in ({"range": "bytes=1000-"}, {}):
        bodies = _request(app, f"/media/{rid}", headers)
        assert len(bodies) < 20, f"kept streaming after the client left: {headers}"
