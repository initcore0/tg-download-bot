# tg-download-bot — Architecture

## 1. What it does

A Telegram bot. A user sends it a link to media (YouTube, TikTok, Instagram, Twitter/X,
Twitch clips, Pinterest, and anything else yt-dlp supports). The bot downloads the media,
makes it Telegram-compatible (remux or transcode only when necessary), and sends it back
as a plain video/photo — no captions, no ads, no watermarks — so the user can forward it.

Works in two contexts:
- **Private chat**: any message containing a supported URL is processed.
- **Groups/channels**: the bot is triggered when mentioned (`@botusername <link>`).
  Note: for groups, privacy mode should be disabled via BotFather (`/setprivacy` → Disable)
  so the bot reliably receives mention messages. In channels the bot must be an admin.

Every request is audited in a database: what URL, what happened, how long it took, and
the resulting Telegram `file_id` — which powers the re-send-without-re-download cache
(§6.1): a link someone already fetched comes back instantly, with no download at all.

## 2. Design goals

1. **Latency first.** The user is on a phone. 720p is plenty. Never re-encode when a
   remux (stream copy) suffices. Pick source formats that are already H.264/AAC MP4.
2. **Plain output.** The returned media has no caption, no links, no branding.
3. **End-to-end reliability.** Clear user-facing errors ("This link isn't supported",
   "Video too large"), timeouts, and cleanup of temp files.
4. **Audit everything.** Users + requests + outcomes in SQLite.

Non-goals (explicitly): playlists, audio-only extraction, quality selection UI, rate
limiting beyond a concurrency cap, ads/branding.

## 3. Stack

| Concern        | Choice                                   | Why |
|----------------|------------------------------------------|-----|
| Language       | Python 3.12, fully async                 | yt-dlp is Python; best ecosystem fit |
| Bot framework  | aiogram 3.x (long polling)               | Modern async, no webhook/infra needed |
| Extraction     | yt-dlp (Python API, in thread executor)  | Supports all required platforms |
| Image fallback | gallery-dl (async subprocess)            | Image posts/stories yt-dlp can't extract |
| Transcoding    | ffmpeg subprocess (async)                | Remux-first; libx264 veryfast fallback |
| DB             | SQLite via SQLAlchemy 2 async + aiosqlite| Zero infra; trivial to move to Postgres |
| Config         | pydantic-settings, `.env`                | Typed env config |
| Packaging      | uv + pyproject.toml                      | Fast, lockfile |
| Tests          | pytest + pytest-asyncio                  | Unit tests with mocks; live-network tests marked `@pytest.mark.network` |
| Deploy         | Dockerfile (python:3.12-slim + ffmpeg)   | Single container, volume for SQLite |

## 4. Request flow

```
Telegram update
  └─ bot/handlers.py
       1. Extract URL(s) from message text/entities (first supported URL wins).
          Private chat: any URL. Group/channel: only when @botusername is mentioned.
       2. Audit: create_request — anonymous, no user/chat identifiers  (storage/repo.py)
       3. React immediately with a neutral "typing…" chat action, kept alive via
          ChatActionSender for the whole download (actions expire ~5s). Only after the
          download succeeds — when we know the link was downloadable and what it holds —
          does the upload phase switch to "sending a photo…"/"sending a video…".
       4. file_id cache (§6.1): look up the normalized URL; on a hit re-send that
          file_id and stop here — no semaphore, no workdir, no download. Any failure
          (repo error, file_id Telegram has forgotten) is just a miss: fall through.
       5. Acquire global semaphore (MAX_CONCURRENT_DOWNLOADS, default 3).
       6. downloader.service.download_media(url, workdir) → list[MediaResult]
       7. Release the semaphore — it caps concurrent *downloads*; the upload that
          follows is network-bound and must not keep the next requester queued.
       8. Send media (sendVideo / sendPhoto / sendAnimation / sendMediaGroup),
          reply-to the triggering message in groups. No caption.
       9. Audit: mark_success(request, results, telegram_file_id) or mark_failure(request, error).
      10. Delete workdir (always, in finally).
```

## 5. Downloader pipeline (`tgdl/downloader/`)

### 5.1 Format selection (latency-critical)

yt-dlp format selector, tried in order:

```
bv*[height<=720][ext=mp4][vcodec^=avc1]+ba[ext=m4a]   # ideal: h264+aac, remux only
/ b[height<=720][ext=mp4]                              # progressive mp4
/ bv*[height<=720]+ba/b[height<=720]                   # anything ≤720p → transcode
/ b                                                    # last resort (e.g. only 1080p exists)
```

Plus `--format-sort "res:720,codec:h264,br"` semantics: prefer ≤720p, h264, lower bitrate.
`max_filesize`-style pre-filtering is unreliable across extractors, so size is enforced
post-download (see 5.3).

### 5.2 Telegram-compat decision (ffprobe → remux vs transcode)

After download, ffprobe the file:

