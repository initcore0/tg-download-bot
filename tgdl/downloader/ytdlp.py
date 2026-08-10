"""yt-dlp wrapper. Blocking extraction runs in a worker thread via `asyncio.to_thread`.

Format selection is latency-first (ARCHITECTURE.md §5.1): prefer sources that are already
H.264 + AAC in MP4 so the pipeline can skip re-encoding entirely.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from tgdl.downloader.models import ExtractionError, UnsupportedUrlError

log = logging.getLogger(__name__)

# Errors that mean "this link will never work", as opposed to transient failures.
_UNSUPPORTED_MARKERS = (
    "unsupported url",
    "no video formats found",
    "is not a valid url",
    "unable to extract",
    "no media found",
)


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
) -> dict[str, Any]:
    """yt-dlp options: quiet, contained inside `workdir`, no writes elsewhere."""
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
        opts.pop("noplaylist", None)
        opts["noplaylist"] = False
    return opts


def _classify(exc: Exception) -> Exception:
    """Map a yt-dlp exception onto our error taxonomy."""
    message = str(exc)
    lowered = message.lower()
    if any(marker in lowered for marker in _UNSUPPORTED_MARKERS):
        return UnsupportedUrlError(message)
    return ExtractionError(message)


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
) -> list[dict[str, Any]]:
    """Download `url` into `workdir`; return one info dict per downloaded item.

    Raises UnsupportedUrlError / ExtractionError — never a bare exception.
    """
    opts = build_options(workdir, max_height=max_height, playlist_items=playlist_items)
    info = await asyncio.to_thread(_extract_sync, url, opts, download=download)
    entries = _entries(info)
    if not entries:
        raise ExtractionError(f"no downloadable entries at {url}")
    return entries


async def probe_info(url: str, workdir: Path, *, max_height: int) -> dict[str, Any]:
    """Metadata-only extraction (no download), used to detect image galleries."""
    opts = build_options(workdir, max_height=max_height)
    opts["skip_download"] = True
    opts["extract_flat"] = False
    return await asyncio.to_thread(_extract_sync, url, opts, download=False)


def looks_like_image_entry(entry: dict[str, Any]) -> bool:
    """True when yt-dlp describes this entry as a still image rather than a video."""
    if entry.get("_type") == "image":
        return True
    ext = (entry.get("ext") or "").lower()
    if ext in {"jpg", "jpeg", "png", "webp", "heic"}:
        return True
    vcodec = (entry.get("vcodec") or "").lower()
    acodec = (entry.get("acodec") or "").lower()
    return vcodec == "none" and acodec == "none"
