from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web.routers import media, pages

_PACKAGE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"


def create_app(db_path, supervisor, output_dir):
    """
    Builds the hub's FastAPI app around an already-started ChannelSupervisor.
    `db_path`/`output_dir` are shared with the supervisor so routes and
    workers agree on where channels/recordings/media live.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Draining here (not just relying on the caller) means `uvicorn`'s
        # own shutdown on SIGTERM/SIGINT also stops recordings gracefully.
        supervisor.stop(wait=True)

    app = FastAPI(title="TikTok Live Recorder Hub", lifespan=lifespan)
    app.state.db_path = str(db_path)
    app.state.supervisor = supervisor
    app.state.output_dir = str(output_dir)
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(pages.router)
    app.include_router(media.router)

    return app
