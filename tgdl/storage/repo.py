"""Audit repository: users + download requests.

STUB — implemented by Agent C (M3). Signatures FROZEN.
Audit failures must never break the user flow: callers may fire-and-forget;
implementations should log exceptions rather than let them propagate where noted.
See ARCHITECTURE.md §6 for the schema.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tgdl.downloader.models import MediaResult
    from tgdl.storage.models import DownloadRequest, User


async def init_db(database_path: Path) -> None:
    """Create engine/session factory, enable WAL, create tables. Idempotent."""
    raise NotImplementedError("M3 — Agent C")


async def close_db() -> None:
    """Dispose the engine. Safe to call when not initialized."""
    raise NotImplementedError("M3 — Agent C")


async def get_or_create_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> "User":
    """Upsert by telegram_id; refresh names, bump last_seen_at and request_count."""
    raise NotImplementedError("M3 — Agent C")


async def create_request(
    user_id: int | None,
    chat_id: int,
    chat_type: str,
    message_id: int | None,
    url: str,
    normalized_url: str,
    platform: str,
) -> "DownloadRequest":
    """Insert a request row with status='pending'; return it (id populated)."""
    raise NotImplementedError("M3 — Agent C")


async def mark_success(
    request_id: int,
    media: "MediaResult",
    telegram_file_id: str | None,
    elapsed_s: float,
) -> None:
    """Set status='success' plus media metadata. Must swallow+log its own errors."""
    raise NotImplementedError("M3 — Agent C")


async def mark_failure(request_id: int, error: BaseException, elapsed_s: float) -> None:
    """Set status='failed', error_class, error_message. Must swallow+log its own errors."""
    raise NotImplementedError("M3 — Agent C")


async def stats() -> dict[str, Any]:
    """Small helper for ops: total users, total/succeeded/failed requests."""
    raise NotImplementedError("M3 — Agent C")
