# CLAUDE.md

Fork of [Michele0303/tiktok-live-recorder](https://github.com/Michele0303/tiktok-live-recorder)
(remote `upstream`; our fork is `origin`). The fork adds a self-hosted web hub
(`serve` mode) on top of the original CLI recorder.

## Working rules

- Commit locally as soon as a change is complete and working (tests pass,
  checked in the app if it's UI). Never commit half-done or broken work.
  Don't push unless asked.
- The upstream `develop` branch has a fix ("handle paused and HLS live
  streams") that conflicts with our recorder; the user chose not to merge it
  for now. Don't merge upstream without asking.
- User-facing UI text is Spanish (rioplatense "vos": "elegí", "subí").
  Code, comments, commit messages and docs are English.

## Commands

```bash
uv sync --extra dev          # or: python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
uv run pytest                # tests (some use the real ffmpeg if installed)
uv run ruff format src tests # CI only enforces ruff format
uv run python src/serve.py   # hub on :8000; env OUTPUT_DIR, HUB_PORT, HUB_HOST
```

Local deployment uses `docker-compose.yml` / `.env`, which are git-ignored
copies of the `*.example` files.

## Recording pipeline (no server-side transcode)

- `core/tiktok_recorder.py` downloads TikTok's HTTP-FLV and feeds it to
  `core/flv_stream.py`:
  - `FlvNormalizer` splices reconnects (each is a new FLV stream with its
    own header and unrelated timestamps; guests joining/leaving changes the
    resolution and often triggers one) into a single monotonic timeline.
    Pure Python, tag-level, no decoding.
  - `TsRemuxer` pipes that through `ffmpeg -c copy` into an MPEG-TS file,
    restarting ffmpeg (with cached codec headers) if it dies.
- Recordings finish as `format = 'ts'`. Old ones may be `flv` (pre-fork
  raw) or `mp4` (old server-side transcodes).
- The user converts on their own PC (RTX 4070, NVENC) and uploads the MP4:
  `PUT /recordings/{id}/converted` streams the body to `<name>.mp4.part`,
  validates it with ffprobe (MP4 container, H.264/AV1/VP9), then swaps it
  in and deletes the raw file. Any failure leaves the raw untouched.
- The ffmpeg command shown to the user is built client-side in
  `templates/partials/library_convert.html` (encoder/resolution/quality,
  single file or PowerShell folder loop). Keep the `scale+pad+fps` and
  `aresample=async=1` parts: they are what fix resolution changes and
  splice gaps.

## Web hub

- FastAPI + Jinja2 + htmx, no build step. Routers in `src/web/routers/`.
- Library: `/library` renders `library.html`, `partials/library_content.html`
  (htmx filter/tab changes) or `partials/library_main.html` (htmx
  recording click, `HX-Target: library-main`) so the sidebar keeps its
  scroll position. Library states: raw / ready (mp4) / recording / failed
  (`recordings_repo._STATE_FILTERS`).
- `/creators` and `/creators/{username}` aggregate by `recordings.username`
  (`recordings_repo.creator_summaries`), so creators whose channel was
  deleted still show up. Timestamps in the DB are UTC (`datetime('now')`);
  the `ago` filter renders relative times.
- Creator pictures/display names come for free from the room-id lookup
  (`TikTokAPI.last_user_info`, signed via tikrec.com - the original
  project's own signing service). The recorder emits a `profile` status,
  the worker saves it at most every 6 h (`supervisor/profiles.py`): avatar
  downloaded to `OUTPUT_DIR/avatars/` (TikTok CDN URLs expire), row in the
  `profiles` table, served by `/avatars/{username}`. The `avatar` macro
  shows the initial underneath and drops the <img> on error.
- Guest/co-host names (best-effort, hub mode only): `core/participants.py`
  polls room info every 30 s while recording (`link_mic.rival_anchor_id`
  for a linked co-host, `linked_user_list` / `show_user_list` for
  multi-guest - the latter not yet seen populated in a real capture) and
  turns ids into handles via the `tiktok.com/share/user/<id>` redirect.
  Age-restricted lives need `sessionid_ss` in cookies.json. Any failure
  only drops the names; it must never affect the recording. Time ranges
  go to `recording_participants`; the convert command can burn them in
  with `drawtext` (single-file mode only).
- `src/cookies.json` is marked `git update-index --skip-worktree` on the
  user's machine because it holds their TikTok session. Never commit it.
- Live notices go to a Discord webhook: `notify/discord_webhook.py`. The
  URL lives in hub.db (`settings` table via `db/settings_repo.py`, key
  `discord_webhook_url`), not in a mounted JSON. The per-channel bell is
  the legacy `use_telegram` column (name kept to avoid a migration); the
  worker reads it from the DB each time, and it is intentionally not a
  supervisor restart trigger.
- Supervisor restart triggers are only `username`, `mode`, `proxy`
  (`supervisor._TRACKED_FIELDS`). A restart stops the worker and cuts any
  ongoing recording, so settings that can change mid-live are read live
  instead: the channel's `automatic_interval` (recorder property backed by
  `automatic_interval_getter`), the bell, and the guest poll interval
  (`settings` key `participants_poll_seconds`, 10-600 s, default 30).
- SQLite connections are per-call (`db.database.get_connection`). In
  `async` endpoints open them inside a `run_in_threadpool` function, never
  via the sync `get_db` dependency (different thread).
