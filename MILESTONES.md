# Milestones

## M0 — Architecture & scaffold (Architect) ✅
Architecture doc, project skeleton, frozen interface contracts (`downloader/models.py`,
stub signatures for service/repo/handlers), config, dependency lockfile.

**Done when:** repo installs with `uv sync`, contracts committed.

## M1 — Downloader pipeline (Agent A)
yt-dlp extraction with latency-first format selection (≤720p, prefer h264/aac mp4),
ffprobe-based compat check, remux-first / transcode-fallback via ffmpeg, size cap with
one 480p retry, URL extraction + platform detection + URL normalization helpers.

**Done when:** `download_media()` returns valid `MediaResult`s for video/image/animation;
unit tests pass offline (mocked yt-dlp/ffmpeg) plus `@pytest.mark.network` live tests;
a real YouTube short and a real Twitter video download and pass ffprobe validation.

## M2 — Bot layer (Agent B)
aiogram 3 bot: /start, /help, private-chat URL handler, group/channel mention handler,
status message UX, media send (video/photo/animation/media group), audit hooks,
concurrency semaphore, error mapping, graceful shutdown, `main.py` entrypoint.

**Done when:** handlers unit-tested with mocked Bot/service/repo; `python -m tgdl.main`
starts and fails fast with a clear message when token is missing.

## M3 — Storage & Ops (Agent C)
SQLAlchemy async models + repo implementation (users, requests, WAL, indexes),
Dockerfile (python:3.12-slim + ffmpeg), docker-compose.yml (volume for data/), README.

**Done when:** repo tests pass against a temp SQLite file; `docker build` succeeds;
README covers BotFather setup (incl. disabling privacy mode), local run, Docker run.

## M4 — Integration & E2E verification (Architect)
Merge all work, full test suite green, live end-to-end check: run the bot with a real
token, send YouTube/TikTok/Twitter/Instagram/Twitch/Pinterest links, receive playable
media in Telegram; verify audit rows; fix whatever breaks.

**Done when:** a real link sent to the real bot returns a playable, forwardable video,
and the request is recorded in SQLite.
