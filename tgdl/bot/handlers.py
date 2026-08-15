"""aiogram handlers: commands, private URL messages, group/channel mentions.

Request flow implemented here mirrors ARCHITECTURE.md §4:
  extract URL -> audit -> chat-action status -> file_id cache -> semaphore -> download
  -> send -> audit -> cleanup.
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    FSInputFile,
    InlineQuery,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultsButton,
    InputMediaPhoto,
    Message,
    ReactionTypeEmoji,
)
from aiogram.utils.chat_action import ChatActionSender

from tgdl import i18n
from tgdl.bot import alerts, responses, runtime
from tgdl.config import Settings
from tgdl.downloader import audio as audio_mod
from tgdl.downloader import service
from tgdl.downloader.models import DownloadError, MediaResult
from tgdl.storage import repo

log = logging.getLogger(__name__)

router = Router(name="tgdl")

#: Fallback URL matcher, used when tgdl.downloader.urls is not available (M1 stub).
_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)

#: Telegram accepts at most 10 items in a media group.
MEDIA_GROUP_LIMIT = 10

#: Per-request workdir name prefix; the startup sweep in main.py matches on it.
WORKDIR_PREFIX = "req-"

#: Put on the user's message while we work on it, and cleared when we're done.
ACK_EMOJI = "👀"

#: The audit `media_kind` for /mp3 rows. It cannot live in the frozen
#: `models.MediaKind` Literal, so the repo takes it as an explicit override.
AUDIO_MEDIA_KIND = "audio"

#: Cache/coalescing kinds for each flow. A video row and an audio row can share a
#: normalized URL, so neither may ever be served in the other's place.
VIDEO_CACHE_KINDS = ("video", "animation", "image")
AUDIO_CACHE_KINDS = (AUDIO_MEDIA_KIND,)

#: Prefix for the audio flow's coalescing key, for the same reason: /mp3 and a plain
#: link to the same video are two different downloads and must not gate each other.
AUDIO_GATE_PREFIX = "audio:"

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
    request_id: int | None,
    media: MediaResult,
    file_id: str | None,
    elapsed_s: float,
    *,
    file_ids: list[str] | None = None,
    cache_hit: bool = False,
    media_kind: str | None = None,
) -> None:
    if request_id is None:
        return
    try:
        await repo.mark_success(
            request_id=request_id,
            media=media,
            telegram_file_id=file_id,
            elapsed_s=elapsed_s,
            telegram_file_ids=file_ids,
            cache_hit=cache_hit,
            media_kind_override=media_kind,
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


async def _alert_failure(url: str, error: BaseException) -> None:
    """Tell the admin about a failure that suggests the *bot* is broken.

    Same contract as the audit wrappers: diagnostics must never break the flow they
    are diagnosing. `alerts` decides what is worth a message (most failures are not)
    and stays anonymous — only the link, platform, and error travel.
    """
    try:
        await alerts.report_failure(_safe_platform(url), error, url)
    except Exception:
        log.debug("admin alert failed", exc_info=True)


# ----------------------------------------------------------------- file_id cache
# The fastest possible download is the one we don't do: Telegram will re-send any
# file we already uploaded if we hand it back the file_id (ARCHITECTURE.md §6.1).
# Videos, animations and images all qualify; an image row is only replayed once it
# carries the full ordered file_id list, so a carousel comes back whole.


def _is_story_url(url: str) -> bool:
    """True for instagram.com/stories/... links, which expire and must never be cached.

    Mirrors `service._is_instagram_story` without the platform lookup: a false
    positive here only costs us a cache miss.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return path.strip("/").startswith("stories/")


