# tg-download-bot — Architecture

## 1. What it does

A Telegram bot. A user sends it a link to media (YouTube, TikTok, Instagram, Twitter/X,
Twitch clips, Pinterest, Reddit, and anything else yt-dlp supports). The bot downloads the media,
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

Non-goals (explicitly): playlists, quality selection UI, rate limiting beyond a
concurrency cap, ads/branding.

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
          Alongside it, set a 👀 reaction on the *user's* message (private + group only;
          never channel posts), cleared in a finally when processing ends. Both are
          best-effort: a refused reaction is a debug line, never a failed request.
       4. Coalescing gate (runtime.coalesce, keyed on the normalized URL): the first
          request for a link is the leader and proceeds; concurrent requests for the
          same link wait for it (bounded by DOWNLOAD_TIMEOUT_S + 60s) holding neither
          the semaphore nor a workdir, then resume at step 5 — where the leader's
          upload is now a cache hit. If the leader failed, the follower just downloads.
          One level only: a follower never becomes a leader.
       5. file_id cache (§6.1): look up the normalized URL; on a hit re-send those
          file_ids and stop here — no semaphore, no workdir, no download. Any failure
          (repo error, file_id Telegram has forgotten) is just a miss: fall through.
       6. Acquire global semaphore (MAX_CONCURRENT_DOWNLOADS, default 3).
       7. downloader.service.download_media(url, workdir) → list[MediaResult]
       8. Release the semaphore — it caps concurrent *downloads*; the upload that
          follows is network-bound and must not keep the next requester queued.
       9. Send media (sendVideo / sendPhoto / sendAnimation / sendMediaGroup),
          reply-to the triggering message in groups. No caption.
      10. Audit: mark_success(request, results, telegram_file_id, telegram_file_ids,
          cache_hit) or mark_failure(request, error).
      11. Delete workdir (always, in finally).
```

`/mp3 <link>` runs the same eleven steps against the audio pipeline (§5.6) with its
own cache and coalescing keys. An **inline query** (§7.1) is the one flow that skips
all of it: it answers from the cache or not at all.

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
tweets, Pinterest pins, Reddit image posts and galleries, story images, and other image
hosts. Downloaded files go through
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

### 5.6 Audio path (`tgdl/downloader/audio.py`) — `/mp3`

`/mp3 <link>` (alias `/audio`) returns the sound track alone, as a Telegram audio
message with title and duration. It is a separate, smaller pipeline:

```python
async def download_audio(
    url: str, workdir: Path, *, max_size_bytes: int, timeout_s: int,
) -> AudioResult      # path, title, duration_s, filesize, performer
```

- **Format**: `ba[ext=m4a]/bestaudio`, passed to `ytdlp.extract` via its
  `format_override` parameter — the audio path reuses the whole retry ladder
  (backoff, YouTube client cycling, cookie routing) rather than duplicating it.
- **The command is /mp3; the payload is m4a.** Telegram plays m4a natively and almost
  every source already carries an AAC track, so m4a means a stream copy where mp3
  would mean a re-encode on every request. An `ba[ext=m4a]` hit is sent untouched;
  anything else goes through `transcode.to_m4a`, which itself copies an AAC stream
  (`-c:a copy`) and only encodes (AAC 128k) when it must.
- **Errors**: identical taxonomy, SSRF guard, and `asyncio.timeout` wrapper as
  `download_media`. No size-retry ladder — an audio track over the cap is a
  `MediaTooLargeError`, since re-encoding an already-small stream buys little.
- **Not a `MediaResult`.** `models.py` is frozen and its `MediaKind` Literal has no
  `"audio"`, so audio carries its own `AudioResult` dataclass. The audit row still
  needs the right kind, so `repo.mark_success` takes an optional
  `media_kind_override` which the bot sets to `"audio"`.
- **Cache and coalescing are keyed apart from video.** The same link now produces both
  a video row and an audio row, so `repo.find_cached` takes an optional `media_kinds`
  filter (the video flow asks for video/animation/image, `/mp3` asks for audio only)
  and the audio coalescing key is prefixed `audio:`. Neither flow can ever be served
  the other's file_id.

## 6. Storage / audit (`tgdl/storage/`)

SQLite at `DATABASE_PATH` (default `data/tgdl.db`), WAL mode. Table created on startup
(`init_db()`); no migration tool needed yet. Schema evolution is additive and runs in
`db.create_all`: after `metadata.create_all`, `_add_missing_columns` compares each ORM
table against the live one and issues `ALTER TABLE ... ADD COLUMN` for anything absent,
compiling the DDL from the ORM column so types and defaults can't drift. New columns
must therefore stay nullable or carry a `server_default` (SQLite refuses to add a bare
NOT NULL column to a populated table). Idempotent, and a no-op once the schema matches.

**Anonymous by design.** There is no user table and no identifying data. The audit exists
to drive the popular-link cache (§6.1) and to measure latency — neither needs to know who
asked. The only contextual field is a coarse `chat_type`.

```
requests: id PK, chat_type NOT NULL,          -- coarse: private|group|supergroup|channel
          url NOT NULL, normalized_url, platform,
          status (pending|success|failed), error_class, error_message,
          media_kind, title, filesize_bytes, duration_s, width, height,
          transcoded BOOL, telegram_file_id,      -- cache key (§6.1), = first of the list
          telegram_file_ids,                      -- JSON list of ALL sent file_ids, ordered
          cache_hit BOOL NOT NULL DEFAULT 0,      -- row was served from cache, not downloaded
          created_at, completed_at, elapsed_s
