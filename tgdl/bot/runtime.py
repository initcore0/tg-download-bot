"""Process-wide runtime state for the bot layer.

Holds the global download semaphore, the per-user in-flight limiter, and the cached
bot username (resolved once at startup via ``bot.me()``), so handlers stay free of
module import cycles and tests can set them up directly.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

_semaphore: asyncio.Semaphore | None = None
_bot_username: str | None = None

# Per-user in-flight download counts (abuse guard). Keyed by Telegram user id;
# chat-level fallback key is used when there is no from_user (channel posts).
_inflight: dict[int, int] = defaultdict(int)
_max_per_user: int = 1


def configure(
    max_concurrent_downloads: int,
    bot_username: str | None = None,
    *,
    max_per_user: int = 1,
) -> None:
    """Initialize the global semaphore, per-user cap, and (optionally) the username."""
    global _semaphore, _max_per_user
    _semaphore = asyncio.Semaphore(max_concurrent_downloads)
    _max_per_user = max(1, max_per_user)
    _inflight.clear()
    if bot_username is not None:
        set_bot_username(bot_username)


@contextmanager
def user_slot(user_key: int) -> Iterator[bool]:
    """Reserve one in-flight slot for `user_key`.

    Yields True if the slot was granted (user was under their cap), False if the user
    is already at their concurrent-download limit. The slot is released on exit only
    when it was actually granted, so `with user_slot(k) as ok:` is always safe.
    """
    granted = _inflight[user_key] < _max_per_user
    if granted:
        _inflight[user_key] += 1
    try:
        yield granted
    finally:
        if granted:
            _inflight[user_key] -= 1
            if _inflight[user_key] <= 0:
                _inflight.pop(user_key, None)


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
    global _semaphore, _bot_username, _max_per_user
    _semaphore = None
    _bot_username = None
    _max_per_user = 1
    _inflight.clear()
