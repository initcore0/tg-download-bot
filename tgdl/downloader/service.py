"""Download orchestration: extract -> download -> ensure Telegram compatibility.

STUB — implemented by Agent A (M1). The signature below is FROZEN.
See ARCHITECTURE.md §5 for the full pipeline spec.
"""
from __future__ import annotations

from pathlib import Path

from tgdl.downloader.models import MediaResult


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
    raise NotImplementedError("M1 — Agent A")
