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
- Room-id lookup (`TikTokAPI.get_room_id_from_user`, every channel check):
  asks TikTok's user/room API directly WITHOUT a signature first (user
  decision 2026-11-21: stop depending on tikrec.com, the original
  project's signing service, which is often down). `statusCode` 19881007
  = user not found -> None, no fallback. Only when TikTok refuses (WAF
  page, other code, no `roomId`) does it use the old signed route via
  tikrec, then Euler Stream; a warning is logged on the 1st and every 20th
  refusal in a row. The same response fills `last_user_info` (avatar,
  nickname). Checked live on 5 accounts with tikrec blocked.
- Stream URLs (`TikTokAPI.get_live_urls`, from the 2026-11-21 research
  of upstream issues, yt-dlp, streamlink and DouyinLiveRecorder):
  room `status` 2 = live, 3 = paused (still live), 4 = ended
  (`LIVE_ROOM_STATUSES`). Candidates are FLV only (an HLS playlist used to
  end the recording with "not FLV"), ordered by the real quality in each
  variant's `sdk_params` (vbitrate, then resolution, then TikTok's level),
  audio-only `ao` last. Each variant has a `main` and a `backup` CDN URL:
  `stream_info[url]` keeps quality/size/bitrate/codec/cdn and
  `alternate_url()` swaps main<->backup; the stream watchdog reconnects to
  the alternate. The stream request has a (10 s, 30 s) connect/read
  timeout, so a connection that goes silent reconnects instead of hanging
  (it used to only trip the health monitor's stall alert). Each
  connection's variant is logged in the stats file (`sources`).
- Reconnect grace (hub only): when `is_room_alive` says the live is over,
  `TikTokRecorder._wait_for_return` keeps the file open for
  `reconnect_grace_seconds` (Settings "Si el live se corta", default 300 s,
  0-1800; CLI = 0), polling every 15 s with a fresh
  `get_room_id_from_user` (a restarted live has a NEW room id). If the
  creator is back it appends to the same .ts (FlvNormalizer splices it)
  and moves the guest tracker to the new room (`set_room`, errors
  swallowed). Status flag `waiting_return_until` drives the dashboard
  "esperando que vuelva" and makes the health monitor skip stall checks.
  After any live ends the channel is checked every 1 min for 15 min
  (`POST_LIVE_FAST_SECONDS`), overriding the smart interval. Motivation:
  the user's channels produced a new recording every few minutes with
  ~2 min lost per cut (2026-11-12 review).
- Recordings finish as `format = 'ts'`. Old ones may be `flv` (pre-fork
  raw) or `mp4` (old server-side transcodes).
- The user converts on their own PC (RTX 4070, NVENC) and uploads the MP4:
  `PUT /recordings/{id}/converted` streams the body to `<name>.mp4.part`,
  validates it with ffprobe (MP4 container, H.264/AV1/VP9), then swaps it
  in. Any failure leaves the raw untouched.
- Kept originals (user decision 2026-11-20, after an MP4 came out with
  half its audio missing and there was nothing left to check):
  `supervisor/originals.py`. The replaced raw stays where it was, the row
  keeps `original_path` / `original_kept_at`, for `keep_originals_days`
  (Settings "Originales y copias de seguridad", default 7, 0-60, 0 = delete
  at once). Downloadable from the library header (`/media/{id}/original`),
  deleted with the recording. The hourly `Housekeeping` thread deletes
  expired ones, and the oldest ones early while the recordings disk has
  less than `HUB_ORIGINALS_MIN_FREE_GB` (default 30) free.
- Stream stats: `core/stream_stats.py` `StreamStats`, fed by
  `FlvNormalizer(stats=...)` at tag level, saved every 60 s and at the end
  as `<recording>.stats.json` (kept after conversion, deleted with the
  recording): audio/video frames+bytes per minute, gaps (> 2 s on the video
  timeline, `null` end = until the end), connections with wall time,
  codec config changes, ffmpeg restarts. A muted mic still sends silent
  AAC frames, so a gap means TikTok sent no audio; silence shows as low
  audio bytes per minute. The library header warns "sin audio/video al
  grabar" and links the JSON ("diagnóstico"). Ask the user for it when a
  recording looks broken.
- Stream watchdog (`core/stream_watchdog.py`, built 2026-11-20): after
  each chunk it reads the StreamStats counts; audio missing for 45 s of
  video (video missing: 120 s, since a paused live also stops video) while
  the other stream flows -> the recorder drops the connection and the
  normal reconnect path opens a new one into the same file. Retries after
  120 s / 300 s / 300 s more of video, then gives up (keeps recording,
  Discord problem alert `stream_missing:<recording>:<kind>`). Status flag
  `stream_missing` shows "⚠ sin audio" on the dashboard from the first
  attempt until it's back. Attempts are logged in the stats file
  (`watchdog`). A muted mic still sends frames, so it never trips.
  `download_live_stream` closes the HTTP response when the loop breaks.
- Daily `hub.db` backup (`supervisor/backups.py`, SQLite backup API) to
  `HUB_BACKUP_DIR` (default `OUTPUT_DIR/backups`, same disk) as
  `hub-YYYY-MM-DD.db` (UTC date), newest 14 kept.
- The ffmpeg command is built in one place, `web/convert.py`
  (encoder/resolution/quality presets, file or PowerShell folder loop).
  The library page fetches it from `/api/convert/command`; the auto-convert
  script fetches `/api/convert/args` (with `{input}`/`{output}`
  placeholders). Never rebuild it client-side again. Keep the
  `scale+pad+fps` filter and `aresample=async=1`: they are what fix
  resolution changes and splice gaps.
- Converting is ON DEMAND with one-line commands (user decision
  2026-11-13: no scheduled/automatic conversion - don't bring back Task
  Scheduler or Settings install steps). The library shows a batch bar
  ("N por convertir", encoder/size/quality selects, remembered in
  localStorage `convert-cmd-options`) and, on each raw recording, a
  command for just that one (`-Id <id>`); manual download/ffmpeg/upload
  steps stay folded under "Hacerlo a mano". The command is
  `& ([scriptblock]::Create((irm "<hub>/tools/convert-pending.ps1?inline=1"))) -Hub <hub> ...`
  so nothing is installed and the newest script always runs. The raw
  panel polls `/api/recordings/{id}` every 5 s and swaps to the player
  when it becomes mp4.
- `src/web/tools/convert-pending.ps1` (Windows PowerShell 5.1, needs ffmpeg
  + built-in curl.exe): served with `__HUB_URL__` replaced (placeholder
  check split in two strings); `?inline=1` = no BOM (a BOM breaks
  `[scriptblock]::Create`), plain download = with BOM. All logic is in
  `Invoke-HubConvert`, which RETURNS an exit code: `exit` from a script
  block would close the user's console, so it only exits when run as a
  file (`$PSCommandPath`). Lists `/api/recordings?state=raw` oldest first
  (filtered by `-Id`), re-checks each is still raw, downloads (resumable),
  converts with `/api/convert/args`, uploads via
  `PUT /recordings/{id}/converted`, deletes local files; one failure
  doesn't stop the rest; a lock file prevents parallel runs. Messages are
  ASCII on purpose (PS 5.1 encoding). Default work dir
  `%USERPROFILE%\TikTokConvert`.
  The user's hub is behind an HTTPS reverse proxy (https://tiktok.pclo.net):
  the script's hub URL comes from `api.public_base_url` (HUB_PUBLIC_URL,
  else X-Forwarded-Proto/Host). A first version baked in http://, and the
  proxy's 301 on the upload PUT was taken as success and the local files
  deleted. The script now follows a redirecting hub URL up front (curl
  `%{redirect_url}`), requires HTTP 200 on upload, and re-checks
  `/api/recordings/{id}` is mp4 before deleting anything local.

