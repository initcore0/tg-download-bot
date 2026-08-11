"""yt-dlp wrapper. Blocking extraction runs in a worker thread via `asyncio.to_thread`.

Format selection is latency-first (ARCHITECTURE.md §5.1): prefer sources that are already
H.264 + AAC in MP4 so the pipeline can skip re-encoding entirely.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from tgdl.downloader.models import (
    ExtractionError,
    TransientExtractionError,
    UnsupportedUrlError,
)

log = logging.getLogger(__name__)

# Errors that mean "this link will never work", as opposed to transient failures.
_UNSUPPORTED_MARKERS = (
    "unsupported url",
    "is not a valid url",
    "no media found",
)

# Errors that are private/removed/absent content — permanent, but not "unsupported".
_PERMANENT_MARKERS = (
    "no video could be found",  # twitter: tweet exists but has no video
    "there is no video in this post",  # instagram: post exists but has no video
    "private video",
    "video unavailable",
    "this video is unavailable",
    "video has been removed",
    "account has been terminated",
    "video is not available",
    "removed by the user",
    "this post is unavailable",
    "no longer available",
    "requested content is not available",
    "age-restricted",
    "sign in to confirm your age",
    "members-only",
    "this live event",
)

# Errors that are worth retrying: throttling, bot-checks, network, transient 5xx.
# YouTube in particular returns these intermittently and clears on retry or a
# different player client.
_TRANSIENT_MARKERS = (
    "http error 403",
    "http error 429",
    "http error 5",  # 500/502/503/504
    "sign in to confirm you're not a bot",
    "confirm you're not a bot",
    "unable to download webpage",
    "unable to download api page",
    "read timed out",
    "connection reset",
    "connection timed out",
    "temporary failure",
    "the read operation timed out",
    "unable to download video data",
    "giving up after",
    "fragment",
    "unable to extract",  # often transient signature-extraction failures on YouTube
    "requested format is not available",
    "precondition check failed",
    "failed to extract any player response",
    "please try again",
)

# YouTube player clients tried in order when extraction keeps failing. Different
# clients dodge different bot-checks/throttles without needing cookies.
_YT_CLIENT_FALLBACKS: tuple[tuple[str, ...], ...] = (
    (),  # yt-dlp default
    ("android", "web"),
    ("ios",),
    ("tv",),
)

_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 1.5

# Optional cookies file (Netscape format), set once at startup. On datacenter IPs
# this is the reliable way past YouTube's "confirm you're not a bot" gate.
_COOKIES_FILE: Path | None = None


def configure(*, cookies_file: Path | None = None) -> None:
    """Set process-wide extraction options (called once from main)."""
    global _COOKIES_FILE
    if cookies_file is not None and Path(cookies_file).is_file():
        _COOKIES_FILE = Path(cookies_file)
        log.info("using YouTube cookies file: %s", _COOKIES_FILE)
    elif cookies_file is not None:
        log.warning("cookies file %s not found — continuing without cookies", cookies_file)
        _COOKIES_FILE = None
    else:
        _COOKIES_FILE = None


def build_format_selector(max_height: int) -> str:
    """yt-dlp format string, best (cheapest to serve) option first."""
    return (
        f"bv*[height<={max_height}][ext=mp4][vcodec^=avc1]+ba[ext=m4a]"
        f"/b[height<={max_height}][ext=mp4]"
        f"/bv*[height<={max_height}]+ba/b[height<={max_height}]"
        f"/b"
    )


def build_options(
    workdir: Path,
    *,
    max_height: int,
    playlist_items: str | None = None,
    youtube_clients: tuple[str, ...] = (),
) -> dict[str, Any]:
    """yt-dlp options: quiet, contained inside `workdir`, no writes elsewhere.

    `youtube_clients`, when non-empty, forces yt-dlp's YouTube player-client order —
    used by the retry loop to dodge client-specific throttling/bot-checks.
    """
    opts: dict[str, Any] = {
        "format": build_format_selector(max_height),
        "format_sort": [f"res:{max_height}", "codec:h264", "br"],
        "outtmpl": str(workdir / "%(id).60s.%(ext)s"),
        "paths": {"home": str(workdir), "temp": str(workdir)},
        "cachedir": str(workdir / ".ytdlp-cache"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": playlist_items is None,
        "restrictfilenames": True,
        "nopart": False,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "consoletitle": False,
        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "overwrites": True,
        "ignoreerrors": False,
        "merge_output_format": "mp4",
    }
    if playlist_items is not None:
        opts["playlist_items"] = playlist_items
        opts["noplaylist"] = False
    if youtube_clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(youtube_clients)}}
    if _COOKIES_FILE is not None:
        opts["cookiefile"] = str(_COOKIES_FILE)
    return opts


def _classify(exc: Exception) -> Exception:
    """Map a yt-dlp exception onto our error taxonomy.

    Order matters: transient (retryable) and permanent (private/removed) are checked
    before the generic buckets so a throttle isn't mislabeled "unsupported".
    """
    message = str(exc)
    lowered = message.lower()
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return TransientExtractionError(message)
    if any(marker in lowered for marker in _PERMANENT_MARKERS):
        return ExtractionError(message)
    if any(marker in lowered for marker in _UNSUPPORTED_MARKERS):
        return UnsupportedUrlError(message)
    # Unknown failures are treated as transient once — cheap insurance against
    # yt-dlp phrasings we haven't catalogued; the retry loop caps total attempts.
    return TransientExtractionError(message)


def _entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a playlist result into a list of leaf entries (single item -> [info])."""
    entries = info.get("entries")
    if not entries:
        return [info]
    flat: list[dict[str, Any]] = []
    for entry in entries:
        if not entry:
            continue
        flat.extend(_entries(entry) if entry.get("entries") else [entry])
    return flat


