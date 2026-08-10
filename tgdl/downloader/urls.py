"""URL helpers: extraction from message text, platform detection, normalization.

STUB — implemented by Agent A (M1). Signatures FROZEN.
"""
from __future__ import annotations


def extract_urls(text: str) -> list[str]:
    """Return all http(s) URLs found in `text`, in order of appearance."""
    raise NotImplementedError("M1 — Agent A")


def detect_platform(url: str) -> str:
    """Map a URL to a platform slug: youtube|tiktok|instagram|twitter|twitch|pinterest|other."""
    raise NotImplementedError("M1 — Agent A")


def normalize_url(url: str) -> str:
    """Canonical form used as the future dedup/audit key.

    Lowercase host, drop fragments and tracking params (utm_*, si, feature, igsh, ...),
    expand youtu.be/<id> to youtube.com/watch?v=<id>, strip trailing slash.
    """
    raise NotImplementedError("M1 — Agent A")