async def _try_send_cached(
    message: Message, url: str, *, quote: bool
) -> tuple[list[str], MediaResult] | None:
    """Re-send previously uploaded media by file_id. Returns (file_ids, media) or None.

    Never raises: a repo error or a file_id Telegram has since forgotten just means
    a cache miss, and the caller downloads as usual.
    """
    try:
        if _is_story_url(url):
            return None

        # Audio rows are excluded deliberately: /mp3 and a plain link share one
        # normalized URL, and a music track is not what someone sending a video
        # link asked for.
        row = await repo.find_cached(_safe_normalized(url), media_kinds=VIDEO_CACHE_KINDS)
        if row is None:
            return None

        # The repo query already filters these out; belt-and-braces, because an
        # image row without its full list would ship a carousel as a single photo.
        if row.media_kind not in VIDEO_CACHE_KINDS:
            return None
        file_ids = repo.decode_file_ids(row)
        if not file_ids or (row.media_kind == "image" and not row.telegram_file_ids):
            return None

        duration = int(row.duration_s) if row.duration_s else None
        if row.media_kind == "video":
            sender = message.reply_video if quote else message.answer_video
            sent = await sender(
                file_ids[0],
                width=row.width,
                height=row.height,
                duration=duration,
                supports_streaming=True,
            )
        elif row.media_kind == "animation":
            sender = message.reply_animation if quote else message.answer_animation
            sent = await sender(
                file_ids[0], width=row.width, height=row.height, duration=duration
            )
        elif len(file_ids) > 1:
            # A carousel: Telegram re-groups its own files from their ids alone.
            group = [InputMediaPhoto(media=f) for f in file_ids[:MEDIA_GROUP_LIMIT]]
            sender = message.reply_media_group if quote else message.answer_media_group
            sent = await sender(group)
        else:
            sender = message.reply_photo if quote else message.answer_photo
            sent = await sender(file_ids[0])

        log.info("cache hit for %s (%s x%d)", url, row.media_kind, len(file_ids))
        media = MediaResult(
            path=Path("cached"),
            kind=row.media_kind,
            source_url=url,
            platform=row.platform or "other",
            filesize=row.filesize_bytes or 0,
            title=row.title,
            width=row.width,
            height=row.height,
            duration_s=row.duration_s,
            transcoded=bool(row.transcoded),
        )
        # Prefer the ids Telegram just handed back; fall back to the stored ones.
        return _extract_file_ids(sent) or file_ids, media
    except Exception:
        log.info("file_id cache unusable for %s; downloading instead", url, exc_info=True)
        return None


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


def _extract_file_ids(sent: Any) -> list[str]:
    """Every file_id in a send result, in order.

    A media group returns one Message per item, and the cache needs all of them —
    replaying a carousel from just the first id would silently drop the rest.
    """
    if sent is None:
        return []
    if isinstance(sent, (list, tuple)):
        return [f for item in sent if (f := _extract_file_id(item))]
    single = _extract_file_id(sent)
    return [single] if single else []


def _extract_file_id(sent: Any) -> str | None:
    """Pull the Telegram file_id out of a sent message (video/photo/animation/audio)."""
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
    audio = getattr(sent, "audio", None)
    if audio is not None:
        return getattr(audio, "file_id", None)
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
) -> list[str]:
    """Send all results; returns every file_id in send order (the cache's raw material).

    The first entry is the audit row's `telegram_file_id`; the whole list is what makes
    a carousel replayable from cache later (§6.1).
    """
    images = [r for r in results if r.kind == "image"]

    # Multiple images -> one media group (no captions).
    if len(results) > 1 and len(images) == len(results):
        group = [InputMediaPhoto(media=FSInputFile(r.path)) for r in images[:MEDIA_GROUP_LIMIT]]
        sender = message.reply_media_group if quote else message.answer_media_group
        sent = await sender(group)
        return _extract_file_ids(sent)

    file_ids: list[str] = []
    for media in results[:MEDIA_GROUP_LIMIT]:
        file_id = await _send_single(message, media, quote=quote)
        if file_id:
            file_ids.append(file_id)
    return file_ids