- Container is mp4/mov, video is h264, audio is aac/none → **use as-is** (0 cost).
- Codecs OK but container is mkv/webm → **remux**: `ffmpeg -c copy -movflags +faststart` (~1s).
- Video is vp9/av1/hevc or audio incompatible → **transcode**:
  `libx264 -preset veryfast -crf 26 -maxrate 2.5M -bufsize 5M`, scale to ≤720p even pixels,
  `aac -b:a 128k`, `-movflags +faststart`.
- Always ensure `+faststart` (moov atom up front) so Telegram can stream/preview it.

Images pass through untouched (jpg/png/webp→jpg for compatibility). GIF-like results
(no audio, short, source `.gif`) → kind `animation`.

### 5.3 Size policy

Bot API upload limit is 50 MB → hard cap `MAX_FILE_SIZE_MB=48`.

- Result ≤ cap → done.
- Over cap → one retry: transcode at 480p with computed bitrate
  `target_bitrate = 0.92 * cap * 8 / duration - audio_bitrate`.
- Still over (very long videos) → fail with `MediaTooLargeError` → user gets a clear message.

### 5.4 Contract (frozen — see `tgdl/downloader/models.py`)

```python
async def download_media(
    url: str, workdir: Path, *,
    max_size_bytes: int, max_height: int, timeout_s: int,
) -> list[MediaResult]      # usually len 1; >1 for image carousels (≤10)
```

`MediaResult`: path, kind (video|image|animation), title, width, height, duration_s,
filesize, source_url, platform, transcoded (bool), elapsed_s.
Errors (all subclass `DownloadError`, each has a `user_message`):
`UnsupportedUrlError`, `ExtractionError`, `MediaTooLargeError`, `TranscodeError`,
`DownloadTimeoutError`.

yt-dlp options: `quiet`, `noplaylist=True`, `playlist_items="1-10"` only for image
galleries, no cache writes outside workdir. yt-dlp's blocking calls run via
`asyncio.to_thread`. Whole operation wrapped in `asyncio.timeout(timeout_s)` (default 300s).

### 5.5 Image fallback engine (`tgdl/downloader/gallerydl.py`)

yt-dlp only extracts videos. When it reports a permanent failure — including the
"there is no video in this post" family, which `ytdlp._classify` maps to a permanent
`ExtractionError` precisely so no retry time is wasted — the service retries the URL
through **gallery-dl** (async subprocess, output confined to `workdir/gallery/`,
`--config-ignore`, `--range 1-10`). This covers Instagram photo posts/carousels, image
tweets, Pinterest pins, story images, and other image hosts. Downloaded files go through
the same per-file pipeline (`_process_file`): images pass through (webp→jpg), story
*videos* take the normal remux/transcode path. If the fallback also fails, the original
yt-dlp error is re-raised — unless gallery-dl hit a login wall, in which case the more
actionable `AuthRequiredError` ("error.login_required") wins.

**When the fallback runs.** Only for `GALLERY_PLATFORMS` and `"other"`. Video-only
platforms (youtube, tiktok, twitch) have no images to find, so their extraction errors
go straight to the user. A `TransientExtractionError` (throttle, bot-check, 5xx) is
re-raised before the fallback is even considered: it means "not right now", not "this
post has no video", and running a second engine against it only burns more of the
platform's patience.

**Stories.** `instagram.com/stories/...` is always login-gated: with no cookies file
configured the service fails fast with `AuthRequiredError` before either engine runs.
**Cookie routing (`tgdl/downloader/cookies.py`).** Cookies live in per-platform jars
so each site only ever receives its own credentials: `YOUTUBE_COOKIES` is used only
for YouTube, `INSTAGRAM_COOKIES` only for Instagram, and `COOKIES` is the generic
fallback for platforms without a dedicated jar. Instagram posts/reels are fetched
**anonymously** by design (an always-on session on public content is what gets
accounts flagged); the Instagram jar is used for stories, plus one automatic retry
when an anonymous attempt hits a login wall. Each jar is set either as env-var
*content* (raw or base64, materialized to a 0600 temp file at startup) or as a
`*_FILE` path; content wins over path. Both engines receive the resolved jar
per-request (`cookies_file=` parameter), not via global state.

## 6. Storage / audit (`tgdl/storage/`)

SQLite at `DATABASE_PATH` (default `data/tgdl.db`), WAL mode. Table created on startup
(`init_db()`); no migration tool needed yet.

**Anonymous by design.** There is no user table and no identifying data. The audit exists
to drive the popular-link cache (§6.1) and to measure latency — neither needs to know who
asked. The only contextual field is a coarse `chat_type`.

```
requests: id PK, chat_type NOT NULL,          -- coarse: private|group|supergroup|channel
          url NOT NULL, normalized_url, platform,
          status (pending|success|failed), error_class, error_message,
          media_kind, title, filesize_bytes, duration_s, width, height,
          transcoded BOOL, telegram_file_id,      -- cache key (§6.1)
          created_at, completed_at, elapsed_s
Indexes: requests(normalized_url), requests(created_at)
```

Deliberately **not** stored: Telegram user id, username, names, chat id, message id. The
per-user rate-limit key (the Telegram id) lives only in memory (`tgdl/bot/runtime.py`) and
is never persisted.

