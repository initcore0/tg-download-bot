"""Process-wide runtime state for the bot layer.

Holds the global download semaphore and the cached bot username (resolved once
at startup via ``bot.me()``), so handlers stay free of module import cycles and
tests can set them up directly.
"""
from __future__ import annotations

import asyncio

_semaphore: asyncio.Semaphore | None = None
_bot_username: str | None = None


def configure(max_concurrent_downloads: int, bot_username: str | None = None) -> None:
    """Initialize the global semaphore and (optionally) the cached username."""
    global _semaphore
    _semaphore = asyncio.Semaphore(max_concurrent_downloads)
    if bot_username is not None:
        set_bot_username(bot_username)


def get_semaphore() -> asyncio.Semaphore:
    """Return the global download semaphore, creating a default if unconfigured."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(3)
    return _semaphore


def set_bot_username(username: str | None) -> None:
    global _bot_username
    _bot_username = username.lstrip("@") if username else None


def get_bot_username() -> str | None:
    return _bot_username


def reset() -> None:
    """Test helper: clear all cached state."""
    global _semaphore, _bot_username
    _semaphore = None
    _bot_username = None