# --------------------------------------------------------------------- reaction ack
# A 👀 on the *user's own message* while we work, cleared when we're done. This is not
# a caption or branding on the delivered media (CLAUDE.md's plain-output rule stands) —
# it's an acknowledgment that the link was seen, on the message the user sent us.


async def _set_reaction(message: Message, emoji: str | None) -> None:
    """Set (or clear, with emoji=None) a reaction on the triggering message.

    Best-effort in every direction: channel posts are skipped (no from_user, and
    reaction permissions there are unreliable), and any API refusal — old message,
    missing permission, reactions disabled in the chat — is a debug line and nothing
    more. The arriving media or error reply is the real answer.
    """
    if message.from_user is None:
        return
    try:
        reaction = [ReactionTypeEmoji(emoji=emoji)] if emoji else []
        await message.react(reaction)
    except Exception:
        log.debug("react(%s) failed", emoji, exc_info=True)


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


async def _acknowledge(message: Message) -> None:
    """Immediate, neutral "I've seen it" feedback: a typing action plus the 👀 ack.

    Neutral by design — at this point we don't yet know whether the link is
    downloadable at all, or what it holds. Both signals are best-effort.
    """
    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:
        log.debug("send_chat_action failed", exc_info=True)

    await _set_reaction(message, ACK_EMOJI)


async def _run_download(
    message: Message, url: str, settings: Settings, *, quote: bool, started: float, locale: str
) -> None:
    """The download+send+audit cycle, run while holding a user slot. Never raises."""
    request_id = await _audit_create_request(message, url)

    # Immediate feedback even while queued on the semaphore. The media-specific
    # action ("sending a video…") is shown only after the download succeeds, in the
    # upload phase below — claiming it earlier reads as a broken promise.
    await _acknowledge(message)

    workdir: Path | None = None
    try:
        # Coalesce concurrent requests for the same link: the first is the leader and
        # downloads; anyone who arrives while it runs waits here and then finds the
        # leader's upload in the cache below. A follower holds neither the global
        # semaphore nor a workdir while it waits.
        async with runtime.coalesce(_safe_normalized(url)):
            # Already uploaded this link once? Re-send by file_id — no download, no
            # semaphore, no workdir. Cheap enough to run inside the user slot.
            cached = await _try_send_cached(message, url, quote=quote)
            if cached is not None:
                cached_file_ids, cached_media = cached
                await _audit_success(
                    request_id,
                    cached_media,
                    cached_file_ids[0] if cached_file_ids else None,
                    time.monotonic() - started,
                    file_ids=cached_file_ids,
                    cache_hit=True,
                )
                return

            # The semaphore caps concurrent *downloads* only: uploading to Telegram is
            # network-bound and shouldn't keep the next requester queued behind us.
            async with runtime.get_semaphore():
                base = Path(settings.download_dir)
                base.mkdir(parents=True, exist_ok=True)
                workdir = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=base))

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
                file_ids = await _send_results(message, results, quote=quote)

            await _audit_success(
                request_id,
                results[0],
                file_ids[0] if file_ids else None,
                time.monotonic() - started,
                file_ids=file_ids,
            )

    except DownloadError as err:
        log.info("download failed for %s: %s", url, err)
        # A caller-supplied custom message is shown verbatim; otherwise translate.
        text = err.custom_message or i18n.t(err.message_key, locale)
        await _reply(message, text, quote=quote)
        await _audit_failure(request_id, err, time.monotonic() - started)
        await _alert_failure(url, err)
    except Exception as err:
        # Top-level guard: an unexpected error must never kill the polling loop.
        log.exception("unexpected error handling %s", url)
        await _reply(message, responses.generic_error(locale), quote=quote)
        await _audit_failure(request_id, err, time.monotonic() - started)
        await _alert_failure(url, err)
    finally:
        # The media (or the error reply) is the real answer, so the "I'm on it"
        # marker comes off either way.
        await _set_reaction(message, None)
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------- audio flow
# /mp3 mirrors the video cycle (slot -> audit -> ack -> cache -> coalesce -> semaphore
# -> download -> send -> audit -> cleanup) but keeps its own cache and coalescing keys:
# the same link yields a video row and an audio row, and neither may stand in for the
# other. The payload is m4a — see tgdl/downloader/audio.py for why.


