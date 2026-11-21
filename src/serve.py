import os
import threading


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
    # keep its raw .ts or mark it failed. Runs in the background so a big
    # backlog of orphaned files doesn't delay the dashboard from coming up.
    threading.Thread(
        target=recover_orphaned_recordings,
        args=(db_path,),
        kwargs={"ffmpeg_path": ffmpeg_path},
        name="orphan-recovery",
        daemon=True,
    ).start()

    # Guest times saved before they followed the video clock: fix the old
    # raw recordings once (reads a few MB per file, in the background).
    from datetime import datetime, timezone

    from db.database import get_connection
    from supervisor.guest_times import run_at_startup

    threading.Thread(
        target=run_at_startup,
        args=(
            db_path,
            get_connection,
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        ),
        name="guest-times-fix",
        daemon=True,
    ).start()

    # Guest names on recordings from before panels were named by their cid
    # (core/layout.py): rename them from the cids their debug files saved.
    from core.tiktok_api import TikTokAPI
    from supervisor.guest_names import run_at_startup as rename_guests
    from utils.utils import read_cookies

    threading.Thread(
        target=rename_guests,
        args=(
            db_path,
            get_connection,
            lambda: TikTokAPI(proxy=None, cookies=read_cookies()),
        ),
        name="guest-names-fix",
        daemon=True,
    ).start()

    reconcile_interval = int(os.environ.get("HUB_RECONCILE_INTERVAL", "5"))
    supervisor = ChannelSupervisor(
        db_path, output_dir, reconcile_interval=reconcile_interval
    )
    supervisor.start()

    # Problem alerts (channel stuck in error, recording that stopped growing).
    from db.database import get_connection
    from supervisor.health import HealthMonitor

    HealthMonitor(db_path, get_connection, supervisor.status).start()

    # Seek-bar preview thumbnails for converted MP4s (low priority).
    from supervisor.storyboards import StoryboardWorker

    storyboards = StoryboardWorker(db_path, get_connection, ffmpeg_path=ffmpeg_path)
    storyboards.start()

    # Canary: alerts if live detection stops working (TikTok changes).
    from supervisor.canary import DetectionCanary

    DetectionCanary(
        db_path,
        get_connection,
        lambda: TikTokAPI(proxy=None, cookies=read_cookies()),
    ).start()

    # Lives whose recording was deleted before live_history existed are
    # still in the daily backups: put them back into the schedule history.
    from supervisor import backups as _backups

    _conn = get_connection(db_path)
    try:
        _backups.recover_live_history(_conn, _backups.backup_dir(output_dir))
    except Exception as e:
        print(f"Live history recovery failed: {e}")
    finally:
        _conn.close()

    # Hourly: delete expired kept originals, daily hub.db backup.
    from supervisor.housekeeping import Housekeeping

    Housekeeping(db_path, get_connection, output_dir).start()

    app = create_app(db_path, supervisor, output_dir)
    app.state.storyboards = storyboards

    host = os.environ.get("HUB_HOST", "0.0.0.0")
    port = int(os.environ.get("HUB_PORT", "8000"))
    # uvicorn installs its own SIGTERM/SIGINT handlers here and runs the
    # app's lifespan shutdown (which calls supervisor.stop(wait=True)) on
    # `docker stop`, so recordings in progress get to close their file
    # cleanly instead of being killed mid-write.
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
