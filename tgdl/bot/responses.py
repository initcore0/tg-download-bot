"""User-facing strings, localized via `tgdl.i18n`.

Media itself is always sent WITHOUT captions or branding (ARCHITECTURE.md §2); these
strings are only for command replies, status, and errors. Every helper takes a `locale`
("en" | "ru") resolved from the Telegram user's language_code.
"""
from __future__ import annotations

from html import escape
from typing import Any

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


def mp3_usage(locale: str = i18n.DEFAULT_LOCALE) -> str:
    return i18n.t("usage.mp3", locale)


def stats_text(data: dict[str, Any]) -> str:
    """Format `repo.stats()` as a compact monospace block.

    Admin-facing and deliberately English-only — it is an ops readout, not a user
    reply, so it stays out of the translation catalog. Everything is escaped and
    wrapped in <pre> because the bot's default parse mode is HTML and platform
    names come from URLs.
    """
    lines = [
        f"requests   {data.get('requests', 0)}",
        f"success    {data.get('success', 0)}",
        f"failed     {data.get('failed', 0)}",
        f"pending    {data.get('pending', 0)}",
        f"cache hits {data.get('cache_hits', 0)} ({data.get('hit_rate', 0.0) * 100:.0f}%)",
    ]

    platforms = data.get("platforms") or {}
    if platforms:
        lines.append("")
        lines.append("last 30d   n     p50     p95")
        for platform, entry in platforms.items():
            lines.append(
                f"{platform[:10]:<10} {entry['count']:<5} "
                f"{entry['p50_s']:>5.1f}s  {entry['p95_s']:>5.1f}s"
            )

    return "<pre>" + escape("\n".join(lines)) + "</pre>"
