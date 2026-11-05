<div align="center">

# TikTok Live Recorder 🎥

_TikTok Live Recorder is a tool for recording live streaming TikTok._

[![Telegram](https://img.shields.io/badge/Telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://telegram.me/tiktokliverecorder)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
[![Licence](https://img.shields.io/github/license/Ileriayo/markdown-badges?style=for-the-badge)](./LICENSE)
[![Stars](https://img.shields.io/github/stars/Michele0303/tiktok-live-recorder?style=for-the-badge)](https://github.com/Michele0303/tiktok-live-recorder/stargazers)
[![Release](https://img.shields.io/github/v/release/Michele0303/tiktok-live-recorder?style=for-the-badge)](https://github.com/Michele0303/tiktok-live-recorder/releases/latest)
[![Docker Pulls](https://img.shields.io/docker/pulls/michele0303/tiktok-live-recorder?style=for-the-badge&logo=docker&logoColor=white)](https://hub.docker.com/r/michele0303/tiktok-live-recorder)

The TikTok Live Recorder is a tool designed to easily capture and save live streaming sessions from TikTok. It records both audio and video, allowing users to revisit and preserve engaging live content for later enjoyment and analysis. It's a valuable resource for creators, researchers, and anyone who wants to capture memorable moments from TikTok live streams.

![preview](https://i.ibb.co/YTHp5DT/image.png)

</div>

## Table of Contents

- [Installation](#installation)
- [Usage](#command-line-usage)
- [Guide](#guide)

## Installation

**Prerequisites:** [Git](https://git-scm.com), [Python 3.11+](https://www.python.org/downloads/), [FFmpeg](https://ffmpeg.org/download.html)

<details>
<summary>Windows 💻</summary>

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
git clone https://github.com/Michele0303/tiktok-live-recorder
cd tiktok-live-recorder
uv venv
uv sync
uv run python src/main.py -h
```

</details>

<details>
<summary>Linux 🐧</summary>

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/Michele0303/tiktok-live-recorder
cd tiktok-live-recorder
uv venv
uv sync
uv run python src/main.py -h
```

</details>

<details>
<summary>macOS 🍎</summary>

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
brew install ffmpeg
git clone https://github.com/Michele0303/tiktok-live-recorder
cd tiktok-live-recorder
uv venv
uv sync
uv run python src/main.py -h
```

</details>

<details>
<summary>Android — Termux 📱</summary>

Install Termux from [F-Droid](https://f-droid.org/packages/com.termux/) (avoid the Play Store version).

```bash
pkg update && pkg upgrade
pkg install git ffmpeg uv tur-repo
pkg uninstall python
pkg install python3.11
git clone https://github.com/Michele0303/tiktok-live-recorder
cd tiktok-live-recorder
uv venv
uv sync
uv run python src/main.py -h
```

</details>

<details>
<summary>Docker 🐳</summary>

> This fork adds fixes for corrupted recordings (reconnects and resolution
> changes are spliced into one continuous MPEG-TS file instead of a broken
> FLV; `docker stop` is also handled gracefully) and the WebUI hub below. Build the
> image from this repo to get them - the published
> `michele0303/tiktok-live-recorder:latest` image does not include them.

```bash
docker build -t tiktok-live-recorder .

docker run \
  -v ./output:/output \
  tiktok-live-recorder \
  -output /output \
  -user <username>
```

</details>

## WebUI Hub

Instead of fixing the recorded channels at container startup, `serve` mode
runs a small web dashboard to add/remove channels on the fly, see live
status per channel, browse/play past recordings filtered by channel or
date, and cut/download clips from a recording (generated on the fly and
streamed straight to the browser - nothing is written to disk; only the
in/out times are saved server-side if you bookmark them).

### Recording format and the convert-on-your-PC workflow

The server never transcodes. TikTok's live FLV drops and reconnects
regularly (most often when a guest joins or leaves and the resolution
changes); each reconnect is a brand-new FLV stream that most tools discard
after the first splice. The recorder normalizes those splices in Python
(one continuous timeline, see `src/core/flv_stream.py`) and remuxes them
with `ffmpeg -c copy` into **MPEG-TS** - near-zero CPU, every frame kept,
and the file stays readable even if the process dies mid-write.

Browsers can't play `.ts`, so the library walks you through:

1. **Download the raw** `.ts` from the recording's page.
2. **Convert it on your PC** with the ffmpeg command shown there (NVIDIA
   H.264/AV1 or CPU; one file or a whole folder in PowerShell). It scales
   every resolution to one fixed size, pads the rest, and rebuilds
   timestamps.
3. **Upload the `.mp4`** (drag & drop, or `curl.exe -T file.mp4
   http://<hub>/recordings/<id>/converted`). It's checked with ffprobe and
   only then replaces the raw file, which is deleted. The clip editor is
   enabled from then on.

Creator profile pictures and display names are picked up from the same
TikTok lookup the recorder already does on every check (no extra
requests), saved under `OUTPUT_DIR/avatars/` and refreshed every few hours.
They appear after a channel's first check.

### Discord live notices

Settings → *Avisos por Discord*: paste a Discord webhook URL (Server
settings → Integrations → Webhooks), then turn on the 🔔 bell of each
channel on the dashboard. You get a message when the live starts, when it
ends (duration, size) and if the recording fails. The URL is stored in
`hub.db`, so it survives rebuilds without mounting anything extra. Set
`HUB_PUBLIC_URL` to add an "open in the hub" link to each message.

If you put the hub behind a reverse proxy, raise its upload size limit
(e.g. nginx `client_max_body_size 0;`) - converted lives are large.

The repo ships `docker-compose.example.yml` and `.env.example` instead of
the real files, so your own config never conflicts with `git pull`. Copy
them once and edit your copies (both are git-ignored):

```bash
cp docker-compose.example.yml docker-compose.yml
cp .env.example .env
docker compose up
```

or directly with `docker run`:

```bash
docker run -d \
  --name tiktok-hub \
  --restart unless-stopped \
  --stop-timeout 60 \
  --cpus="1.5" \
  -p 8000:8000 \
  -v ./output:/output \
  -v ./src/cookies.json:/app/cookies.json \
  -v ./src/telegram.json:/app/telegram.json \
  tiktok-live-recorder serve
```

Then open `http://localhost:8000`. Mount `cookies.json`/`telegram.json` as
shown above (not `:ro` - the `/settings` page writes to them when you save
credentials from the browser) so edits survive a container restart (see
[How to set cookies](docs/GUIDE.md#how-to-set-cookies)).

`--stop-timeout 60` (or `stop_grace_period: 60s` in compose) gives
recordings in progress time to flush and close their `.ts` file before
`docker stop` escalates to `SIGKILL`.

`--cpus="1.5"` (or `cpus: "${HUB_CPUS:-1.5}"` in compose, overridable with
`HUB_CPUS=3 docker compose up` or a `.env` file) caps how much CPU the
container can use. Recording is only a remux, so this rarely matters.

### Environment variables (`serve` mode only)

| Variable | Default | Description |
|---|---|---|
| `OUTPUT_DIR` | `/output` | Where recordings, thumbnails, and the hub's SQLite DB (`hub.db`) are stored. |
| `HUB_DB_PATH` | `$OUTPUT_DIR/hub.db` | Override the DB file location. |
| `HUB_HOST` | `0.0.0.0` | Interface the web server binds to. |
| `HUB_PORT` | `8000` | Port the web server listens on. |
| `HUB_RECONCILE_INTERVAL` | `5` | Seconds between checks for channel add/remove/edit. |
| `HUB_PUBLIC_URL` | - | Base URL of the hub (e.g. `http://192.168.1.10:8000`) used for links in Discord notices. |
| `HUB_CPUS` | `1.5` | compose-only: CPU limit passed to `cpus:` (see above). |

## Command-Line Usage

```bash
uv run python src/main.py [options]
```

### Options

| Flag | Description |
|------|-------------|
| `-user <USERNAME>` | Username(s) to record. Separate multiple with commas. |
| `-url <URL>` | TikTok live URL to record from. |
| `-room_id <ROOM_ID>` | Room ID to record from. |
| `-mode <MODE>` | Recording mode: `manual`, `automatic`, `followers`. |
| `-automatic_interval <MIN>` | Polling interval in minutes (automatic mode only). |
| `-output <DIRECTORY>` | Directory where recordings will be saved. |
| `-duration <SECONDS>` | Stop recording after this many seconds. |
| `-proxy <URL>` | HTTP proxy to bypass regional restrictions. |
| `-bitrate <BITRATE>` | No effect anymore: recordings are saved as raw `.ts` and converted offline. |
| `-telegram` | Upload the recording to Telegram when done. Requires `telegram.json`. |
| `-no-update-check` | Skip the automatic update check on startup. |

### Recording Modes

- **`manual`** *(default)*: Records immediately if the user is currently live.
- **`automatic`**: Polls at regular intervals and records whenever the user goes live.
- **`followers`**: Automatically records live streams from all followed users.

## Guide

- [How to set cookies in cookies.json](https://github.com/Michele0303/tiktok-live-recorder/blob/main/docs/GUIDE.md#how-to-set-cookies)
- [How to get room_id](https://github.com/Michele0303/tiktok-live-recorder/blob/main/docs/GUIDE.md#how-to-get-room_id)
- [How to enable upload to Telegram](https://github.com/Michele0303/tiktok-live-recorder/blob/main/docs/GUIDE.md#how-to-enable-upload-to-telegram)

## Contributing

Contributions are welcome! Feel free to open an [issue](https://github.com/Michele0303/tiktok-live-recorder/issues) or submit a [pull request](https://github.com/Michele0303/tiktok-live-recorder/pulls).

## Legal ⚖️

This code is in no way affiliated with, authorized, maintained, sponsored or endorsed by TikTok or any of its affiliates or subsidiaries. Use at your own risk.
