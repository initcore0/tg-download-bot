# tg-download-bot

Telegram bot: send it a media link (YouTube/TikTok/Instagram/X/Twitch/Pinterest/...),
it downloads via yt-dlp, remuxes/transcodes to Telegram-friendly MP4 (≤720p, ≤48MB),
and sends it back as plain media. Read ARCHITECTURE.md before changing anything.

## Commands
- Install: `uv sync`
- Test (offline): `uv run pytest -m "not network"`
- Test (all, hits real sites): `uv run pytest`
- Lint: `uv run ruff check .`
- Run: `uv run tgdl-bot` (needs TELEGRAM_BOT_TOKEN in env or .env)

## Rules
- Latency first: never re-encode when a stream-copy remux suffices; cap at 720p/48MB.
- Output to users is plain media — no captions, links, or branding.
- Frozen contracts (do not change signatures): `tgdl/downloader/models.py`,
  `tgdl/downloader/service.download_media`, `tgdl/storage/repo.py` functions,
  `tgdl/config.py`.
- Every request must be audited via `tgdl/storage/repo.py`; audit failures must never
  break the user-facing flow.
- All blocking work (yt-dlp, ffmpeg) stays off the event loop (to_thread / subprocess exec).