def _audio_media_result(url: str, audio: audio_mod.AudioResult) -> MediaResult:
    """Adapt an AudioResult to the MediaResult `mark_success` takes.

    `kind` is a lie here by construction — the frozen MediaKind Literal has no audio
    member — so every caller pairs this with `media_kind=AUDIO_MEDIA_KIND`, which the
    repo writes instead of `media.kind`.
    """
    return MediaResult(
        path=audio.path,
        kind="video",  # placeholder; overridden by media_kind=AUDIO_MEDIA_KIND
        source_url=url,
        platform=_safe_platform(url),
        filesize=audio.filesize,
        title=audio.title,
        duration_s=audio.duration_s,
    )


async def _try_send_cached_audio(
    message: Message, url: str, *, quote: bool
) -> tuple[list[str], MediaResult] | None:
    """Re-send a previously uploaded audio track by file_id. Never raises."""
    try:
        if _is_story_url(url):
            return None

        row = await repo.find_cached(_safe_normalized(url), media_kinds=AUDIO_CACHE_KINDS)
        if row is None or row.media_kind != AUDIO_MEDIA_KIND:
            return None
        file_ids = repo.decode_file_ids(row)
        if not file_ids:
            return None

        sender = message.reply_audio if quote else message.answer_audio
        sent = await sender(
            file_ids[0],
            title=row.title,
            duration=int(row.duration_s) if row.duration_s else None,
        )
        log.info("audio cache hit for %s", url)
        media = MediaResult(
            path=Path("cached"),
            kind="video",  # placeholder; see _audio_media_result
            source_url=url,
            platform=row.platform or "other",
            filesize=row.filesize_bytes or 0,
            title=row.title,
            duration_s=row.duration_s,
        )
        return _extract_file_ids(sent) or file_ids, media
    except Exception:
        log.info("audio file_id cache unusable for %s; downloading instead", url, exc_info=True)
        return None


async def process_audio_url(message: Message, url: str, settings: Settings) -> None:
    """Full audio download+send+audit cycle for one URL. Never raises."""
    quote = _is_group(message)
    locale = _locale(message)
    started = time.monotonic()

    with runtime.user_slot(_user_key(message)) as granted:
        if not granted:
            await _reply(message, responses.busy_per_user(locale), quote=quote)
            return
        await _run_audio_download(
            message, url, settings, quote=quote, started=started, locale=locale
        )


