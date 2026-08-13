"""Anonymous audit repository: download requests only.

There is no user table — requests are recorded without any identifying data (see
DownloadRequest and README "Privacy"). We store links + performance metadata to drive
a future popular-link cache, never who asked.

Audit failures must never break the user flow: `mark_success` / `mark_failure` swallow
and log their own exceptions. `create_request` may raise — the caller decides how to
degrade.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tgdl.storage import db as _db
from tgdl.storage.models import DownloadRequest

if TYPE_CHECKING:
    from tgdl.downloader.models import MediaResult

logger = logging.getLogger(__name__)

# Module-level state, managed by init_db/close_db.
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None

_MAX_ERROR_MESSAGE = 2000
_MAX_TITLE = 1000

#: Kinds that may be replayed from the file_id cache. Videos and animations are single
#: files, so their `telegram_file_id` is the whole story; images need the full
#: `telegram_file_ids` list, since replaying a carousel from one id would drop the rest.
CACHEABLE_MEDIA_KINDS = ("video", "animation", "image")

#: Image rows are only replayable once they carry the full ordered file_id list.
_LIST_REQUIRED_MEDIA_KINDS = ("image",)

#: How long a Telegram file_id is trusted before we re-download instead.
DEFAULT_CACHE_MAX_AGE_DAYS = 30

#: Audit rows older than this are deleted on startup.
DEFAULT_RETENTION_DAYS = 90

#: A row still 'pending' after this long belongs to a crashed run, not a live download.
DEFAULT_STALE_PENDING_S = 3600


def _session() -> AsyncSession:
    if _sessionmaker is None:
        raise RuntimeError("Database not initialized — call init_db() first.")
    return _sessionmaker()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit]


async def init_db(database_path: Path) -> None:
    """Create engine/session factory, enable WAL, create tables. Idempotent."""
    global _engine, _sessionmaker

    if _engine is not None:
        # Already initialized for this path; just make sure the schema is present.
        await _db.create_all(_engine)
        return

    engine = _db.create_engine(database_path)
    await _db.create_all(engine)

    _engine = engine
    _sessionmaker = _db.create_session_factory(engine)
    logger.info("Storage initialized at %s", Path(database_path).resolve())


async def close_db() -> None:
    """Dispose the engine. Safe to call when not initialized."""
    global _engine, _sessionmaker

    engine, _engine = _engine, None
    _sessionmaker = None
    if engine is not None:
        await engine.dispose()


async def create_request(
    chat_type: str,
    url: str,
    normalized_url: str,
    platform: str,
) -> DownloadRequest:
    """Insert an anonymous request row with status='pending'; return it (id populated).

    `chat_type` is the only contextual field kept, and it is coarse (private/group/
    channel) — no user, chat, or message identifiers are stored.
    """
    async with _session() as session:
        async with session.begin():
            request = DownloadRequest(
                chat_type=chat_type,
                url=url,
                normalized_url=normalized_url,
                platform=platform,
                status="pending",
                created_at=_utcnow(),
            )
            session.add(request)

        return request


def encode_file_ids(file_ids: list[str] | None) -> str | None:
    """JSON-encode an ordered file_id list for storage; None/empty stores NULL."""
    cleaned = [f for f in (file_ids or []) if f]
    return json.dumps(cleaned) if cleaned else None


def decode_file_ids(row: DownloadRequest | Any) -> list[str]:
    """Ordered file_ids of an audit row, falling back to the single-id column.

    Never raises: a row written by an older build (or with a corrupt JSON blob) simply
    degrades to whatever `telegram_file_id` holds.
    """
    raw = getattr(row, "telegram_file_ids", None)
    if raw:
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            logger.debug("unreadable telegram_file_ids on row %s", getattr(row, "id", None))
            decoded = None
        if isinstance(decoded, list):
            file_ids = [f for f in decoded if isinstance(f, str) and f]
            if file_ids:
                return file_ids

    single = getattr(row, "telegram_file_id", None)
    return [single] if single else []


async def find_cached(
    normalized_url: str, *, max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS
) -> DownloadRequest | None:
    """Most recent successful, still-fresh row for `normalized_url` that can be replayed.

    Returns None when nothing qualifies. Video and animation rows need only their
    `telegram_file_id`; image rows additionally need the full `telegram_file_ids`
    list, so a carousel is replayed whole or not at all (see CACHEABLE_MEDIA_KINDS).
    Like `create_request` this may raise — the caller decides how to degrade (the bot
    falls through to a normal download).
    """
    if not normalized_url:
        return None

    cutoff = _utcnow() - timedelta(days=max_age_days)
    async with _session() as session:
        row = (
            await session.execute(
                select(DownloadRequest)
                .where(
                    DownloadRequest.normalized_url == normalized_url,
                    DownloadRequest.status == "success",
                    DownloadRequest.telegram_file_id.is_not(None),
                    DownloadRequest.media_kind.in_(CACHEABLE_MEDIA_KINDS),
                    DownloadRequest.created_at >= cutoff,
                    or_(
                        DownloadRequest.media_kind.not_in(_LIST_REQUIRED_MEDIA_KINDS),
                        DownloadRequest.telegram_file_ids.is_not(None),
                    ),
                )
                .order_by(DownloadRequest.created_at.desc(), DownloadRequest.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    return row


async def find_cached_file_id(
    normalized_url: str, *, max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS
) -> DownloadRequest | None:
    """Backwards-compatible alias for `find_cached` (the name predates image caching)."""
    return await find_cached(normalized_url, max_age_days=max_age_days)


async def prune_audit(
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    stale_pending_s: int = DEFAULT_STALE_PENDING_S,
) -> dict[str, int]:
    """Housekeeping: drop expired rows and close out rows a crashed run left pending.

    Returns {"deleted": n, "stale_pending": n}. Called at startup; the caller keeps
    going if it raises.
    """
    now = _utcnow()
    async with _session() as session, session.begin():
        deleted = (
            await session.execute(
                delete(DownloadRequest).where(
                    DownloadRequest.created_at < now - timedelta(days=retention_days)
                )
            )
        ).rowcount
        stale = (
            await session.execute(
                update(DownloadRequest)
                .where(
                    DownloadRequest.status == "pending",
                    DownloadRequest.created_at < now - timedelta(seconds=stale_pending_s),
                )
                .values(status="failed", error_class="StaleRequest", completed_at=now)
            )
        ).rowcount

    return {"deleted": deleted or 0, "stale_pending": stale or 0}


async def mark_success(
    request_id: int,
    media: MediaResult,
    telegram_file_id: str | None,
    elapsed_s: float,
    *,
    telegram_file_ids: list[str] | None = None,
    cache_hit: bool = False,
) -> None:
    """Set status='success' plus media metadata. Must swallow+log its own errors.

    `telegram_file_ids` is every file_id sent for this request, in order — a carousel
    needs all of them to be replayable. `telegram_file_id` stays the first one.
    `cache_hit` marks a row that was served from the cache instead of downloaded.
    """
    try:
        async with _session() as session, session.begin():
            request = await session.get(DownloadRequest, request_id)
            if request is None:
                logger.warning("mark_success: request %s not found", request_id)
                return

            request.status = "success"
            request.media_kind = media.kind
            request.title = _truncate(media.title, _MAX_TITLE)
            request.filesize_bytes = media.filesize
            request.duration_s = media.duration_s
            request.width = media.width
            request.height = media.height
            request.transcoded = bool(media.transcoded)
            request.telegram_file_id = telegram_file_id
            request.telegram_file_ids = encode_file_ids(
                telegram_file_ids if telegram_file_ids is not None else [telegram_file_id]
            )
            request.cache_hit = bool(cache_hit)
            request.platform = request.platform or media.platform
            request.completed_at = _utcnow()
            request.elapsed_s = elapsed_s
    except Exception:
        logger.exception("Failed to record success for request %s", request_id)


async def mark_failure(request_id: int, error: BaseException, elapsed_s: float) -> None:
    """Set status='failed', error_class, error_message. Must swallow+log its own errors."""
    try:
        async with _session() as session, session.begin():
            request = await session.get(DownloadRequest, request_id)
            if request is None:
                logger.warning("mark_failure: request %s not found", request_id)
                return

            request.status = "failed"
            request.error_class = type(error).__name__
            request.error_message = _truncate(str(error), _MAX_ERROR_MESSAGE)
            request.completed_at = _utcnow()
            request.elapsed_s = elapsed_s
    except Exception:
        logger.exception("Failed to record failure for request %s", request_id)


async def stats() -> dict[str, Any]:
    """Small helper for ops: total/succeeded/failed requests (no user counts exist)."""
    async with _session() as session:
        rows = (
            await session.execute(
                select(DownloadRequest.status, func.count()).group_by(DownloadRequest.status)
            )
        ).all()

    by_status = {status: count for status, count in rows}
    return {
        "requests": sum(by_status.values()),
        "pending": by_status.get("pending", 0),
        "success": by_status.get("success", 0),
        "failed": by_status.get("failed", 0),
    }