Indexes: requests(normalized_url), requests(created_at)
```

Deliberately **not** stored: Telegram user id, username, names, chat id, message id. The
per-user rate-limit key (the Telegram id) lives only in memory (`tgdl/bot/runtime.py`) and
is never persisted.

`normalized_url`: lowercase host, strip tracking params (`utm_*`, `si`, `feature`…),
resolve youtu.be→youtube.com/watch form — this is the cache/dedup key.

Repo API (`tgdl/storage/repo.py`): `init_db`, `create_request`, `find_cached`
(`find_cached_file_id` is a kept alias), `encode_file_ids` / `decode_file_ids`,
`mark_success`, `mark_failure`, `prune_audit`, `stats`. All async except the two
codecs. Audit failures must never break the user flow (log and continue).

### 6.1 file_id cache

The fastest download is the one we skip. `find_cached(normalized_url)` returns the most
recent audit row that is `status='success'`, has a `telegram_file_id`, and is younger
than 30 days; the handler hands those file_ids straight back to `sendVideo` /
`sendAnimation` / `sendPhoto` / `sendMediaGroup` with the stored width/height/duration.
Telegram re-serves its own copy, so the request costs one API call instead of a
download + transcode + upload.

**Images and galleries are cached too.** `telegram_file_ids` stores every file_id a
request sent, in order, so a carousel is replayed whole — as `InputMediaPhoto(media=<file_id>)`
items in one media group (capped at `MEDIA_GROUP_LIMIT`), a single image as one
`sendPhoto`. `telegram_file_id` keeps holding the first item, unchanged. An image row is
only eligible once it carries a non-empty list, so rows written before this column
existed still re-download rather than shipping a carousel as a lone photo.

Deliberate limits:
- **Instagram stories are never cached** — they expire, so a hit would ship content the
  poster has already taken down.
- **Any failure is a miss.** A repo error, or a file_id Telegram has since forgotten, is
  logged and falls through to the normal download path. The cache can never fail a request.

Rows served from the cache are marked `cache_hit=1`, which is what a hit-rate metric
in `/stats` will be built on.

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
- **`check_ytdlp_freshness()`** compares the installed `yt_dlp.version.__version__`
  against PyPI's latest (3s total timeout, aiohttp) and logs one WARNING when they
  differ, DEBUG when current. Extractors break weekly and a stale yt-dlp is the most
  likely cause of a future "nothing downloads any more", so it is worth one log line
  — and one admin alert (§7.3), since nobody reads logs before the outage.
  Runs as a background `create_task` — never awaited in the startup path, cancelled in
  the shutdown `finally` — and any failure (no network, timeout, odd JSON) is swallowed
  at debug level.

## 7. Bot layer (`tgdl/bot/`)

- Long polling via aiogram Dispatcher; single process.
- Handlers: `/start`, `/help` (short usage text), `/mp3` (alias `/audio`, §5.6),
  `/stats` (admin, §7.2), private-message URL handler, group/channel mention handler,
  and an inline-query handler (`message` + `channel_post` + `inline_query` updates).
- Upload via `FSInputFile`; `sendVideo(width, height, duration, supports_streaming=True)`.
- Store every returned file_id into the audit row (`telegram_file_ids`, with the first
  also in `telegram_file_id`) — that write is what populates the §6.1 cache for the next
  person who sends the same link.
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

### 7.1 Inline mode (cache hits only)

`@botname <link>` in any chat answers from the §6.1 file_id cache and never downloads.
Telegram gives an inline query a few seconds and no way to show progress, so starting
a download there would time out and read as broken. Must be enabled once via BotFather
(`/setinline`).

- **Hit**: the cached row is answered as `InlineQueryResultCachedVideo` /
  `CachedMpeg4Gif` / `CachedPhoto` / `CachedAudio` from the stored file_id(s). Inline
  has no media groups, so a gallery row becomes up to 10 separate photo results
  (ids are `<row id>-<index>`, unique within the answer). `cache_time=300`.
- **Miss** (or no URL in the query): zero results plus an
  `InlineQueryResultsButton` into private chat — "send me the link here first" —
  where the normal flow warms the cache for everyone. `cache_time=30`.
- `is_personal=False` throughout: the answer depends only on the link, never on who
  asked, which is both true and consistent with the anonymity design.
- Hits are audited with `chat_type="inline"` and `cache_hit=True`. An inline query
  carries no chat, and nothing identifying is recorded — same rule as everywhere else.

### 7.2 `/stats` (admin)

An ops readout: request/success/failure counts, cache hit rate, and a per-platform
p50/p95 latency table for the last 30 days (percentiles computed in Python by nearest
rank — SQLite has none — over a bounded sample). Replies as an escaped `<pre>` block,
in English only: it is an ops surface, not a user reply.

Gated on **both** a private chat and `message.from_user.id == ADMIN_USER_ID` (0 =
disabled). Every other case does nothing at all — not even an error — so the command's
existence isn't advertised. The admin id is compared in memory and never stored, so §6's
anonymity guarantee is untouched.

### 7.3 Admin alerts (`tgdl/bot/alerts.py`)

The failures that matter operationally — a flagged Instagram session, YouTube
bot-checks from a stale runtime, a broken ffmpeg — are invisible until a user
complains. When `ADMIN_USER_ID` is set and `ADMIN_ALERTS` is on, the bot DMs the
admin about them. Process-wide state in the style of `runtime.py`:
`configure(bot, admin_user_id)` from `main.run()` (id 0, no bot, or `ADMIN_ALERTS=false`
makes every function a no-op), `reset()` for tests.

`report_failure(platform, error, url)` is the single hook, called from the two
except blocks of each download flow alongside `_audit_failure`. It sorts failures
into three tiers:

- **Never alert**: `UnsupportedUrlError`, `MediaTooLargeError`. These are answers,
  not outages; alerting on them would train the admin to ignore the channel.
- **Immediate**: `TranscodeError` (ffmpeg is broken — *every* request is about to
  fail) and any non-`DownloadError` exception (a bug in us). First occurrence alerts.
- **Burst**: `TransientExtractionError`, `AuthRequiredError`, plain `ExtractionError`,
  `DownloadTimeoutError` — failures healthy bots produce one at a time, where only
  the *rate* is interesting. Occurrences are counted per `(platform, error class)` in
  a 900s sliding window (timestamps pruned on every touch, so the map is bounded by
  what is failing right now) and alert once 3 land inside it. `AuthRequiredError`
  additionally says which platform's cookies to refresh.

Every send passes a per-key cooldown (3600s), so an outage lasting all afternoon is
one message an hour rather than one per failed request. Nothing here raises: an
alert that cannot be delivered is a debug line, never a second incident. The startup
freshness check (§6.2) uses the same `notify()` for a one-off "yt-dlp is stale".

**Anonymity holds.** An alert carries the failing URL, platform, and error class/text
— never user ids, chat ids, or chat types. The admin learns the bot is broken, not
who was using it. The counters live in memory for the run and are never persisted.

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
ADMIN_USER_ID        default 0 (disabled) — the only id allowed to run /stats (§7.2)
ADMIN_ALERTS         default true — DM that admin when the bot is unhealthy (§7.3);
                     false keeps /stats without the messages
TELEGRAM_API_URL     default "" (Telegram's cloud API) — see §8.1
```