## Web hub

- FastAPI + Jinja2 + htmx, no build step. Routers in `src/web/routers/`.
- Library layout is video-first (user request 2026-11-13): full width
  (`container-wide` block in base.html), title + compact stats + tabs in
  one row, a one-line convert bar, and for MP4s a two-column `.rec-body`
  (video `.rec-media`, sticky; guests/clip tools/marks in `.rec-side`)
  from 1250 px, stacked below. A script in library.html sizes the video to
  the room left below it (`fit`) so it and the "En pantalla" bar fit
  without scrolling. On phones the player comes before the list.
  `partials/guests_panel.html` is shared by the player side column and
  the raw/recording views.
- Player stalls (diagnosed 2026-11-20 on the user's hub): a `<video>`
  removed by an htmx swap kept downloading until GC, and after ~4
  recording switches the new player hung until F5. library.html
  `releaseVideos` empties the source + `load()` on `htmx:beforeSwap` and
  `pagehide`. Server side was fine (Range 206, ~110 MB/s, faststart, GOP
  2 s, h2 at the proxy); seeking in a 2.6 h MP4 still takes ~1-1.5 s in
  the browser.
- Hub froze on "Too many open files" (2026-11-21, ulimit 1024 in the
  container): Starlette 1.6.0's `FileResponse` ignored client disconnects
  (uvicorn sends ASGI spec 2.3), so every range request the browser
  abandoned on a seek kept reading the MP4 to the end - on the NAS HDD
  hundreds piled up (each holding the MP4 + hub.db + hub.db-wal fds).
  That also explains the earlier "3 MB/s" buffering. Fixed by Starlette
  1.10 in uv.lock (test: `tests/web/test_media_disconnect.py`, fails on
  1.6.0), streaming endpoints read the row with `_lookup` instead of
  holding `Depends(get_db)` for the whole response, the health monitor
  alerts on Discord at 80 % of the fd limit. The default 1024 limit is
  kept on purpose (user decision 2026-11-21): normal use needs a few dozen,
  and a low limit makes the 80 % alert catch a future leak early. After
  the fix playback was smooth again. The 60 s restart was
  `stop_grace_period`.
- Custom player controls (user chose own controls over Plyr, 2026-11-20):
  `static/player.js` `initPlayer(#player)`, called from
  library_player.html on every render (tears down the previous instance;
  native `controls` stay in the HTML as the no-JS fallback). Seek bar with
  buffered range, saved clips (`.mark[data-start/end]`, redrawn on
  `htmx:afterSettle`), the clip being cut (`window.playerSetSelection`,
  fed by the noUiSlider) and a guest lane; hover/drag shows a storyboard
  thumbnail + time + who was on screen, and dragging seeks only on
  release. -10/-1/+1/+10, frame steps (1/30 s), speed, volume (speed and
  volume in localStorage `player-prefs`), fullscreen on the wrapper.
  Keys: space/K, J/L ±10, ←/→ ±5 (Shift ±1), `,`/`.` frames, 0-9 %, M,
  F; I/O stay the clip editor's. Self-recovery: stuck loading 8 s while
  playing or a source error → `load()` and resume at the same time and
  play state, backoff, after 6 tries a "tocá para reintentar" notice.
- Storyboards (seek-bar thumbnails): `supervisor/storyboards.py`,
  `<video>.storyboard/` next to the MP4 (`sheet_NNN.jpg` 10x10 grids,
  tiles 160 px high, one every `max(2, ceil(duration/1000))` s, plus
  `meta.json`). Made on the user's PC while converting (user decision
  2026-11-20, to spare the hub's NAS HDD from re-reading every MP4): the
  script asks `/api/convert/args?recording_id=N`, whose `storyboard.args`
  is a second, separate ffmpeg command run on the FINISHED MP4
  (`storyboards.ffmpeg_args`, keyframes only, < 1 s for 20 min of AV1;
  interval from the recording's duration), then after the MP4 upload zips
  the sheets and sends
  `PUT /recordings/{id}/storyboard?interval=N` (`install_uploaded`
  validates names, JPEG magic, sizes, consecutive sheets; failure is only
  a warning in the script). They were first a second output of the
  conversion itself; that drifted minutes off on recording 42 (fixed
  2026-11-20): TikTok's audio jumped 21.5 s ahead at ~696 s, ffmpeg padded
  the MP4 (+33 s vs the raw) and a filter reinit dropped a half-filled
  sheet, so tiles followed the raw's timeline, not the MP4's.
  `-reinit_filter 0` fixes the tiles but breaks real resolution changes
  (the scale/pad stop adapting) - don't use it. Thumbnails must always be
  made from the MP4. Fallback: `StoryboardWorker` thread (serve.py,
  `app.state.storyboards`) builds missing ones for MP4s older than
  `UPLOAD_GRACE_SECONDS` (15 min, so the PC's upload wins), newest first,
  keyframes only, `nice`, rescans every 10 min. Served by `/media/{id}/storyboard`
  (JSON, 404 until built) and `/media/{id}/storyboard/{n}.jpg`; deleted
  with the recording.
- Library: `/library` renders `library.html`, `partials/library_content.html`
  (htmx filter/tab changes) or `partials/library_main.html` (htmx
  recording click, `HX-Target: library-main`) so the sidebar keeps its
  scroll position. Library states: raw / ready (mp4) / recording / failed
  (`recordings_repo._STATE_FILTERS`).
- Clips = saved clip marks (`clip_marks`). Library filter `clips=with|without`
  (old `no_clips=1` still maps to without); items show a "✂ N" pill. The
  "Sin clips" view offers a bulk delete (user request 2026-11-13, "doble
  check"): a button, then an inline "Sí, eliminar N" confirmation, then
  `POST /recordings/delete-without-clips` re-checks every id server-side
  (still a finished MP4, still zero marks) and keeps anything that
  changed. Raw and recording rows are listed but never bulk-deleted.
- `/creators` and `/creators/{username}` aggregate by `recordings.username`
  (`recordings_repo.creator_summaries`), so creators whose channel was
  deleted still show up. Timestamps in the DB are UTC (`datetime('now')`);
  the `ago` filter renders relative times. Absolute dates are localized
  in the browser by a script in base.html: `.rec-list` items carry
  `data-started` and get regrouped under local-day headers ("Hoy",
  "Ayer", "mié 12 nov"), `[data-utc-date]` elements and `title="... UTC"`
  tooltips are rewritten; it re-runs on `htmx:afterSettle`.
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
  Times are seconds of VIDEO, not wall clock (fixed 2026-11-20): the
  recorder hands the tracker `TsRemuxer.media_seconds` (FlvNormalizer's
  output timeline), because a cut spliced by the reconnect grace leaves
  no gap in the video and wall-clock times drifted later after every cut.
  `recording_finished` also carries `media_seconds`, stored as the
  recording's duration. Old recordings are fixed once at startup by
  `supervisor/guest_times.py`: TikTok's H.264 SEI carries the capture
  time (`"ts":<epoch ms>`), so sampling it along a raw .ts shows where
  the cuts are and how long each took (checked on recording 39: offset
  steady per segment, 4 cuts, ~6.7 min lost). Converted MP4s lost the SEI
  and can't be located; they are only counted in the log. Settings keys:
  `guest_times_video_clock_since` (first boot of the fix - only older
  recordings are touched) and `guest_times_fixed` (ids done).
  Someone counts as gone only after 2 consecutive missing polls, and a
  return within 120 s continues the same row (TikTok's data flickers).
  Every time the set of linked ids changes, the raw link data is appended
  to `<recording>.participants.jsonl` next to the recording (downloadable
  from the guests panel) - ask the user for it when a list looks wrong.
  With 3+ linked creators `rival_anchor_id` rotates among them (confirmed
  2026-11-20), so since then the VIDEO gives the times (user chose this
  "video + sala" approach 2026-11-20, on trial for a few days):
  `core/layout.py` `LayoutTracker` reads TikTok's mix-layout SEI
  (`{"app_data": "{\"grids\":[...], \"anchor_link_mic_id\": ...}"}`, ~1/s)
  from every video frame via `FlvNormalizer(on_video_frame=...)` (byte
  search, at most one JSON parse per second of video). Each grid except
  the creator's (`anchor_link_mic_id`) is a guest panel. A panel missing 5 s
  is gone; no layout for 10 s = solo again. Names (found and shipped
  2026-11-21): a panel's `cid` is the room id of that person's OWN live, so
  each poll first asks room info for every new cid's owner
  (`TikTokAPI.get_room_owner`, once per panel, max 8 per poll, cached per
  cid so a returning guest keeps the name) - checked on recording 43's
  saved cids and live on a 4-person co-host, where it named all 3 guests
  while room info showed only 1. Only when that lookup fails (age-
  restricted room without a working session; viewer guests without a live
  of their own - untested) do room-info polls name the panel: a name not
  on screen yet goes to the NEWEST unnamed panel (`LayoutTracker.name`),
  and an exact cid name later replaces such a guess. Panels keep their
  box (x/y/w/h); `.participants.jsonl` "panels" lines log
  {cid: {name, by, box}}.
  Old recordings (panels saved, names from the rotating polls) are
  renamed once at startup by `supervisor/guest_names.py`: each row gets
  the cid that newly appeared in the first "panels" line at/after its
  start (several at once: the one that left when the row ended - the old
  names can't decide, they're what was wrong), then its room's owner.
  Times untouched; refused rooms keep the old name; Settings key
  `guest_names_by_cid_done`. Dry run on recording 43 found swapped and
  wrong names (e.g. a "flt65" row was miranda_fernandezzz).
  Once a recording has seen any layout, the snapshot comes from the panels
  (unnamed ones saved with username "" and shown as "Sin identificar",
  excluded from counts, list pills and dashboard); without layout the old
  poll-only logic applies. Panels are also logged to `.participants.jsonl`
  (`"panels"` lines). Unknown: whether 2-person co-hosts and multi-guest
  (viewers) lives carry the same SEI - only a 4-creator co-host was seen.
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
- Live schedules (`core/schedule.py` pure math, `supervisor/schedules.py`
  DB loading + 5 min cache, invalidated on live start/end). Agreed rules,
  driven by the user's case of a night creator who once streamed at noon:
  - A learned schedule only changes how OFTEN a channel is checked, never
    whether. Smart interval is opt-in per channel (`channels.smart_interval`,
    ⚡ on the dashboard): usual hours = half the channel interval (min
    1 min), other hours = double (max 15 min, never faster than set),
    configured interval unchanged while learning.
  - Learned after 5 lives over 7 days. Probability per hour of the week =
    weighted share of days live at that hour (half-life 30 days, ±1 h at
    half credit so checks speed up just before the usual start). "Usual"
    = >= 30 %, or >= half of the creator's own peak hour (floor 8 %) -
    added 2026-11-12 because creators who don't stream daily (peak 17 %)
    never reached 30 % and ⚡ only ever slowed them down
    (`schedule.usual_threshold`, stored as `threshold` in the result). Only complete days count (a live today counts from
    tomorrow). Per-weekday values (after 3 weeks of every weekday) are
    shrunk toward the pooled hour-of-day pattern (prior of 2 days) and
    the max of both is used - one odd Friday can't make an hour usual.
  - Bins are UTC; the creator page heatmap/summary shift to the viewer's
    local time in JS. The library marks recordings "fuera de horario".
- `db/database.py` `_migrate` adds columns introduced after a table was
  first created (`_ADDED_COLUMNS`); add new columns there too, not only in
  schema.sql, or existing installs break.
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
  accepted and swapped in (the raw is now kept for `keep_originals_days`,
  so it's recoverable for a while, but nothing warns).
  - [ ] Server: ffprobe raw vs upload and reject (raw untouched) if the
    durations differ by more than `max(10 s, 2 %)`.
  - [ ] Browser: if the dropped file isn't named `<raw stem>.mp4`, ask for
    confirmation before uploading (warning, not a block).
- **A/V timestamp jump in raw recordings** (found 2026-11-20 on
  recording 42, not yet investigated): at ~696 s the raw's audio
  timestamps jump 21.5 s ahead of the video and then wobble backwards
  ~0.13 s per packet; the converted MP4 gets ~99 s of near-silence
  (695-794 s) and ends 33 s longer than the raw. So the MP4 timeline can
  differ from the video clock that guest times use. Check whether it
  comes from TikTok or from `FlvNormalizer`'s discontinuity handling
  (a lone audio jump > `_MAX_FORWARD_MS` resets the shared offset); the
  new `.stats.json` will show the next one.
- **Conversion checks** (discussed 2026-11-20, after real stats data):
  on upload, compare audio vs video stream duration and the raw's
  duration; measure long silences on the PC during conversion
  (`aresample=async=1` pads gaps with silence, so a "full" audio track can
  be mute) and compare with the raw's stats.
- **Guest source for 3+ linked creators** - solved 2026-11-21 by naming
  panels by cid (see "Web hub"). Next possible steps: show who is where
  (panel boxes are stored in memory/debug, not in the DB yet); "appearances"
  on creator pages; re-name old "Sin identificar" rows from the cids in
  their `.participants.jsonl` - shipped 2026-11-21. History, checked on
  the hub 2026-11-20:
  - Room info never lists more than one id; `linked_user_list` /
    `show_user_list` are always empty. In a 4-panel co-host
    (`link_mic.channel_info.layout = 4`, recording 39) `rival_anchor_id`
    rotates among the 3-4 creators every 30-90 s, so the guests panel
    shows people "coming and going" who were all on screen.
  - The raw video carries the mix layout in H.264 SEI (per frame):
    `{"app_data":"{\"grids\":[{id,x,y,w,h,uid_str,cid,talk,...}]}"}`
    (normalized panel boxes), `{"infos":[{uid,talk,volume}]}` (who is
    speaking, several times a second) and `anchor_linkmic_id` (the
    creator's own panel). (Corrected 2026-11-21: `cid` is the guest's own
    room id, `uid_str` = "<cid>_<n>_0" - so SEI does give who.)
  - Websocket sources (TikTokLive / TikTok-Live-Connector) declined by
    the user 2026-11-20: they depend on Euler Stream signing. Don't
    propose them again.
  - Built 2026-11-20 as SEI panels + names from the rotating polls (see
    "Web hub"); the user is trying it for a few days. Next possible step:
    the panel's position (x/y/w/h is in the SEI) to show who is where.

### Research leads (2026-11-21; the guests topic led to cid naming)

- `https://www.tiktok.com/api-live/user/room?aid=1988&sourceType=54&uniqueId=<user>`
  answers WITHOUT signing (streamlink uses it; checked 2026-11-21): roomId,
  status, stream data, `hevcStreamData` (H.265 variants), and
  `multiStreamScene` / `multiStreamSource` (meaning unknown - log them
  during a live with guests). Shipped 2026-11-21 as the primary room-id
  lookup (see "Recording pipeline").
- yt-dlp: when a user is a guest on someone else's live, their own room has
  no HLS URLs; it falls back to `https://www.tiktok.com/api/live/detail/?aid=1988&roomID=`.
- The SEI mix layout is the standard RTC "layout SEI" (Alibaba Cloud /
  Agora document the same uid + x/y/w/h + volume pattern); TikTok's comes
  from ByteDance's Volcano Engine RTC. No public doc of `cid` found. The
  panel->user list only travels on the signed webcast websocket (declined).
- Upstream #439 reports audio drifting out of sync "when the live lags" -
  the same TikTok behaviour as recording 42; no fix there either.

### Findings from the live UI review (2026-11-12, not yet chosen)

Observed on the user's hub (https://tiktok.pclo.net) with real data:

1. ~~**A live is split into many recordings**~~ - shipped 2026-11-12
   (reconnect grace + post-live fast checks, see "Recording pipeline").
   Original finding: Today
   @miguelrv777 produced 7 recordings 2 min apart (the 2 min = smart
   interval "unusual" = double of 1 min); ~2 min of video lost per cut,
   and the library/lives counts/schedule get inflated. Causes: the creator
   ends and restarts a few minutes later (user says this is very common),
   or TikTok briefly reports the room as not alive/paused
   (`is_room_alive` requires status 2; upstream `develop` has "handle
   paused and HLS live streams"). Idea: a grace window after a live ends -
   keep the recording open, check fast (~15-20 s) for N minutes (default
   ~5), re-resolve the room id, and append to the same .ts if they come
   back; plus a fast-check window after a live ends regardless of the smart
   interval. Optionally group consecutive recordings as one session in UI.
2. ~~**Smart interval never sees "usual" hours for creators who don't
   stream daily**~~ - shipped 2026-11-12 (relative threshold). Original: @miguelrv777's peak is 17 % (< 30 % fixed threshold), so the
   page says "Sin un horario fijo" despite a clear 18-24 h pattern and ⚡
   always slows checks. Idea: relative threshold (e.g. usual = p >= 50 %
   of the creator's own peak and >= ~8 %).
3. ~~**Library date headers use the UTC date**~~ - shipped 2026-11-12.
   Original: (shows "2026-11-13" for
   evening lives of Nov 12 local). Group by the viewer's local date.
4. ~~**Dashboard unusable on phones**~~ - shipped 2026-11-12 (cards
   under 700 px). Original:: the table overflows, status and
   buttons are off-screen. Needs a card layout under ~700 px.
5. ~~**"Eliminar" looks the same as "Pausar"**~~ - shipped 2026-11-12.
   Original: on the dashboard (blue): make
   it a danger button, it sits right next to Pausar.
6. ~~Smart-interval hint shown while recording~~ - hidden, 2026-11-12.
7. ~~Guests panel lists the same person on several rows~~ - shipped
   2026-11-12 (one row per person, each range clickable). Original: when they leave and
   come back (e.g. @julyyrg22 twice); group rows per person.
8. Session: the hub flagged "TikTok rechazó la sesión" on @miguelrv777 -
   the user should paste a fresh `sessionid_ss`.

### Declined by the user

- Merging existing split recordings into one (proposed 2026-11-12): the
  user doesn't want it. Don't propose it again; the reconnect grace window
  prevents new splits instead.
- Web UI login: hub is LAN-only (see idea 2 below).
- Scheduled/automatic conversion (nightly Task Scheduler): the user
  prefers running the one-line command when they want (2026-11-13).

### Ideas proposed, not yet chosen (2026-11-06)

Ranked by usefulness for how the user works (24/7 recording, converting on
their own PC, reviewing in the web UI):

1. ~~Auto-convert script~~ - shipped 2026-11-06, reworked 2026-11-13 into
   on-demand one-line commands (see "Recording pipeline").
2. ~~Web UI login~~ - declined 2026-11-12: the hub is only reachable on
   the user's LAN (tiktok.pclo.net resolves internally). Don't propose it
   again unless that changes. (If ever built: the auto-convert script and
   its `/api/*`, `/media/*/download`, `/recordings/*/converted` calls must
   authenticate too.)
3. Disk care: Discord alert below a free-space threshold; optional cleanup
   of old raws / recordings older than N days, per channel.
4. ~~Problem alerts on Discord~~ - shipped 2026-11-06.
5. ~~Dashboard live info~~ - shipped 2026-11-06.
6. "Appearances" on creator pages: lives of others where they were a guest.
7. ~~Usual live hours per creator + smart interval~~ - shipped
   2026-11-06.

