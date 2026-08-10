"""Download orchestration: extract -> download -> ensure Telegram compatibility.

`download_media` is the only entry point the bot layer uses; its signature is FROZEN.
See ARCHITECTURE.md §5 for the pipeline spec.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from tgdl.downloader import transcode as tc
from tgdl.downloader import ytdlp
from tgdl.downloader.models import (
    DownloadError,
    DownloadTimeoutError,
    ExtractionError,
    MediaResult,
    MediaTooLargeError,
    UnsupportedUrlError,
)
from tgdl.downloader.urls import detect_platform

log = logging.getLogger(__name__)

# Platforms whose posts are frequently image carousels rather than single videos.
GALLERY_PLATFORMS = {"instagram", "pinterest"}
MAX_GALLERY_ITEMS = 10
RETRY_HEIGHT = 480


async def download_media(
    url: str,
    workdir: Path,
    *,
    max_size_bytes: int,
    max_height: int = 720,
    timeout_s: int = 300,
) -> list[MediaResult]:
    """Download media at `url` into `workdir` and return Telegram-ready files.

    - Usually returns exactly one MediaResult; image carousels return up to 10.
    - Files in the returned results live inside `workdir`; the caller owns cleanup.
    - Raises a DownloadError subclass on any failure (never a bare Exception).
    - Total wall time is bounded by `timeout_s` (raise DownloadTimeoutError).
    """
    started = time.monotonic()
    if not url or not url.strip():
        raise UnsupportedUrlError("empty url")

    workdir = Path(workdir)
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError(f"cannot create workdir {workdir}: {exc}") from exc

    try:
        async with asyncio.timeout(timeout_s):
            results = await _run_pipeline(
                url,
                workdir,
                max_size_bytes=max_size_bytes,
                max_height=max_height,
            )
    except TimeoutError as exc:
        raise DownloadTimeoutError(f"timed out after {timeout_s}s downloading {url}") from exc
    except DownloadError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("unexpected failure downloading %s", url)
        raise DownloadError(f"unexpected error: {exc!r}") from exc

    elapsed = time.monotonic() - started
    for result in results:
        result.elapsed_s = elapsed
    return results


async def _run_pipeline(
    url: str,
    workdir: Path,
    *,
    max_size_bytes: int,
    max_height: int,
) -> list[MediaResult]:
    """Extract, then post-process every downloaded entry into a MediaResult."""
    platform = detect_platform(url)
    playlist_items = f"1-{MAX_GALLERY_ITEMS}" if platform in GALLERY_PLATFORMS else None

    entries = await ytdlp.extract(
        url,
        workdir,
        max_height=max_height,
        playlist_items=playlist_items,
    )

    results: list[MediaResult] = []
    errors: list[str] = []
    for entry in entries[:MAX_GALLERY_ITEMS]:
        path = ytdlp.downloaded_path(entry)
        if path is None:
            errors.append(f"no file produced for entry {entry.get('id')!r}")
            continue
        try:
            results.append(
                await _process_entry(
                    entry,
                    path,
                    source_url=url,
                    platform=platform,
                    max_size_bytes=max_size_bytes,
                    max_height=max_height,
                )
            )
        except DownloadError as exc:
            # One bad item in a gallery should not sink the whole request.
            errors.append(f"{path.name}: {exc.detail or exc}")
            if len(entries) == 1:
                raise

    if not results:
        raise ExtractionError(
            "no media could be produced from that link" + (f" ({'; '.join(errors)})" if errors else "")
        )
    if errors:
        log.warning("partial gallery download for %s: %s", url, "; ".join(errors))
    return results


async def _process_entry(
    entry: dict[str, Any],
    path: Path,
    *,
    source_url: str,
    platform: str,
    max_size_bytes: int,
    max_height: int,
) -> MediaResult:
    """Turn one downloaded file into a Telegram-ready MediaResult."""
    title = entry.get("title") or entry.get("description") or None
    if isinstance(title, str):
        title = title.strip()[:200] or None

    source_ext = path.suffix.lower()
    info = await tc.probe(path)

    if info.is_image:
        return await _process_image(
            path, info, source_url=source_url, platform=platform, title=title
        )

    final_path, transcoded = await _ensure_compatible(path, info, max_height=max_height)
    final_info = info if final_path == path and not transcoded else await tc.probe(final_path)

    final_path, transcoded, final_info = await _enforce_size_cap(
        final_path,
        final_info,
        transcoded=transcoded,
        max_size_bytes=max_size_bytes,
    )

    kind = "animation" if tc.is_animation(final_info, source_ext) else "video"
    return MediaResult(
        path=final_path,
        kind=kind,
        source_url=source_url,
        platform=platform,
        filesize=final_path.stat().st_size,
        title=title,
        width=final_info.width,
        height=final_info.height,
        duration_s=final_info.duration_s,
        transcoded=transcoded,
    )


async def _process_image(
    path: Path,
    info: tc.MediaInfo,
    *,
    source_url: str,
    platform: str,
    title: str | None,
) -> MediaResult:
    """Images pass through; webp is converted to jpg for Telegram compatibility."""
    final_path = path
    converted = False
    if path.suffix.lower() in {".webp", ".heic", ".bmp"}:
        final_path = await tc.convert_image(path)
        converted = True
        info = await tc.probe(final_path)

    return MediaResult(
        path=final_path,
        kind="image",
        source_url=source_url,
        platform=platform,
        filesize=final_path.stat().st_size,
        title=title,
        width=info.width,
        height=info.height,
        duration_s=None,
        transcoded=converted,
    )


async def _ensure_compatible(
    path: Path, info: tc.MediaInfo, *, max_height: int
) -> tuple[Path, bool]:
    """Apply the cheapest step that makes the file Telegram-ready.

    Returns (path, transcoded). A remux does not count as a transcode: nothing was re-encoded.
    """
    decision = tc.decide(info)
    too_tall = info.height is not None and info.height > max_height

    if decision == tc.Decision.TRANSCODE or (too_tall and decision != tc.Decision.PASSTHROUGH):
        return await tc.transcode(path, max_height=max_height), True
    if too_tall:
        # Codecs and container are fine, but the frame is larger than we want to ship.
        return await tc.transcode(path, max_height=max_height), True
    if decision == tc.Decision.REMUX:
        return await tc.remux(path), False
    return path, False


async def _enforce_size_cap(
    path: Path,
    info: tc.MediaInfo,
    *,
    transcoded: bool,
    max_size_bytes: int,
) -> tuple[Path, bool, tc.MediaInfo]:
    """Keep the result under `max_size_bytes`, with one bitrate-targeted 480p retry."""
    size = path.stat().st_size
    if size <= max_size_bytes:
        return path, transcoded, info

    duration = info.duration_s or 0.0
    if duration <= 0:
        raise MediaTooLargeError(
            f"{path.name} is {size} bytes (cap {max_size_bytes}) and has no known duration"
        )

    audio_bitrate = tc.target_audio_bitrate(max_size_bytes, duration)
    bitrate = tc.target_video_bitrate(max_size_bytes, duration, audio_bitrate)
    log.info(
        "size cap retry: %s is %.1f MB, re-encoding at %dp / %d kbps video + %d kbps audio",
        path.name,
        size / 1024 / 1024,
        RETRY_HEIGHT,
        bitrate // 1000,
        audio_bitrate // 1000,
    )
    retry_path = await tc.transcode(
        path,
        max_height=RETRY_HEIGHT,
        video_bitrate=bitrate,
        audio_bitrate=audio_bitrate,
        drop_audio=not info.has_audio,
    )
    retry_size = retry_path.stat().st_size
    if retry_size > max_size_bytes:
        raise MediaTooLargeError(
            f"{path.name} still {retry_size} bytes after 480p retry (cap {max_size_bytes})"
        )
    return retry_path, True, await tc.probe(retry_path)
