"""Cross-module data contracts for the download pipeline.

FROZEN by the architect — build agents must not modify this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MediaKind = Literal["video", "image", "animation"]


@dataclass(slots=True)
class MediaResult:
    """One downloaded, Telegram-ready media file."""

    path: Path
    kind: MediaKind
    source_url: str
    platform: str  # e.g. "youtube", "tiktok", "instagram", "twitter", "twitch", "pinterest", "other"
    filesize: int  # bytes
    title: str | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    transcoded: bool = False
    elapsed_s: float = 0.0


class DownloadError(Exception):
    """Base class for download failures.

    The downloader layer stays locale-agnostic: each error carries a stable
    `message_key` (translated by the bot at send time via `tgdl.i18n`). `user_message`
    is the English text — a fallback for any non-bot caller or logging.
    """

    message_key: str = "error.generic"
    user_message: str = "Sorry, I couldn't download that."

    def __init__(self, detail: str = "", user_message: str | None = None):
        super().__init__(detail or self.__class__.user_message)
        self.detail = detail
        # A caller-supplied message is an explicit literal override; when present it
        # wins over translation (the bot cannot translate arbitrary custom text).
        self.custom_message = user_message
        if user_message is not None:
            self.user_message = user_message


class UnsupportedUrlError(DownloadError):
    message_key = "error.unsupported_url"
    user_message = "I don't recognize a downloadable link in that message."


class ExtractionError(DownloadError):
    message_key = "error.extraction"
    user_message = "I couldn't fetch media from that link. It may be private, deleted, or unsupported."


class TransientExtractionError(ExtractionError):
    """A retryable extraction failure (throttling, bot-check, transient network/5xx).

    Subclasses ExtractionError so existing `except ExtractionError` handling still
    catches it; the service layer retries it before giving up.
    """

    message_key = "error.transient"
    user_message = (
        "That service is rate-limiting or temporarily unavailable. "
        "Please try again in a moment."
    )


class MediaTooLargeError(DownloadError):
    message_key = "error.too_large"
    user_message = "That video is too large to send over Telegram (50 MB bot limit), even after compression."


class TranscodeError(DownloadError):
    message_key = "error.transcode"
    user_message = "I downloaded the media but couldn't convert it to a Telegram-friendly format."


class DownloadTimeoutError(DownloadError):
    message_key = "error.timeout"
    user_message = "That download took too long and was cancelled. Please try a shorter video."
