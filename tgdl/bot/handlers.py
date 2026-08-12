"""aiogram handlers: commands, private URL messages, group/channel mentions.

Request flow implemented here mirrors ARCHITECTURE.md §4:
  extract URL -> audit -> chat-action status -> semaphore -> download -> send -> audit -> cleanup.
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, InputMediaPhoto, Message
from aiogram.utils.chat_action import ChatActionSender

from tgdl import i18n
from tgdl.bot import responses, runtime
from tgdl.config import Settings
from tgdl.downloader import service
from tgdl.downloader.models import DownloadError, MediaResult
from tgdl.storage import repo

log = logging.getLogger(__name__)

router = Router(name="tgdl")

#: Fallback URL matcher, used when tgdl.downloader.urls is not available (M1 stub).
_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)

#: Telegram accepts at most 10 items in a media group.
MEDIA_GROUP_LIMIT = 10

GROUP_CHAT_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


# --------------------------------------------------------------------------- helpers


def extract_first_url(text: str | None) -> str | None:
    """Return the first http(s) URL in `text`, or None.

    Prefers `tgdl.downloader.urls.extract_urls` (Agent A) and falls back to a local
    regex while that module is still a stub.
    """
    if not text:
        return None
    try:
        from tgdl.downloader import urls as urls_mod

        found = urls_mod.extract_urls(text)
        if found:
            return found[0]
        return None
    except NotImplementedError:
        pass
    except Exception:  # pragma: no cover - defensive: never let URL parsing kill a request
        log.exception("extract_urls failed; falling back to regex")

    match = _URL_RE.search(text)
    return match.group(0) if match else None


def _safe_platform(url: str) -> str:
    """Best-effort platform slug; audit metadata must never break a download."""
    try:
        from tgdl.downloader import urls as urls_mod

        return urls_mod.detect_platform(url)
    except Exception:
        log.debug("detect_platform failed for %s", url, exc_info=True)
        return "other"


def _safe_normalized(url: str) -> str:
    """Best-effort normalized URL; falls back to the raw URL."""
    try:
        from tgdl.downloader import urls as urls_mod

        return urls_mod.normalize_url(url)
    except Exception:
        log.debug("normalize_url failed for %s", url, exc_info=True)
        return url


def message_text(message: Message) -> str | None:
    """Text or caption of a message, whichever is present."""
    return message.text or message.caption


def mentions_bot(message: Message, username: str | None) -> bool:
    """True if the message text/caption mentions @username (case-insensitive)."""
    if not username:
        return False
    text = message_text(message)
    if not text:
        return False
    return f"@{username}".lower() in text.lower()


def _is_group(message: Message) -> bool:
    return message.chat.type in GROUP_CHAT_TYPES


# ------------------------------------------------------------------- audit wrappers
# Audit must never break the user flow (ARCHITECTURE.md §6 / CLAUDE.md). Rows are
# anonymous: we record only the link, platform, coarse chat_type, and performance
# metadata — no user, chat, or message identifiers (see README "Privacy").


async def _audit_create_request(message: Message, url: str) -> int | None:
    try:
        row = await repo.create_request(
            chat_type=str(message.chat.type),
            url=url,
            normalized_url=_safe_normalized(url),
            platform=_safe_platform(url),
        )
        return getattr(row, "id", None)
    except Exception:
        log.exception("audit: create_request failed")
        return None


async def _audit_success(
    request_id: int | None, media: MediaResult, file_id: str | None, elapsed_s: float
) -> None:
    if request_id is None:
        return
    try:
        await repo.mark_success(
            request_id=request_id, media=media, telegram_file_id=file_id, elapsed_s=elapsed_s
        )
    except Exception:
        log.exception("audit: mark_success failed")


async def _audit_failure(request_id: int | None, error: BaseException, elapsed_s: float) -> None:
    if request_id is None:
        return
    try:
        await repo.mark_failure(request_id=request_id, error=error, elapsed_s=elapsed_s)
    except Exception:
        log.exception("audit: mark_failure failed")


# ---------------------------------------------------------------------- send helpers


async def _reply(message: Message, text: str, *, quote: bool) -> Message | None:
    """Answer or reply-to depending on chat type; never raises."""
    try:
        if quote:
            return await message.reply(text)
        return await message.answer(text)
    except Exception:
        log.exception("failed to send text message")
        return None


def _extract_file_id(sent: Any) -> str | None:
    """Pull the Telegram file_id out of a sent message (video/photo/animation)."""
    if sent is None:
        return None
    if isinstance(sent, (list, tuple)):
        sent = sent[0] if sent else None
        if sent is None:
            return None
    video = getattr(sent, "video", None)
    if video is not None:
        return getattr(video, "file_id", None)
    animation = getattr(sent, "animation", None)
    if animation is not None:
        return getattr(animation, "file_id", None)
    photo = getattr(sent, "photo", None)
    if photo:
        return getattr(photo[-1], "file_id", None)
    return None


async def _send_single(message: Message, media: MediaResult, *, quote: bool) -> str | None:
    """Send one MediaResult as plain media (no caption). Returns its file_id."""
    file = FSInputFile(media.path)

    if media.kind == "video":
        sender = message.reply_video if quote else message.answer_video
        sent = await sender(
            file,
            width=media.width,
            height=media.height,
            duration=int(media.duration_s) if media.duration_s else None,
            supports_streaming=True,
        )
    elif media.kind == "animation":
        sender = message.reply_animation if quote else message.answer_animation
        sent = await sender(
            file, width=media.width, height=media.height,
            duration=int(media.duration_s) if media.duration_s else None,
        )
    else:  # image
        sender = message.reply_photo if quote else message.answer_photo
        sent = await sender(file)

    return _extract_file_id(sent)


async def _send_results(
    message: Message, results: list[MediaResult], *, quote: bool
) -> str | None:
    """Send all results; returns the file_id of the first sent item (audit key)."""
    images = [r for r in results if r.kind == "image"]

    # Multiple images -> one media group (no captions).
    if len(results) > 1 and len(images) == len(results):
        group = [InputMediaPhoto(media=FSInputFile(r.path)) for r in images[:MEDIA_GROUP_LIMIT]]
        sender = message.reply_media_group if quote else message.answer_media_group
        sent = await sender(group)
        return _extract_file_id(sent)

    first_file_id: str | None = None
    for media in results[:MEDIA_GROUP_LIMIT]:
        file_id = await _send_single(message, media, quote=quote)
        if first_file_id is None:
            first_file_id = file_id
    return first_file_id


# ------------------------------------------------------------------------- main flow


def _user_key(message: Message) -> int:
    """In-memory rate-limit key: the sender id, or chat id for channel posts.

    Used ONLY for the transient per-user concurrency guard in `runtime` — it lives in
    RAM for the duration of a download and is never written to the database. The audit
    layer stores nothing derived from it.
    """
    if message.from_user is not None:
        return message.from_user.id
    return message.chat.id


def _locale(message: Message) -> str:
    """Pick the reply language from the sender's Telegram language_code.

    Used only to render this reply; never stored (keeps the bot anonymous). Channel
    posts have no from_user, so they fall back to the default locale (English).
    """
    code = message.from_user.language_code if message.from_user is not None else None
    return i18n.locale_of(code)


async def process_url(message: Message, url: str, settings: Settings) -> None:
    """Full download+send+audit cycle for one URL. Never raises."""
    quote = _is_group(message) or message.chat.type == ChatType.CHANNEL
    locale = _locale(message)
    started = time.monotonic()

    # Per-user abuse guard: refuse a new download while this user already has one
    # in flight, so a single user cannot monopolize the global download slots.
    with runtime.user_slot(_user_key(message)) as granted:
        if not granted:
            await _reply(message, responses.busy_per_user(locale), quote=quote)
            return
        await _run_download(message, url, settings, quote=quote, started=started, locale=locale)


async def _run_download(
    message: Message, url: str, settings: Settings, *, quote: bool, started: float, locale: str
) -> None:
    """The download+send+audit cycle, run while holding a user slot. Never raises."""
    request_id = await _audit_create_request(message, url)

    # Immediate feedback even while queued on the semaphore — but a *neutral*
    # "typing…" only: we don't yet know whether the link is downloadable at all,
    # or whether it holds a video or photos. Claiming "sending a video…" and then
    # failing (or sending a photo) reads as a broken promise. The media-specific
    # action is shown after the download succeeds, in the upload phase below.
    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:
        log.debug("send_chat_action failed", exc_info=True)

    workdir: Path | None = None
    try:
        async with runtime.get_semaphore():
            base = Path(settings.download_dir)
            base.mkdir(parents=True, exist_ok=True)
            workdir = Path(tempfile.mkdtemp(prefix="req-", dir=base))

            async with ChatActionSender.typing(
                bot=message.bot, chat_id=message.chat.id
            ):
                results = await service.download_media(
                    url,
                    workdir,
                    max_size_bytes=settings.max_file_size_bytes,
                    max_height=settings.max_height,
                    timeout_s=settings.download_timeout_s,
                )

            if not results:
                raise DownloadError("downloader returned no results")

            # Upload phase: the download succeeded and we know the media kind, so
            # now (and only now) show the matching "sending a photo/video…" action.
            action_sender = (
                ChatActionSender.upload_photo
                if all(r.kind == "image" for r in results)
                else ChatActionSender.upload_video
            )
            async with action_sender(bot=message.bot, chat_id=message.chat.id):
                file_id = await _send_results(message, results, quote=quote)

        await _audit_success(request_id, results[0], file_id, time.monotonic() - started)

    except DownloadError as err:
        log.info("download failed for %s: %s", url, err)
        # A caller-supplied custom message is shown verbatim; otherwise translate.
        text = err.custom_message or i18n.t(err.message_key, locale)
        await _reply(message, text, quote=quote)
        await _audit_failure(request_id, err, time.monotonic() - started)
    except Exception as err:
        # Top-level guard: an unexpected error must never kill the polling loop.
        log.exception("unexpected error handling %s", url)
        await _reply(message, responses.generic_error(locale), quote=quote)
        await _audit_failure(request_id, err, time.monotonic() - started)
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- handlers


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(responses.start_text(runtime.get_bot_username(), _locale(message)))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(responses.help_text(runtime.get_bot_username(), _locale(message)))


@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_private(message: Message, settings: Settings) -> None:
    """Private chat: any message containing an http(s) URL is processed."""
    url = extract_first_url(message_text(message))
    if not url:
        return
    await process_url(message, url, settings)


@router.message(F.chat.type.in_(GROUP_CHAT_TYPES))
async def handle_group(message: Message, settings: Settings) -> None:
    """Groups/supergroups: only act when the bot is mentioned by @username."""
    if not mentions_bot(message, runtime.get_bot_username()):
        return
    url = extract_first_url(message_text(message))
    if not url:
        return
    await process_url(message, url, settings)


@router.channel_post()
async def handle_channel_post(message: Message, settings: Settings) -> None:
    """Channel posts: only act when the bot is mentioned by @username."""
    if not mentions_bot(message, runtime.get_bot_username()):
        return
    url = extract_first_url(message_text(message))
    if not url:
        return
    await process_url(message, url, settings)