async def _run_audio_download(
    message: Message, url: str, settings: Settings, *, quote: bool, started: float, locale: str
) -> None:
    """The audio download+send+audit cycle, run while holding a user slot. Never raises."""
    request_id = await _audit_create_request(message, url)
    await _acknowledge(message)

    workdir: Path | None = None
    try:
        # Prefixed key: an /mp3 request must not wait behind (or be satisfied by) a
        # plain video download of the same link.
        async with runtime.coalesce(AUDIO_GATE_PREFIX + _safe_normalized(url)):
            cached = await _try_send_cached_audio(message, url, quote=quote)
            if cached is not None:
                cached_file_ids, cached_media = cached
                await _audit_success(
                    request_id,
                    cached_media,
                    cached_file_ids[0] if cached_file_ids else None,
                    time.monotonic() - started,
                    file_ids=cached_file_ids,
                    cache_hit=True,
                    media_kind=AUDIO_MEDIA_KIND,
                )
                return

            async with runtime.get_semaphore():
                base = Path(settings.download_dir)
                base.mkdir(parents=True, exist_ok=True)
                workdir = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=base))

                async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
                    audio = await audio_mod.download_audio(
                        url,
                        workdir,
                        max_size_bytes=settings.max_file_size_bytes,
                        timeout_s=settings.download_timeout_s,
                    )

            # The kind is known from the start here, so the upload action can be
            # specific immediately.
            async with ChatActionSender.upload_voice(bot=message.bot, chat_id=message.chat.id):
                file_ids = await _send_audio(message, audio, quote=quote)

            await _audit_success(
                request_id,
                _audio_media_result(url, audio),
                file_ids[0] if file_ids else None,
                time.monotonic() - started,
                file_ids=file_ids,
                media_kind=AUDIO_MEDIA_KIND,
            )

    except DownloadError as err:
        log.info("audio download failed for %s: %s", url, err)
        text = err.custom_message or i18n.t(err.message_key, locale)
        await _reply(message, text, quote=quote)
        await _audit_failure(request_id, err, time.monotonic() - started)
        await _alert_failure(url, err)
    except Exception as err:
        log.exception("unexpected error handling audio for %s", url)
        await _reply(message, responses.generic_error(locale), quote=quote)
        await _audit_failure(request_id, err, time.monotonic() - started)
        await _alert_failure(url, err)
    finally:
        await _set_reaction(message, None)
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


async def _send_audio(
    message: Message, audio: audio_mod.AudioResult, *, quote: bool
) -> list[str]:
    """Send one audio file as plain media (title + duration, no caption)."""
    sender = message.reply_audio if quote else message.answer_audio
    sent = await sender(
        FSInputFile(audio.path),
        title=audio.title,
        performer=audio.performer,
        duration=int(audio.duration_s) if audio.duration_s else None,
    )
    return _extract_file_ids(sent)


# --------------------------------------------------------------------------- handlers


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(responses.start_text(runtime.get_bot_username(), _locale(message)))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(responses.help_text(runtime.get_bot_username(), _locale(message)))


@router.message(Command("stats"))
async def cmd_stats(message: Message, settings: Settings) -> None:
    """Admin-only ops summary. Silent for everyone else — including that it exists.

    Two conditions, both required: a private chat, and the configured admin id
    (ADMIN_USER_ID, 0 = disabled). The id is compared in memory and never stored,
    so this stays compatible with the anonymity design (ARCHITECTURE.md §6).
    """
    admin_id = settings.admin_user_id
    if not admin_id or message.chat.type != ChatType.PRIVATE:
        return
    if message.from_user is None or message.from_user.id != admin_id:
        return

    try:
        data = await repo.stats()
    except Exception:
        log.exception("/stats: repo.stats failed")
        await _reply(message, responses.generic_error(_locale(message)), quote=False)
        return

    await _reply(message, responses.stats_text(data), quote=False)


@router.message(Command(commands=["mp3", "audio"]))
async def cmd_mp3(message: Message, settings: Settings) -> None:
    """Audio-only download. Works in private chats and groups.

    No mention is required in groups: an explicit command already says the message is
    for this bot, which is exactly what the mention rule exists to establish.
    """
    if message.chat.type not in (ChatType.PRIVATE, *GROUP_CHAT_TYPES):
        return
    url = extract_first_url(message_text(message))
    if not url:
        await _reply(message, responses.mp3_usage(_locale(message)), quote=_is_group(message))
        return
    await process_audio_url(message, url, settings)


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


# ------------------------------------------------------------------------ inline mode
# Inline serves the file_id cache and nothing else. Telegram gives an inline query a
# few seconds and no way to show progress, so starting a download here would time out
# and look broken; a miss instead offers a one-tap route into private chat, where the
# normal flow warms the cache for everyone. Enable once via BotFather (/setinline).

#: Cache lifetimes Telegram may reuse our answer for. A hit is stable (the same
#: file_ids for everyone), a miss is short so a warmed link becomes available quickly.
INLINE_CACHE_TIME_HIT_S = 300
INLINE_CACHE_TIME_MISS_S = 30