def downloaded_path(entry: dict[str, Any]) -> Path | None:
    """Best-effort resolution of the file yt-dlp actually wrote for `entry`."""
    requested = entry.get("requested_downloads") or []
    for item in requested:
        for key in ("filepath", "_filename", "filename"):
            value = item.get(key)
            if value and Path(value).exists():
                return Path(value)
    for key in ("filepath", "_filename", "filename"):
        value = entry.get(key)
        if value and Path(value).exists():
            return Path(value)
    return None


def _extract_sync(url: str, opts: dict[str, Any], *, download: bool) -> dict[str, Any]:
    """Blocking yt-dlp call — always invoked through `asyncio.to_thread`."""
    import yt_dlp  # imported lazily so tests can patch/skip it cheaply

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download)
    except Exception as exc:
        raise _classify(exc) from exc
    if not info:
        raise ExtractionError(f"yt-dlp returned no info for {url}")
    return info


async def extract(
    url: str,
    workdir: Path,
    *,
    max_height: int,
    playlist_items: str | None = None,
    download: bool = True,
    max_attempts: int = _MAX_ATTEMPTS,
) -> list[dict[str, Any]]:
    """Download `url` into `workdir`; return one info dict per downloaded item.

    Retries transient failures (throttling, bot-checks, transient network/5xx) with
    exponential backoff, cycling YouTube player clients between attempts. Permanent
    failures (UnsupportedUrlError / non-transient ExtractionError) raise immediately.
    Never raises a bare exception.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        clients = _YT_CLIENT_FALLBACKS[min(attempt, len(_YT_CLIENT_FALLBACKS) - 1)]
        opts = build_options(
            workdir,
            max_height=max_height,
            playlist_items=playlist_items,
            youtube_clients=clients,
        )
        try:
            info = await asyncio.to_thread(_extract_sync, url, opts, download=download)
            entries = _entries(info)
            if not entries:
                raise ExtractionError(f"no downloadable entries at {url}")
            if attempt:
                log.info("extraction of %s succeeded on attempt %d", url, attempt + 1)
            return entries
        except TransientExtractionError as exc:
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            delay = _BACKOFF_BASE_S * (2**attempt)
            log.warning(
                "transient extraction failure for %s (attempt %d/%d): %s — retrying in %.1fs",
                url,
                attempt + 1,
                max_attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
        except (UnsupportedUrlError, ExtractionError):
            raise  # permanent — do not retry

    # Exhausted retries: surface the last transient error (retryable message).
    assert last_exc is not None
    raise last_exc
