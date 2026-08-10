"""Audit repository: users + download requests.

Signatures FROZEN (see ARCHITECTURE.md §6).

Audit failures must never break the user flow: `mark_success` / `mark_failure` swallow
and log their own exceptions. `get_or_create_user` / `create_request` may raise — the
caller decides how to degrade.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tgdl.storage import db as _db
from tgdl.storage.models import DownloadRequest, User

if TYPE_CHECKING:
    from tgdl.downloader.models import MediaResult

logger = logging.getLogger(__name__)

# Module-level state, managed by init_db/close_db.
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None

_MAX_ERROR_MESSAGE = 2000
_MAX_TITLE = 1000


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


async def get_or_create_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> User:
    """Upsert by telegram_id; refresh names, bump last_seen_at and request_count."""
    now = _utcnow()
    async with _session() as session:
        async with session.begin():
            user = (
                await session.execute(select(User).where(User.telegram_id == telegram_id))
            ).scalar_one_or_none()

            if user is None:
                user = User(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    first_seen_at=now,
                    last_seen_at=now,
                    request_count=1,
                )
                session.add(user)
            else:
                # Profile fields can change at any time — always refresh.
                user.username = username
                user.first_name = first_name
                user.last_name = last_name
                user.last_seen_at = now
                user.request_count = (user.request_count or 0) + 1

        return user


async def create_request(
    user_id: int | None,
    chat_id: int,
    chat_type: str,
    message_id: int | None,
    url: str,
    normalized_url: str,
    platform: str,
) -> DownloadRequest:
    """Insert a request row with status='pending'; return it (id populated)."""
    async with _session() as session:
        async with session.begin():
            request = DownloadRequest(
                user_id=user_id,
                chat_id=chat_id,
                chat_type=chat_type,
                message_id=message_id,
                url=url,
                normalized_url=normalized_url,
                platform=platform,
                status="pending",
                created_at=_utcnow(),
            )
            session.add(request)

        return request


async def mark_success(
    request_id: int,
    media: MediaResult,
    telegram_file_id: str | None,
    elapsed_s: float,
) -> None:
    """Set status='success' plus media metadata. Must swallow+log its own errors."""
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
    """Small helper for ops: total users, total/succeeded/failed requests."""
    async with _session() as session:
        users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        rows = (
            await session.execute(
                select(DownloadRequest.status, func.count()).group_by(DownloadRequest.status)
            )
        ).all()

    by_status = {status: count for status, count in rows}
    return {
        "users": users,
        "requests": sum(by_status.values()),
        "pending": by_status.get("pending", 0),
        "success": by_status.get("success", 0),
        "failed": by_status.get("failed", 0),
    }