`normalized_url`: lowercase host, strip tracking params (`utm_*`, `si`, `feature`…),
resolve youtu.be→youtube.com/watch form — this is the cache/dedup key.

Repo API (`tgdl/storage/repo.py`): `init_db`, `create_request`, `find_cached_file_id`,
`mark_success`, `mark_failure`, `prune_audit`, `stats`. All async. Audit failures must
never break the user flow (log and continue).

### 6.1 file_id cache

The fastest download is the one we skip. `find_cached_file_id(normalized_url)` returns
the most recent audit row that is `status='success'`, has a `telegram_file_id`, and is
younger than 30 days; the handler hands that file_id straight back to `sendVideo` /
`sendAnimation` with the stored width/height/duration. Telegram re-serves its own copy,
so the request costs one API call instead of a download + transcode + upload.

Deliberate limits:
- **Videos and animations only.** An image gallery's row stores only the *first* item's
  file_id, so replaying it would silently drop the rest of a carousel. Serving images
  from cache needs a schema change; until then they always re-download.
- **Instagram stories are never cached** — they expire, so a hit would ship content the
  poster has already taken down.
- **Any failure is a miss.** A repo error, or a file_id Telegram has since forgotten, is
  logged and falls through to the normal download path. The cache can never fail a request.

### 6.2 Housekeeping (startup)

Two best-effort passes run once in `main.run()`, after `init_db` and before polling.
Neither can block startup: both are wrapped, log, and continue.

- **`prune_audit()`** deletes rows older than 90 days, and marks rows still `pending`
  after an hour as `failed` / `error_class='StaleRequest'` — those belong to a run that
  crashed mid-download, not to anything in flight.
- **`sweep_orphan_workdirs()`** deletes `req-*` dirs under `DOWNLOAD_DIR` whose mtime is
  older than `2 × DOWNLOAD_TIMEOUT_S`. The `finally` in the handler cleans up the normal
  path; this catches what a crash or a post-timeout zombie yt-dlp thread left behind. The
  2× margin guarantees a download running right now is never swept out from under itself.

## 7. Bot layer (`tgdl/bot/`)

- Long polling via aiogram Dispatcher; single process.
- Handlers: `/start`, `/help` (short usage text), private-message URL handler,
  group/channel mention handler (`message` + `channel_post` updates).
- Upload via `FSInputFile`; `sendVideo(width, height, duration, supports_streaming=True)`.
- Store returned `message.video.file_id` into the audit row — that write is what
  populates the §6.1 cache for the next person who sends the same link.
- Per-chat politeness: reply_to the triggering message in groups; plain send in private.
- Errors: the localized message for `DownloadError.message_key` (or a caller-supplied
  `custom_message`) shown to user; unexpected exceptions → generic "Something went wrong"
  + full traceback in logs.
- Graceful shutdown: cancel in-flight tasks, close DB.

**Localization (`tgdl/i18n.py`).** EN + RU. Locale is resolved per message from
`from_user.language_code` (`ru`/`ru-RU` → Russian, else English; channel posts → English)
and never stored, so it doesn't affect anonymity. All user-facing strings live in one
catalog keyed by message id. The downloader is language-agnostic: each `DownloadError`
carries a stable `message_key`, and the bot translates it at send time.

## 8. Config (env / `.env` — see `.env.example`)

```
TELEGRAM_BOT_TOKEN   (required)
DATABASE_PATH        default data/tgdl.db
DOWNLOAD_DIR         default data/downloads   (workdir per request, always cleaned)
MAX_FILE_SIZE_MB     default 48
MAX_HEIGHT           default 720
MAX_CONCURRENT_DOWNLOADS default 3
DOWNLOAD_TIMEOUT_S   default 300
LOG_LEVEL            default INFO
YOUTUBE_COOKIES      Netscape cookies.txt CONTENT (raw or base64), used only for
                     YouTube (bot-check bypass)
INSTAGRAM_COOKIES    same, used only for Instagram (stories + login-wall retry;
                     public posts stay anonymous)
COOKIES              same, generic jar for platforms without a dedicated one
*_COOKIES_FILE       file-path variant of each jar (COOKIES_FILE,
                     YOUTUBE_COOKIES_FILE, INSTAGRAM_COOKIES_FILE); content wins
```

## 9. Module ownership & boundaries (for parallel build agents)

| Area | Files | Owner |
|------|-------|-------|
| Contracts & config (FROZEN — do not edit) | `pyproject.toml`, `tgdl/config.py`, `tgdl/downloader/models.py`, stub signatures | Architect |
| M1 Downloader | `tgdl/downloader/{service,ytdlp,transcode,urls}.py`, `tests/test_downloader*.py`, `tests/test_urls.py`, `tests/test_transcode.py` | Agent A |
| M2 Bot | `tgdl/bot/*`, `tgdl/main.py`, `tests/test_bot*.py` | Agent B |
| M3 Storage & Ops | `tgdl/storage/*`, `tests/test_storage*.py`, `Dockerfile`, `docker-compose.yml`, `README.md` | Agent C |

Rules: implement the frozen signatures exactly; do not modify files outside your area;
do not run git commands; mock other modules in your tests.
