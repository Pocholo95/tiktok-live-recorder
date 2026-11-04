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
- Live notices go to a Discord webhook: `notify/discord_webhook.py`. The
  URL lives in hub.db (`settings` table via `db/settings_repo.py`, key
  `discord_webhook_url`), not in a mounted JSON. The per-channel bell is
  the legacy `use_telegram` column (name kept to avoid a migration); the
  worker reads it from the DB each time, and it is intentionally not a
  supervisor restart trigger.
- SQLite connections are per-call (`db.database.get_connection`). In
  `async` endpoints open them inside a `run_in_threadpool` function, never
  via the sync `get_db` dependency (different thread).
