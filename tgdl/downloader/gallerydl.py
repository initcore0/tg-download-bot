"""gallery-dl wrapper: the image-fetch fallback engine.

yt-dlp is a video extractor — image-only posts (Instagram photos, image tweets,
Pinterest pins) and story images are out of its reach. When yt-dlp reports that a
link has no video, the service layer retries the URL through gallery-dl, which
specializes in exactly those. Runs as an async subprocess so the event loop stays
free, mirroring the ffmpeg helpers.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from tgdl.downloader.models import (
    DownloadError,
    ExtractionError,
    TransientExtractionError,
    UnsupportedUrlError,
)

log = logging.getLogger(__name__)

#: Subdirectory of the request workdir that receives gallery-dl output. Keeping it
#: separate means collected files can never be confused with yt-dlp downloads.
DEST_SUBDIR = "gallery"

_HTTP_TIMEOUT_S = 30


class AuthRequiredError(ExtractionError):
    """The target content is behind a login (stories, private/restricted posts)."""

    message_key = "error.login_required"
    user_message = (
        "That content requires a logged-in account (stories and some posts are "
        "login-only). The bot needs a cookies file configured to fetch it."
    )


def build_command(
    url: str, dest: Path, *, max_items: int, cookies_file: Path | None = None
) -> list[str]:
    """gallery-dl argv: quiet, contained inside `dest`, no reads of user config.

    `cookies_file` is resolved per request by `tgdl.downloader.cookies` — the
    Instagram jar is only attached for stories or an explicit login retry.
    """
    args = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--quiet",
        "--no-colors",
        "--no-input",
        "--no-part",
        "--config-ignore",
        "--directory",
        str(dest),
        "--cache-file",
        str(dest / ".gdl-cache.sqlite3"),
        "--http-timeout",
        str(_HTTP_TIMEOUT_S),
        "--range",
        f"1-{max_items}",
    ]
    if cookies_file is not None:
        args += ["--cookies", str(cookies_file)]
    args.append(url)
    return args


# Stderr markers, checked in order. gallery-dl's messages vary per extractor, so we
# match loosely; anything unrecognized becomes a generic ExtractionError.
_AUTH_MARKERS = (
    "login required",
    "authorizationerror",
    "authentication",
    "401",
    "unauthorized",
    "account required",
    "logged-in",
    "cookies needed",
    # Instagram phrases the wall as a redirect, e.g.
    # "HTTP redirect to login page (https://www.instagram.com/accounts/login/)".
    "redirect to login",
    "login page",
    "accounts/login",
)
_UNSUPPORTED_MARKERS = (
    "unsupported url",
    "no suitable extractor",
)
_TRANSIENT_MARKERS = (
    "429",
    "rate limit",
    "too many requests",
    "timed out",
    "connection reset",
    "temporary failure",
    "http error 5",
    "502",
    "503",
)


def classify_failure(stderr: str, url: str) -> DownloadError:
    """Map gallery-dl stderr onto our error taxonomy (auth checked first)."""
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return AuthRequiredError(f"gallery-dl auth failure for {url}: {stderr[:300]}")
    if any(marker in lowered for marker in _UNSUPPORTED_MARKERS):
        return UnsupportedUrlError(f"gallery-dl does not support {url}")
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return TransientExtractionError(f"gallery-dl transient failure for {url}: {stderr[:300]}")
    return ExtractionError(f"gallery-dl failed for {url}: {stderr[:300]}")


async def _run(args: list[str]) -> tuple[int, str]:
    """Run gallery-dl, returning (returncode, stderr). Kills the process on cancel."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ExtractionError(f"could not start gallery-dl: {exc}") from exc
    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # The outer asyncio.timeout()/task cancel must not leave an orphan process.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stderr.decode(errors="replace")


def collect_files(dest: Path) -> list[Path]:
    """Downloaded media files inside `dest`, in download (mtime) order."""
    if not dest.is_dir():
        return []
    files = [
        p
        for p in dest.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix != ".part"
    ]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


async def fetch(
    url: str, workdir: Path, *, max_items: int = 10, cookies_file: Path | None = None
) -> list[Path]:
    """Download the media behind `url` via gallery-dl into `workdir`/gallery.

    Returns the downloaded files in post order (images, and videos for stories).
    Raises a DownloadError subclass on failure or when nothing was produced.
    """
    dest = Path(workdir) / DEST_SUBDIR
    dest.mkdir(parents=True, exist_ok=True)

    code, stderr = await _run(
        build_command(url, dest, max_items=max_items, cookies_file=cookies_file)
    )
    files = collect_files(dest)

    if not files:
        if code != 0 or stderr.strip():
            raise classify_failure(stderr, url)
        raise ExtractionError(f"gallery-dl produced no files for {url}")
    if code != 0:
        # Partial success (some items failed): keep what we got, like galleries do.
        log.warning("gallery-dl exited %d for %s but produced %d files", code, url, len(files))
    return files[:max_items]
