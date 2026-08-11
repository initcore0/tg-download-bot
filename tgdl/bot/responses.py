"""User-facing strings, localized via `tgdl.i18n`.

Media itself is always sent WITHOUT captions or branding (ARCHITECTURE.md §2); these
strings are only for command replies, status, and errors. Every helper takes a `locale`
("en" | "ru") resolved from the Telegram user's language_code.
"""
from __future__ import annotations

from tgdl import i18n

# Fallback when the bot username isn't known yet (bot.me() not resolved).
DEFAULT_USERNAME = "the_bot"


def start_text(username: str | None, locale: str = i18n.DEFAULT_LOCALE) -> str:
    return i18n.t("start", locale, username=username or DEFAULT_USERNAME)


def help_text(username: str | None, locale: str = i18n.DEFAULT_LOCALE) -> str:
    return i18n.t("help", locale, username=username or DEFAULT_USERNAME)


def busy_per_user(locale: str = i18n.DEFAULT_LOCALE) -> str:
    return i18n.t("busy_per_user", locale)


def generic_error(locale: str = i18n.DEFAULT_LOCALE) -> str:
    return i18n.t("generic_error", locale)
