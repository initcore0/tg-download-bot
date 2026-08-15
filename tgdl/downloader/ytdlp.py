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
from tgdl.downloader.urls import detect_platform

log = logging.getLogger(__name__)

# Errors that mean "this link will never work", as opposed to transient failures.
_UNSUPPORTED_MARKERS = (
    "unsupported url",
    "is not a valid url",
    "no media found",
)

# Errors that mean "this post exists but holds no video" — image-only posts.
# Permanent for yt-dlp, but the service layer retries these through the
# gallery-dl image fallback, so they must never be classified as transient
# (which would waste ~15s of retries before the fallback can run).
_NO_VIDEO_MARKERS = (
    "there is no video in this post",  # instagram: post exists but has no video
    "no video could be found",  # twitter: tweet exists but has no video
    "no video formats found",
    "no videos found",
    "does not contain a video",
)

# Errors that are private/removed content — permanent, but not "unsupported".
_PERMANENT_MARKERS = (
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

# First retry attempt (0-based) that drops cookies on YouTube. A logged-in session
# makes YouTube serve web clients SABR-only streams (no classic formats -> yt-dlp
# reports "Requested format is not available"), and yt-dlp skips the alternate
# player clients that don't support cookies — so with cookies attached, every
# fallback attempt degenerates into the same doomed configuration. Dropping the
# jar for the later rungs restores the anonymous android/ios/tv coverage.
_YT_ANONYMOUS_FROM_ATTEMPT = 2

_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 1.5


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
    cookies_file: Path | None = None,
    format_override: str | None = None,
    max_filesize_bytes: int | None = None,
) -> dict[str, Any]:
    """yt-dlp options: quiet, contained inside `workdir`, no writes elsewhere.

    `youtube_clients`, when non-empty, forces yt-dlp's YouTube player-client order —
    used by the retry loop to dodge client-specific throttling/bot-checks.
    `cookies_file` is resolved per request by `tgdl.downloader.cookies` so each
    platform only ever sees its own jar.
    `format_override` replaces the video format selector (and its video-oriented
    format_sort) wholesale — the audio path asks for a bare audio stream and would
    otherwise inherit a selector that only ever yields video.
    `max_filesize_bytes`, when set, is a hard early-abort ceiling: yt-dlp refuses to
    start (or aborts) a download whose declared size exceeds it, so a hostile server
    cannot stream a huge file to exhaust disk before the post-download size cap runs.
    It is deliberately a generous multiple of the send cap (computed by the caller) so
    it never rejects a legitimately-sized media file — it only bounds pathological ones.
    """
    opts: dict[str, Any] = {
        "format": format_override or build_format_selector(max_height),
        "format_sort": [] if format_override else [f"res:{max_height}", "codec:h264", "br"],
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
    }
    # Only meaningful when a video and an audio stream have to be muxed together;
    # an audio-only request never merges, and forcing mp4 there just confuses yt-dlp.
    if not format_override:
        opts["merge_output_format"] = "mp4"
    if playlist_items is not None:
        opts["playlist_items"] = playlist_items
        opts["noplaylist"] = False
    if youtube_clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(youtube_clients)}}
    if cookies_file is not None:
        opts["cookiefile"] = str(cookies_file)
    if max_filesize_bytes is not None:
        opts["max_filesize"] = max_filesize_bytes
    return opts


def _classify(exc: Exception) -> Exception:
    """Map a yt-dlp exception onto our error taxonomy.

    Order matters: transient (retryable) and permanent (private/removed) are checked
    before the generic buckets so a throttle isn't mislabeled "unsupported".
    """
    message = str(exc)
    lowered = message.lower()
    if any(marker in lowered for marker in _NO_VIDEO_MARKERS):
        return ExtractionError(message)
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

    from tgdl.downloader import ytdlp_patches

    ytdlp_patches.apply()
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
    cookies_file: Path | None = None,
    format_override: str | None = None,
    max_filesize_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Download `url` into `workdir`; return one info dict per downloaded item.

    Retries transient failures (throttling, bot-checks, transient network/5xx) with
    exponential backoff, cycling YouTube player clients between attempts. Permanent
    failures (UnsupportedUrlError / non-transient ExtractionError) raise immediately.
    Never raises a bare exception.

    `format_override` swaps the format selector — the audio path reuses this whole
    retry ladder rather than growing a second copy of it.
    `max_filesize_bytes`, when set, is passed straight through to `build_options` as a
    hard early-abort disk-exhaustion ceiling (see there).
    """
    is_youtube = detect_platform(url) == "youtube"
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        clients = _YT_CLIENT_FALLBACKS[min(attempt, len(_YT_CLIENT_FALLBACKS) - 1)]
        attempt_cookies = cookies_file
        if is_youtube and cookies_file is not None and attempt >= _YT_ANONYMOUS_FROM_ATTEMPT:
            attempt_cookies = None
            log.info(
                "attempt %d for %s: retrying anonymously (cookies limit YouTube "
                "to SABR-only web formats)",
                attempt + 1,
                url,
            )
        opts = build_options(
            workdir,
            max_height=max_height,
            playlist_items=playlist_items,
            youtube_clients=clients,
            cookies_file=attempt_cookies,
            format_override=format_override,
            max_filesize_bytes=max_filesize_bytes,
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
