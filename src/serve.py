import os


def main():
    import uvicorn

    from db.database import init_db
    from supervisor.reconcile import recover_orphaned_recordings
    from supervisor.supervisor import ChannelSupervisor
    from utils.dependencies import check_ffmpeg
    from utils.utils import banner
    from web.app import create_app

    banner()

    ffmpeg_path = os.environ.get("FFMPEG_PATH", "ffmpeg")
    check_ffmpeg(ffmpeg_path)

    output_dir = os.environ.get("OUTPUT_DIR", "/output")
    os.makedirs(output_dir, exist_ok=True)

    db_path = os.environ.get("HUB_DB_PATH", os.path.join(output_dir, "hub.db"))
    init_db(db_path)

    # Any recording still marked "recording" at this point predates this
    # process (a container restart/crash mid-recording, most commonly) -
    # finish converting it or mark it failed before workers start.
    recover_orphaned_recordings(db_path, ffmpeg_path=ffmpeg_path)

    reconcile_interval = int(os.environ.get("HUB_RECONCILE_INTERVAL", "5"))
    supervisor = ChannelSupervisor(
        db_path, output_dir, reconcile_interval=reconcile_interval
    )
    supervisor.start()

    app = create_app(db_path, supervisor, output_dir)

    host = os.environ.get("HUB_HOST", "0.0.0.0")
    port = int(os.environ.get("HUB_PORT", "8000"))
    # uvicorn installs its own SIGTERM/SIGINT handlers here and runs the
    # app's lifespan shutdown (which calls supervisor.stop(wait=True)) on
    # `docker stop`, so recordings in progress get to finish converting
    # instead of being killed mid-write.
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
