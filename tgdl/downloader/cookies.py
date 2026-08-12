"""Per-platform cookie routing.

Cookies are credentials. Sending them where they aren't needed creates risk and
nothing else: an Instagram session attached to every public-reel fetch looks like
scraping and gets the account flagged, and YouTube has no use for Instagram's
cookies at all. So each platform gets its own jar, resolved per request:

- youtube            -> the YOUTUBE jar, else the generic one
- instagram, story   -> the INSTAGRAM jar, else generic (stories are login-only)
- instagram, other   -> anonymous. The login jar is offered only via
                        ``use_login=True``, which the service uses for a single
                        retry after an anonymous attempt hits a login wall.
- everything else    -> the generic jar

Paths are set once at startup via `configure()` (main materializes env-var
content into temp files first).
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_GENERIC: Path | None = None
_YOUTUBE: Path | None = None
_INSTAGRAM: Path | None = None


def _validated(path: Path | None, label: str) -> Path | None:
    if path is None:
        return None
    path = Path(path)
    if not path.is_file():
        log.warning("%s cookies file %s not found — ignoring", label, path)
        return None
    log.info("%s cookies configured", label)
    return path


def configure(
    *,
    generic: Path | None = None,
    youtube: Path | None = None,
    instagram: Path | None = None,
) -> None:
    """Set the per-platform cookie jars (called once from main)."""
    global _GENERIC, _YOUTUBE, _INSTAGRAM
    _GENERIC = _validated(generic, "generic")
    _YOUTUBE = _validated(youtube, "youtube")
    _INSTAGRAM = _validated(instagram, "instagram")


def instagram_login() -> Path | None:
    """The jar that can authenticate to Instagram, if any."""
    return _INSTAGRAM or _GENERIC


def resolve(platform: str, *, story: bool = False, use_login: bool = False) -> Path | None:
    """The cookies file to use for one request, or None for anonymous."""
    if platform == "youtube":
        return _YOUTUBE or _GENERIC
    if platform == "instagram":
        if story or use_login:
            return instagram_login()
        return None  # anonymous by policy: don't flag the account on public posts
    return _GENERIC
