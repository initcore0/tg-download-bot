# tg-download-bot

A Telegram bot that turns media links into plain, forwardable Telegram media.

Send it a link — YouTube, TikTok, Instagram, X/Twitter, Twitch clips, Pinterest, or
anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports — and it downloads the
media, makes it Telegram-compatible, and sends it straight back.

## Features

- **Broad platform support** via yt-dlp (hundreds of sites, not just the ones listed above).
- **Latency first.** Prefers source formats that are already H.264/AAC MP4 so it can
  stream-copy instead of re-encoding; capped at 720p. Transcoding is a fallback, not the norm.
- **Plain output.** No captions, no links, no watermarks, no branding — the media arrives
  clean and ready to forward.
- **Size-aware.** Enforces Telegram's bot upload limit (48 MB cap) with one automatic 480p
  compression retry before giving up with a clear message.
- **Works in private chats, groups, and channels.** Private chats process any supported URL;
  groups and channels respond to `@yourbotname <link>` mentions.
- **Clear errors.** Unsupported links, private/deleted media, oversized videos, and timeouts
  all produce a specific user-facing message.
- **Audit database.** Every request is recorded in SQLite: who asked, the URL, the outcome,
  timing, and the resulting Telegram `file_id`.

## Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/) (local runs), or Docker (container runs).
- **ffmpeg** and **ffprobe** on `PATH` for local runs. The Docker image installs them for you.

## Telegram setup (BotFather)

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. Copy the token BotFather returns — this is your `TELEGRAM_BOT_TOKEN`.

### For group usage: disable privacy mode

By default a bot only receives messages that are commands, so mention-triggered downloads
will silently not work in groups. Disable privacy mode:

1. Send `/setprivacy` to BotFather.
2. Select your bot.
3. Choose **Disable**.

> Re-add the bot to existing groups after changing this — the setting is applied when the
> bot joins.

### For channels

The bot must be added to the channel as an **administrator** to receive `channel_post`
updates. Trigger it there the same way: `@yourbotname <link>`.

## Running locally

```bash
uv sync                       # install dependencies
cp .env.example .env          # then edit .env and set TELEGRAM_BOT_TOKEN
uv run tgdl-bot
```

The bot uses long polling — no public URL, webhook, or reverse proxy required. It exits
immediately with a clear message if `TELEGRAM_BOT_TOKEN` is unset.

## Running with Docker

```bash
cp .env.example .env          # then edit .env and set TELEGRAM_BOT_TOKEN
docker compose up -d --build
docker compose logs -f
```

The compose service reads `.env`, mounts `./data` into the container for the SQLite
database and temporary download workdirs, and restarts automatically unless explicitly
stopped. The container runs as a non-root user.

To stop it:

```bash
docker compose down
```

## Configuration

All settings are read from the environment or a `.env` file. Only the token is required.

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **Required.** Token from BotFather. |
| `DATABASE_PATH` | `data/tgdl.db` | SQLite audit database path. |
| `DOWNLOAD_DIR` | `data/downloads` | Per-request temp workdir root; always cleaned up. |
| `MAX_FILE_SIZE_MB` | `48` | Upload size cap (Telegram's bot limit is 50 MB). |
| `MAX_HEIGHT` | `720` | Maximum video height; larger sources are downscaled. |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Global cap on simultaneous downloads. |
| `DOWNLOAD_TIMEOUT_S` | `300` | Per-request timeout in seconds. |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`, …). |

## The audit database

Every request is recorded in SQLite at `DATABASE_PATH` (`data/tgdl.db` by default), created
automatically on startup and running in WAL mode. There are two tables:

- **`users`** — Telegram id, username and names (refreshed on every interaction),
  first/last seen timestamps, and a running request count.
- **`requests`** — the requesting user and chat, the original and normalized URL, detected
  platform, status (`pending` / `success` / `failed`), error class and message on failure,
  media metadata (kind, title, size, duration, dimensions, whether it was transcoded), the
  returned Telegram `file_id`, and timing.

All timestamps are stored as timezone-aware UTC. Recording the `file_id` lays the groundwork
for re-sending previously downloaded media without re-downloading it; that optimization is
not implemented yet. Audit writes are best-effort by design — a database problem is logged
but never breaks a user's download.

Since WAL mode is enabled, the database is safe to query read-only while the bot is running:

```bash
sqlite3 data/tgdl.db "SELECT status, COUNT(*) FROM requests GROUP BY status;"
```

Back up the whole `data/` directory (including the `-wal` and `-shm` files) to preserve history.

## Development

```bash
uv run pytest -m "not network"   # offline test suite
uv run pytest                    # includes tests that hit real sites
uv run ruff check .              # lint
```

See `ARCHITECTURE.md` for the design, module boundaries, and frozen interface contracts.