#: Deep-link payload on the switch-to-PM button.
INLINE_START_PARAMETER = "inline"


def _inline_results(row: Any, file_ids: list[str]) -> list[Any]:
    """Build the inline result(s) for one cached audit row.

    A gallery becomes up to MEDIA_GROUP_LIMIT separate photo results — inline mode has
    no media groups, so the carousel is offered item by item. Result ids combine the
    row id and the index so they stay unique within an answer.
    """
    kind = row.media_kind
    row_id = getattr(row, "id", 0)
    title = row.title or None

    if kind == "video":
        return [
            InlineQueryResultCachedVideo(
                id=f"{row_id}-0", video_file_id=file_ids[0], title=title or "Video"
            )
        ]
    if kind == "animation":
        return [InlineQueryResultCachedMpeg4Gif(id=f"{row_id}-0", mpeg4_file_id=file_ids[0])]
    if kind == AUDIO_MEDIA_KIND:
        return [InlineQueryResultCachedAudio(id=f"{row_id}-0", audio_file_id=file_ids[0])]
    if kind == "image":
        return [
            InlineQueryResultCachedPhoto(id=f"{row_id}-{index}", photo_file_id=file_id)
            for index, file_id in enumerate(file_ids[:MEDIA_GROUP_LIMIT])
        ]
    return []


async def _audit_inline_hit(url: str, media_kind: str, file_ids: list[str]) -> None:
    """Record an inline cache hit. Never breaks the answer; never identifies anyone.

    `chat_type="inline"` is the same kind of coarse, non-identifying context the
    message flows store — an inline query carries no chat at all.
    """
    try:
        row = await repo.create_request(
            chat_type="inline",
            url=url,
            normalized_url=_safe_normalized(url),
            platform=_safe_platform(url),
        )
    except Exception:
        log.exception("audit: inline create_request failed")
        return

    await _audit_success(
        getattr(row, "id", None),
        MediaResult(
            path=Path("cached"),
            kind="video",  # placeholder; media_kind below is what gets written
            source_url=url,
            platform=_safe_platform(url),
            filesize=0,
        ),
        file_ids[0] if file_ids else None,
        0.0,
        file_ids=file_ids,
        cache_hit=True,
        media_kind=media_kind,
    )


@router.inline_query()
async def handle_inline_query(query: InlineQuery) -> None:
    """Answer an inline query from the file_id cache, or offer to warm it."""
    locale = i18n.locale_of(getattr(query.from_user, "language_code", None))
    url = extract_first_url(query.query)

    results: list[Any] = []
    file_ids: list[str] = []
    row = None
    if url and not _is_story_url(url):
        try:
            row = await repo.find_cached(_safe_normalized(url))
        except Exception:
            log.info("inline cache lookup failed for %s", url, exc_info=True)
            row = None

    if row is not None:
        file_ids = repo.decode_file_ids(row)
        # An image row without its full ordered list would offer a carousel's first
        # photo alone — same guard as the message flow (§6.1).
        if file_ids and not (row.media_kind == "image" and not row.telegram_file_ids):
            results = _inline_results(row, file_ids)

    if not results:
        # Nothing to serve. Zero results plus a button into private chat, where the
        # normal download flow will cache it for the next inline query.
        await query.answer(
            [],
            cache_time=INLINE_CACHE_TIME_MISS_S,
            is_personal=False,
            button=InlineQueryResultsButton(
                text=i18n.t("inline.no_cache", locale),
                start_parameter=INLINE_START_PARAMETER,
            ),
        )
        return

    # Results are identical for every user (nothing user-specific is involved), which
    # is both consistent with the anonymity design and lets Telegram cache them.
    await query.answer(results, cache_time=INLINE_CACHE_TIME_HIT_S, is_personal=False)
    if url:
        await _audit_inline_hit(url, row.media_kind, file_ids)
