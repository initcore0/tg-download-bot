"""Entrypoint: config, logging, DB init, aiogram polling.

Fails fast with a clear one-line error if TELEGRAM_BOT_TOKEN is missing.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import sys
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from sqlalchemy.exc import OperationalError

from tgdl.bot import handlers, runtime
from tgdl.config import Settings, load_settings
from tgdl.downloader import gallerydl, transcode, ytdlp
from tgdl.storage import repo

log = logging.getLogger("tgdl")

#: We only ever need these two update types (ARCHITECTURE.md §7).
ALLOWED_UPDATES = ["message", "channel_post"]


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


def _materialize_cookies(settings: Settings) -> tuple[Path | None, bool]:
    """Resolve the cookies source into a file path for the download engines.

    Env-var content wins over the *_FILE path settings; it is written to a
    0600 temp file (never the persistent data volume — the content is a
    credential). Returns (path, is_temporary) so the caller can clean up.
    """
    content = settings.cookies_content
    if not content:
        return settings.effective_cookies_file, False
    fd, name = tempfile.mkstemp(prefix="tgdl-cookies-", suffix=".txt")  # 0600 by default
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_decode_cookies_content(content))
    log.info("cookies loaded from environment variable into a private temp file")
    return Path(name), True


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
    cookies_file, cookies_is_temp = _materialize_cookies(settings)
    ytdlp.configure(cookies_file=cookies_file)
    gallerydl.configure(cookies_file=cookies_file)

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

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(handlers.router)

    # Injected into every handler as the `settings` kwarg.
    dp["settings"] = settings

    runtime.configure(
        settings.max_concurrent_downloads,
        max_per_user=settings.max_per_user_concurrent,
    )

    try:
        await _resolve_username(bot)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES, handle_signals=True)
    finally:
        try:
            await repo.close_db()
        except Exception:
            log.exception("error closing database")
        try:
            await bot.session.close()
        except Exception:
            log.exception("error closing bot session")
        if cookies_is_temp and cookies_file is not None:
            try:
                cookies_file.unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove temp cookies file %s", cookies_file)


async def _healthcheck(settings: Settings) -> int:
    """Liveness probe: confirm the token/network are working via getMe. 0 = healthy."""
    bot = Bot(token=settings.telegram_bot_token)
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
