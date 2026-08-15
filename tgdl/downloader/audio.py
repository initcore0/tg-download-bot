"""Audio-only extraction for the /mp3 command (ARCHITECTURE.md §5.6).

The command is called /mp3 because that is what people ask for; the payload is
**m4a**. Telegram plays m4a natively and the vast majority of sources already carry
an AAC track, so shipping m4a means a stream copy where mp3 would mean a pointless
re-encode on every single request — the latency-first rule (CLAUDE.md) applies to
audio exactly as it does to video.

`AudioResult` is deliberately its own small dataclass rather than a `MediaResult`:
`models.py` is frozen and its `MediaKind` Literal has no "audio" member, so reusing
it would mean labelling every audio file a video.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tgdl.downloader import cookies, ytdlp
from tgdl.downloader import transcode as tc
from tgdl.downloader.models import (
    DownloadError,
    DownloadTimeoutError,
    ExtractionError,
    MediaTooLargeError,
    UnsupportedUrlError,
)
from tgdl.downloader.urls import detect_platform, is_safe_public_url

log = logging.getLogger(__name__)

#: Latency-first selector: an m4a stream needs no conversion at all, and `bestaudio`
#: is the fallback for sources that don't offer one.
AUDIO_FORMAT = "ba[ext=m4a]/bestaudio"

#: Extensions whose contents are already an AAC-in-MP4 track — send as-is.
_PASSTHROUGH_EXTENSIONS = {".m4a", ".aac", ".mp4"}

#: Absolute floor for the yt-dlp early-abort ceiling on audio. Audio tracks are small,
#: so a modest bound is plenty of headroom for any real track while still stopping a
#: hostile server from streaming an unbounded stream to exhaust the disk.
_MIN_AUDIO_CEILING_BYTES = 100 * 1024 * 1024


@dataclass(slots=True)
class AudioResult:
    """One downloaded, Telegram-ready audio file."""

    path: Path
    title: str | None = None
    duration_s: float | None = None
    filesize: int = 0
    performer: str | None = None


async def download_audio(
    url: str,
    workdir: Path,
    *,
    max_size_bytes: int,
    timeout_s: int = 300,
) -> AudioResult:
    """Download the audio track at `url` into `workdir` as a Telegram-ready m4a.

    Mirrors `service.download_media`'s contract: same SSRF guard, same timeout
    wrapper, and only `DownloadError` subclasses ever escape. There is no size retry
    ladder — an audio track over the cap is a `MediaTooLargeError`, since re-encoding
    an already-small stream buys far less than it does for video.
    """
    if not url or not url.strip():
        raise UnsupportedUrlError("empty url")

    # SSRF guard: never let the extractor fetch internal/link-local hosts. DNS
    # resolution is blocking, so run it off the event loop.
    if not await asyncio.to_thread(is_safe_public_url, url.strip()):
        raise UnsupportedUrlError(f"blocked non-public or unresolvable host: {url}")

    workdir = Path(workdir)
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError(f"cannot create workdir {workdir}: {exc}") from exc

    try:
        async with asyncio.timeout(timeout_s):
            return await _run_pipeline(url, workdir, max_size_bytes=max_size_bytes)
    except TimeoutError as exc:
        raise DownloadTimeoutError(f"timed out after {timeout_s}s downloading {url}") from exc
    except DownloadError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("unexpected failure downloading audio from %s", url)
        raise DownloadError(f"unexpected error: {exc!r}") from exc


async def _run_pipeline(url: str, workdir: Path, *, max_size_bytes: int) -> AudioResult:
    """Extract the audio stream, then make it an m4a — copying whenever possible."""
    platform = detect_platform(url)
    entries = await ytdlp.extract(
        url,
        workdir,
        max_height=0,  # unused: format_override replaces the video selector entirely
        cookies_file=cookies.resolve(platform),
        format_override=AUDIO_FORMAT,
        max_filesize_bytes=max(3 * max_size_bytes, _MIN_AUDIO_CEILING_BYTES),
    )

    entry = entries[0]
    path = ytdlp.downloaded_path(entry)
    if path is None:
        raise ExtractionError(f"no audio file produced for {url}")

    final_path = await _ensure_m4a(path)
    size = final_path.stat().st_size
    if size > max_size_bytes:
        raise MediaTooLargeError(
            f"{final_path.name} is {size} bytes (cap {max_size_bytes})"
        )

    return AudioResult(
        path=final_path,
        title=_clean(entry.get("track") or entry.get("title")),
        duration_s=_duration(entry),
        filesize=size,
        performer=_clean(entry.get("artist") or entry.get("uploader")),
    )


async def _ensure_m4a(path: Path) -> Path:
    """Return an m4a for `path`, converting only when the source isn't already one.

    A `ba[ext=m4a]` hit is already exactly what we want to send, so the common case
    costs nothing. Everything else (opus/webm from YouTube, mp3 from a podcast host)
    goes through ffmpeg, which itself stream-copies an AAC track when it finds one.
    """
    if path.suffix.lower() in _PASSTHROUGH_EXTENSIONS:
        info = await tc.probe(path)
        if info.audio_codec in ("aac", "mp4a") and not info.has_video:
            log.debug("%s is already a Telegram-ready audio track; sending as-is", path.name)
            return path
    return await tc.to_m4a(path)


def _clean(value: Any) -> str | None:
    """Trim a yt-dlp metadata string down to something worth putting on a message."""
    if not isinstance(value, str):
        return None
    return value.strip()[:200] or None


def _duration(entry: dict[str, Any]) -> float | None:
    try:
        duration = float(entry.get("duration"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None