### 8.1 Self-hosted Bot API server

The 50 MB cap behind `MAX_FILE_SIZE_MB=48` is Telegram's *cloud* Bot API limit, not
ours. Running a `telegram-bot-api` server raises it to 2 GB. Setting
`TELEGRAM_API_URL` makes `main._make_bot` build the Bot with
`AiohttpSession(api=TelegramAPIServer.from_base(url))`; empty (the default) keeps the
cloud API and today's behavior. Every Bot in the process — the polling bot and the
`--healthcheck` probe — is constructed through that one helper so they cannot end up
pointed at different servers.

`docker-compose.yml` carries the server under the `local-api` profile, so a plain
`docker compose up` never starts it; opting in is `docker compose --profile local-api
up -d` plus `TELEGRAM_API_URL` (and `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from
my.telegram.org). `MAX_FILE_SIZE_MB` can then be raised (e.g. 1500).

One-time caveat, documented in `.env.example` and deliberately **not** automated: a
token already used against the cloud API must be released with a single `logOut` call
to the cloud API before a local server will accept it.

## 9. Module ownership & boundaries (for parallel build agents)

| Area | Files | Owner |
|------|-------|-------|
| Contracts & config (FROZEN — do not edit) | `pyproject.toml`, `tgdl/config.py`, `tgdl/downloader/models.py`, stub signatures | Architect |
| M1 Downloader | `tgdl/downloader/{service,ytdlp,transcode,urls}.py`, `tests/test_downloader*.py`, `tests/test_urls.py`, `tests/test_transcode.py` | Agent A |
| M2 Bot | `tgdl/bot/*`, `tgdl/main.py`, `tests/test_bot*.py` | Agent B |
| M3 Storage & Ops | `tgdl/storage/*`, `tests/test_storage*.py`, `Dockerfile`, `docker-compose.yml`, `README.md` | Agent C |

Rules: implement the frozen signatures exactly; do not modify files outside your area;
do not run git commands; mock other modules in your tests.
