"""Process-wide runtime state for the bot layer.

Holds the global download semaphore, the per-user in-flight limiter, the in-flight
URL coalescing gate, and the cached bot username (resolved once at startup via
``bot.me()``), so handlers stay free of module import cycles and tests can set them
up directly.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

log = logging.getLogger(__name__)

_semaphore: asyncio.Semaphore | None = None
_bot_username: str | None = None

# Per-user in-flight download counts (abuse guard). Keyed by Telegram user id;
# chat-level fallback key is used when there is no from_user (channel posts).
_inflight: dict[int, int] = defaultdict(int)
_max_per_user: int = 1

# In-flight coalescing: normalized URL -> the event the leader sets when it's done.
# See `coalesce` for the leader/follower contract.
_leaders: dict[str, asyncio.Event] = {}

#: Slack added to the download timeout when waiting on a leader, covering the upload
#: that follows the download. Only ever an upper bound — the event fires as soon as
#: the leader finishes.
FOLLOWER_TIMEOUT_SLACK_S = 60
DEFAULT_FOLLOWER_TIMEOUT_S = 300 + FOLLOWER_TIMEOUT_SLACK_S

_follower_timeout_s: float = DEFAULT_FOLLOWER_TIMEOUT_S


def configure(
    max_concurrent_downloads: int,
    bot_username: str | None = None,
    *,
    max_per_user: int = 1,
    follower_timeout_s: float = DEFAULT_FOLLOWER_TIMEOUT_S,
) -> None:
    """Initialize the semaphore, per-user cap, coalescing timeout, and the username."""
    global _semaphore, _max_per_user, _follower_timeout_s
    _semaphore = asyncio.Semaphore(max_concurrent_downloads)
    _max_per_user = max(1, max_per_user)
    # Floored just above zero: a non-positive timeout would make every follower give
    # up instantly, defeating the gate. Tests legitimately configure fractions.
    _follower_timeout_s = max(0.01, float(follower_timeout_s))
    _inflight.clear()
    _leaders.clear()
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


@asynccontextmanager
async def coalesce(normalized_url: str) -> AsyncIterator[bool]:
    """Gate concurrent requests for the same link behind a single leader.

    Two people posting the same viral link seconds apart would otherwise download it
    twice at once. The first request for a normalized URL becomes the *leader* and
    proceeds normally; anyone arriving while it runs is a *follower* and waits here
    for the leader to finish before re-checking the file_id cache — the leader's
    success is a cache hit, and its failure just means the follower downloads itself.

    Yields True to the leader and False to a follower (whose wait has already
    happened by the time the body runs). One level only: a follower that ends up
    downloading does not become a leader, so there is no election loop to get stuck
    in. The leader's event is always set and its map entry always removed on exit,
    including on exception and cancellation.

    The wait is bounded by the configured follower timeout, so a wedged leader
    (a hung thread, a lost task) can never strand followers indefinitely.
    """
    if not normalized_url:
        yield True  # nothing to key on: everyone is their own leader
        return

    waiting = _leaders.get(normalized_url)
    if waiting is not None:
        try:
            await asyncio.wait_for(waiting.wait(), timeout=_follower_timeout_s)
            log.debug("coalesced with in-flight download of %s", normalized_url)
        except TimeoutError:
            # The leader is wedged. Downloading ourselves is strictly better than
            # making the user wait on it forever.
            log.warning(
                "leader for %s did not finish within %.0fs; proceeding alone",
                normalized_url,
                _follower_timeout_s,
            )
        yield False
        return

    event = asyncio.Event()
    _leaders[normalized_url] = event
    try:
        yield True
    finally:
        _leaders.pop(normalized_url, None)
        event.set()


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
    global _semaphore, _bot_username, _max_per_user, _follower_timeout_s
    _semaphore = None
    _bot_username = None
    _max_per_user = 1
    _follower_timeout_s = DEFAULT_FOLLOWER_TIMEOUT_S
    _inflight.clear()
    # Wake anything still waiting before dropping the map, so a leaked follower from
    # a previous test can't hang on an event nobody will ever set.
    for event in _leaders.values():
        event.set()
    _leaders.clear()
