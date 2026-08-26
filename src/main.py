import sys
import os
import signal
import multiprocessing

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _raise_keyboard_interrupt(signum, frame):
    """
    Translate SIGTERM (e.g. `docker stop`) into the same graceful shutdown
    path already used for Ctrl-C, so an in-progress recording gets flushed
    and converted instead of being killed mid-write.
    """
    raise KeyboardInterrupt()


def record_user(config):
    from core.tiktok_recorder import TikTokRecorder
    from utils.logger_manager import logger

    try:
        TikTokRecorder(config).run()
    except Exception as e:
        logger.error(f"{e}", exc_info=True)


def _build_config(args, mode, cookies, user=None):
    from utils.recorder_config import RecorderConfig

    return RecorderConfig(
        url=args.url,
        user=user,
        room_id=args.room_id,
        mode=mode,
        automatic_interval=args.automatic_interval,
        cookies=cookies,
        proxy=args.proxy,
        output=args.output,
        duration=args.duration,
        use_telegram=args.telegram,
        bitrate=args.bitrate,
        ffmpeg_path=args.ffmpeg_path,
    )


def run_recordings(args, mode, cookies):
    # Registered before any child process is forked, so children (which
    # inherit signal handlers on fork) also convert SIGTERM into a
    # graceful stop instead of dying mid-recording/mid-conversion.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    if isinstance(args.user, list):
        processes = []
        for user in args.user:
            config = _build_config(args, mode, cookies, user=user)
            p = multiprocessing.Process(target=record_user, args=(config,))
            p.start()
            processes.append(p)
        try:
            for p in processes:
                p.join()
        except KeyboardInterrupt:
            print("\n[!] Stopping all recordings (Ctrl-C or SIGTERM)...")
            for p in processes:
                if p.is_alive():
                    p.terminate()
            try:
                for p in processes:
                    p.join()
            except KeyboardInterrupt:
                print("\n[!] Forcefully terminating all processes.")
                for p in processes:
                    if p.is_alive():
                        p.kill()
    else:
        config = _build_config(args, mode, cookies, user=args.user)
        record_user(config)


def main():
    from utils.args_handler import validate_and_parse_args
    from utils.utils import read_cookies
    from utils.logger_manager import logger
    from utils.custom_exceptions import TikTokRecorderError
    from utils.dependencies import check_ffmpeg
    from check_updates import check_updates

    try:
        # validate and parse command line arguments
        args, mode = validate_and_parse_args()

        # check ffmpeg binary (supports custom path via -ffmpeg-path)
        check_ffmpeg(args.ffmpeg_path or "ffmpeg")

        # check for updates
        if args.update_check is True:
            logger.info("Checking for updates...\n")
            if check_updates():
                exit()
        else:
            logger.info("Skipped update check\n")

        # read cookies from the config file
        cookies = read_cookies()

        # run the recordings based on the parsed arguments
        run_recordings(args, mode, cookies)

    except TikTokRecorderError as ex:
        logger.error(f"Application Error: {ex}")

    except Exception as ex:
        logger.critical(f"Generic Error: {ex}", exc_info=True)


if __name__ == "__main__":
    # print the banner
    from utils.utils import banner

    banner()

    # check and install dependencies
    from utils.dependencies import check_and_install_dependencies

    check_and_install_dependencies()

    # set up signal handling for graceful shutdown
    multiprocessing.freeze_support()

    # run
    main()
