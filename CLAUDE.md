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
- This file is the project's memory. Record here every decision, pending
  item and idea the user agrees on (see "Backlog" at the end) instead of
  relying on conversation history, and keep it current as work lands.

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
- The ffmpeg command is built in one place, `web/convert.py`
  (encoder/resolution/quality presets, file or PowerShell folder loop).
  The library page fetches it from `/api/convert/command`; the auto-convert
  script fetches `/api/convert/args` (with `{input}`/`{output}`
  placeholders). Never rebuild it client-side again. Keep the
  `scale+pad+fps` filter and `aresample=async=1`: they are what fix
  resolution changes and splice gaps.
- Auto-convert: `src/web/tools/convert-pending.ps1` (Windows PowerShell 5.1,
  only needs ffmpeg + the built-in curl.exe). Served by
  `/tools/convert-pending.ps1` with `__HUB_URL__` replaced by the hub's
  address (the script's own placeholder check is split in two strings so
  the substitution can't rewrite it). It lists `/api/recordings?state=raw`
  oldest first, re-checks each one is still raw, downloads (resumable),
  converts, uploads via the normal `PUT /recordings/{id}/converted`, and
  deletes the local files; one failure doesn't stop the rest, and a lock
  file prevents overlapping runs. Settings shows install/run/schedule
  commands (Task Scheduler via `Register-ScheduledTask`). Script messages
  are ASCII on purpose (PS 5.1 encoding). Known gap: a corrupt raw fails
  again on every run (logged, harmless).

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
  Someone counts as gone only after 2 consecutive missing polls, and a
  return within 120 s continues the same row (TikTok's data flickers).
  Every time the set of linked ids changes, the raw link data is appended
  to `<recording>.participants.jsonl` next to the recording (downloadable
  from the guests panel) - ask the user for it when a list looks wrong.
  Open question: a user saw two different co-hosts alternate while both
  were likely on screen; `rival_anchor_id` may only name the current
  battle opponent in 3+ creator links. Confirm with a diagnostics file
  before switching sources (battle_scores / TikTokLive websocket).
  Age-restricted lives need `sessionid_ss` in cookies.json. Any failure
  only drops the names; it must never affect the recording. Time ranges
  go to `recording_participants` and are shown only in the web UI (the
  "Con quién estuvo" panel and the "En pantalla" bar under the player,
  synced to playback). The user decided against burning names into the
  MP4: TikTok's layout changes made fixed positions land on the video or
  the black bars unpredictably, and per-panel placement isn't knowable.
- `src/cookies.json` is marked `git update-index --skip-worktree` on the
  user's machine because it holds their TikTok session. Never commit it.
- Live notices go to a Discord webhook: `notify/discord_webhook.py`. The
  URL lives in hub.db (`settings` table via `db/settings_repo.py`, key
  `discord_webhook_url`), not in a mounted JSON. The per-channel bell is
  the legacy `use_telegram` column (name kept to avoid a migration); the
  worker reads it from the DB each time, and it is intentionally not a
  supervisor restart trigger.
- Problem alerts (`supervisor/alerts.py`, same Discord webhook, toggle
  `problem_alerts_enabled`, default on; each kind throttled via
  `settings` keys `alert_last:<kind>`):
  - TikTok session rejected: there is NO reliable way to ask TikTok whether
    `sessionid_ss` is valid (`/passport/web/account/info`,
    `/api/user/info`, `/webcast/user/me` answer the same for a real, fake
    or missing session - checked 2026-11-06). It's detected by symptom
    instead: room info returns 4003110 while a session is configured
    (`ParticipantTracker.on_access_denied` -> recorder `session_problem`
    status -> worker). Shown in Settings under Cookies; cleared when a new
    session is saved. Without a session the alert is "age-restricted live,
    configure a session" instead.
  - `supervisor/health.py` (thread started in serve.py, every 60 s):
    channel in "error" for 30+ min (one alert per episode, keyed by the
    status `since`), recording file not growing for 5+ min (sets status
    flag `stalled_since`, shown on the dashboard). Observe-only.
  - `ChannelStatusStore` statuses carry `since` (when the phase started,
    kept across same-phase sets) and `flags` (reset on phase change).
- Dashboard rows of recording channels show elapsed time, current file
  size, guests linked right now and the stall warning (`_live_info` in
  pages.py), refreshed by the existing 3 s polling.
- Supervisor restart triggers are only `username`, `mode`, `proxy`
  (`supervisor._TRACKED_FIELDS`). A restart stops the worker and cuts any
  ongoing recording, so settings that can change mid-live are read live
  instead: the channel's `automatic_interval` (recorder property backed by
  `automatic_interval_getter`), the bell, and the guest poll interval
  (`settings` key `participants_poll_seconds`, 10-600 s, default 30).
- SQLite connections are per-call (`db.database.get_connection`). In
  `async` endpoints open them inside a `run_in_threadpool` function, never
  via the sync `get_db` dependency (different thread).

## Backlog

Agreed but not built yet. Move items out of here when they ship.

### Pending (user asked to note them)

- **Upload safety.** `PUT /recordings/{id}/converted` only checks that the
  file is a browser-playable MP4; the MP4 of a *different* live is
  accepted, swapped in, and the raw `.ts` is deleted (unrecoverable).
  - [ ] Server: ffprobe raw vs upload and reject (raw untouched) if the
    durations differ by more than `max(10 s, 2 %)`.
  - [ ] Browser: if the dropped file isn't named `<raw stem>.mp4`, ask for
    confirmation before uploading (warning, not a block).
- **Guest source for 3+ linked creators** (see the open question in "Web
  hub"). Needs a `.participants.jsonl` from such a live first. TikTokLive
  risks already discussed with the user: no account risk if it connects
  anonymously (never pass the session), some IP rate-limit risk shared
  with the recorder, depends on Euler Stream signing, protocol churn.

### Ideas proposed, not yet chosen (2026-11-06)

Ranked by usefulness for how the user works (24/7 recording, converting on
their own PC, reviewing in the web UI):

1. ~~Auto-convert script~~ - shipped 2026-11-06 (see "Recording
   pipeline").
2. Web UI login (user/password from `.env`): the hub now stores the TikTok
   session and the Discord webhook and has no auth at all. When built, the
   auto-convert script and its `/api/*` + `/media/*/download` +
   `/recordings/*/converted` calls must authenticate too (e.g. a token
   param baked into the downloaded script), or nightly runs break.
3. Disk care: Discord alert below a free-space threshold; optional cleanup
   of old raws / recordings older than N days, per channel.
4. ~~Problem alerts on Discord~~ - shipped 2026-11-06.
5. ~~Dashboard live info~~ - shipped 2026-11-06.
6. "Appearances" on creator pages: lives of others where they were a guest.
7. Usual live hours per creator (when they tend to go live).

