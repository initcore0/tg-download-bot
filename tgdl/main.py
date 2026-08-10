"""Entrypoint: config, logging, DB init, aiogram polling.

Fails fast with a clear one-line error if TELEGRAM_BOT_TOKEN is missing.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from tgdl.bot import handlers, runtime
from tgdl.config import Settings, load_settings
from tgdl.storage import repo

log = logging.getLogger("tgdl")

#: We only ever need these two update types (ARCHITECTURE.md §7).
ALLOWED_UPDATES = ["message", "channel_post"]


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
    try:
        await repo.init_db(settings.database_path)
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

    runtime.configure(settings.max_concurrent_downloads)

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


def main() -> None:
    settings = load_settings()

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
