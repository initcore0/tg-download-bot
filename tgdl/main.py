"""Entrypoint: config, logging, DB init, aiogram polling.

Fails fast with a clear one-line error if TELEGRAM_BOT_TOKEN is missing.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import shutil
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from sqlalchemy.exc import OperationalError

from tgdl.bot import handlers, runtime
from tgdl.config import Settings, load_settings
from tgdl.downloader import cookies, transcode
from tgdl.storage import repo

log = logging.getLogger("tgdl")

#: The update types we act on (ARCHITECTURE.md §7). `inline_query` serves cache hits
#: only and must also be enabled once via BotFather (/setinline).
ALLOWED_UPDATES = ["message", "channel_post", "inline_query"]

#: A workdir is only swept once it is this many times older than the download timeout,
#: so a download running at the moment we start up is never pulled out from under it.
ORPHAN_AGE_FACTOR = 2


def sweep_orphan_workdirs(download_dir: Path, timeout_s: int) -> int:
    """Delete `req-*` workdirs left behind by crashes or post-timeout zombie threads.

    Returns how many were removed. Never raises: a dirty download dir is not a
    reason to refuse to start.
    """
    removed = 0
    try:
        base = Path(download_dir)
        if not base.is_dir():
            return 0
        cutoff = time.time() - timeout_s * ORPHAN_AGE_FACTOR
        for entry in base.iterdir():
            if not entry.name.startswith(handlers.WORKDIR_PREFIX) or not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    except Exception:
        log.exception("workdir sweep failed; continuing startup")
        return removed

    if removed:
        log.info("removed %d orphaned download workdir(s) from %s", removed, download_dir)
    return removed


def _decode_cookies_content(content: str) -> str:
    """Normalize a COOKIES env value into Netscape cookies.txt text.

    Accepts the raw file text (real newlines/tabs survive multiline env vars on
    Coolify and in .env), its base64 encoding (a single line without tabs — the
    safest way to pass it through any dashboard), or a single-line paste with
    literal ``\\n``/``\\t`` escape sequences.
    """
    content = content.strip()
    if "\t" in content or "\n" in content or content.startswith("#"):
        text = content
    else:
        try:
            text = base64.b64decode(content.encode(), validate=True).decode()
        except (binascii.Error, ValueError, UnicodeDecodeError):
            text = content
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    return text if text.endswith("\n") else text + "\n"


def _materialize_jar(
    content: str, file: Path | None, label: str, temp_files: list[Path]
) -> Path | None:
    """Resolve one cookie jar: env-var content (raw/base64) wins over a file path.

    Content is written to a 0600 temp file (never the persistent data volume —
    it's a credential); the path is appended to `temp_files` for cleanup.
    """
    content = (content or "").strip()
    if not content:
        return file
    fd, name = tempfile.mkstemp(prefix=f"tgdl-cookies-{label}-", suffix=".txt")  # 0600 by default
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_decode_cookies_content(content))
    log.info("%s cookies loaded from environment variable into a private temp file", label)
    temp_files.append(Path(name))
    return Path(name)


def _materialize_cookies(settings: Settings) -> tuple[dict[str, Path | None], list[Path]]:
    """Resolve all three cookie jars; returns ({name: path}, temp files to clean up)."""
    temp_files: list[Path] = []
    jars = {
        "generic": _materialize_jar(
            settings.cookies, settings.cookies_file, "generic", temp_files
        ),
        "youtube": _materialize_jar(
            settings.youtube_cookies, settings.youtube_cookies_file, "youtube", temp_files
        ),
        "instagram": _materialize_jar(
            settings.instagram_cookies, settings.instagram_cookies_file, "instagram", temp_files
        ),
    }
    return jars, temp_files


#: PyPI's JSON metadata endpoint for the package whose extractors keep us alive.
YTDLP_PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"

#: Hard bound on the freshness check. It is a log line, not a feature: if PyPI is slow
#: or unreachable we drop it rather than hold anything up.
YTDLP_CHECK_TIMEOUT_S = 3


async def check_ytdlp_freshness() -> None:
    """Log a WARNING when the installed yt-dlp is behind the latest release on PyPI.

    Extractors break weekly and yt-dlp ships fixes at the same pace, so a pinned or
    forgotten version is the most likely cause of a site suddenly failing for every
    user. This turns that into one visible log line at startup.

    Entirely best-effort: no network, a timeout, or unexpected JSON logs at debug and
    is swallowed. Runs as a background task and must never delay or fail startup.
    """
    try:
        import aiohttp
        from yt_dlp.version import __version__ as installed

        timeout = aiohttp.ClientTimeout(total=YTDLP_CHECK_TIMEOUT_S)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(YTDLP_PYPI_URL) as response,
        ):
            response.raise_for_status()
            payload = await response.json()

        latest = payload["info"]["version"]
        if latest != installed:
            log.warning(
                "yt-dlp %s is installed but %s is the latest release — extractors break "
                "weekly; upgrade if downloads start failing",
                installed,
                latest,
            )
        else:
            log.debug("yt-dlp %s is current", installed)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("yt-dlp freshness check failed; skipping", exc_info=True)


def _make_bot(settings: Settings, *, default: DefaultBotProperties | None = None) -> Bot:
    """Build a Bot pointed at either Telegram's cloud API or a self-hosted server.

    `TELEGRAM_API_URL` opts into a local `telegram-bot-api` instance, whose upload
    limit is 2 GB instead of the cloud's 50 MB (see .env.example / ARCHITECTURE.md §8).
    Every Bot in the process goes through here so the main bot and the healthcheck
    can't end up talking to different servers.
    """
    api_url = settings.telegram_api_url.strip()
    session = None
    if api_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(api_url))
        log.info("using self-hosted Bot API server at %s", api_url)
    return Bot(token=settings.telegram_bot_token, session=session, default=default)


async def _resolve_username(bot: Bot) -> str | None:
    """Cache the bot's @username so group/channel mention matching works."""
    try:
        me = await bot.me()
    except TelegramUnauthorizedError:
        raise SystemExit(
            "ERROR: Telegram rejected the bot token (401 Unauthorized). "
            "Check TELEGRAM_BOT_TOKEN."
        ) from None
    except Exception:
        log.exception("could not resolve bot username; mention handling may not work")
        return None
    runtime.set_bot_username(me.username)
    log.info("running as @%s (id=%s)", me.username, me.id)
    return me.username


async def run(settings: Settings) -> None:
    """Async entrypoint: init DB, start long polling, shut down cleanly."""
    if not transcode.ffmpeg_available():
        raise SystemExit(
            "ERROR: ffmpeg and/or ffprobe were not found on PATH. They are required "
            "for remuxing and transcoding. Install ffmpeg (the Docker image already "
            "includes it) and try again."
        )
    jars, cookie_temp_files = _materialize_cookies(settings)
    cookies.configure(
        generic=jars["generic"], youtube=jars["youtube"], instagram=jars["instagram"]
    )

    try:
        await repo.init_db(settings.database_path)
    except (OperationalError, OSError) as exc:
        detail = str(getattr(exc, "orig", None) or exc)
        # Only the genuine can't-open-file case gets the permissions guidance;
        # other operational errors (e.g. a failed migration) must not be
        # mislabeled as a filesystem problem.
        if "unable to open database file" in detail.lower():
            raise SystemExit(
                f"ERROR: cannot open the SQLite database at {settings.database_path} "
                f"({detail}). The directory must exist and be writable by the bot "
                "user (uid 10001 in Docker). If you mount a volume there, the stock "
                "image fixes ownership automatically on start; otherwise run "
                "`chown -R 10001:10001 <data dir>` on the host, or point "
                "DATABASE_PATH somewhere writable."
            ) from exc
        log.exception("database initialization failed")
        raise
    except Exception:
        log.exception("database init failed")
        raise

    # Housekeeping, best-effort: neither a full audit table nor a littered download
    # dir is worth failing startup over.
    try:
        pruned = await repo.prune_audit()
        if pruned["deleted"] or pruned["stale_pending"]:
            log.info(
                "audit pruned: %d expired row(s) deleted, %d stale pending row(s) closed",
                pruned["deleted"],
                pruned["stale_pending"],
            )
    except Exception:
        log.exception("audit prune failed; continuing")

    sweep_orphan_workdirs(settings.download_dir, settings.download_timeout_s)

    bot = _make_bot(settings, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(handlers.router)

    # Injected into every handler as the `settings` kwarg.
    dp["settings"] = settings

    runtime.configure(
        settings.max_concurrent_downloads,
        max_per_user=settings.max_per_user_concurrent,
        follower_timeout_s=settings.download_timeout_s + runtime.FOLLOWER_TIMEOUT_SLACK_S,
    )

    # Fire-and-forget: a stale yt-dlp is the single most likely cause of a future
    # "nothing downloads any more", so make it visible in the logs. Never awaited in
    # the critical path — the reference only exists to keep it off the GC's radar.
    version_check = asyncio.create_task(check_ytdlp_freshness())

    try:
        await _resolve_username(bot)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES, handle_signals=True)
    finally:
        # The freshness check logs its own failures; shutdown only needs it stopped.
        version_check.cancel()
        with suppress(BaseException):
            await version_check
        try:
            await repo.close_db()
        except Exception:
            log.exception("error closing database")
        try:
            await bot.session.close()
        except Exception:
            log.exception("error closing bot session")
        for temp_cookie in cookie_temp_files:
            try:
                temp_cookie.unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove temp cookies file %s", temp_cookie)


async def _healthcheck(settings: Settings) -> int:
    """Liveness probe: confirm the token/network are working via getMe. 0 = healthy."""
    bot = _make_bot(settings)
    try:
        await bot.get_me()
        return 0
    except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await bot.session.close()


def main() -> None:
    settings = load_settings()

    if "--healthcheck" in sys.argv:
        if not settings.telegram_bot_token.strip():
            sys.exit(1)
        sys.exit(asyncio.run(_healthcheck(settings)))

    if not settings.telegram_bot_token.strip():
        print(
            "ERROR: TELEGRAM_BOT_TOKEN is not set. "
            "Put it in your environment or a .env file (see .env.example).",
            file=sys.stderr,
        )
        sys.exit(1)

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
