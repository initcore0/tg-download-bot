"""SQLAlchemy ORM model: DownloadRequest.

A single, anonymous audit table. There is intentionally no user model — see the
DownloadRequest docstring and README "Privacy" for why. Schema per ARCHITECTURE.md §6.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    """Timezone-aware UTC now (SQLite has no native tz, so we normalize on write/read)."""
    return datetime.now(UTC)


class TZDateTime(TypeDecorator):
    """UTC-aware datetime for SQLite.

    SQLite has no timezone type and hands back naive datetimes, so we coerce to UTC on
    the way in and re-attach UTC on the way out. Callers always see aware datetimes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base for all storage models."""


class DownloadRequest(Base):
    """One anonymous, audited download request and its outcome.

    Deliberately stores NOTHING that identifies the requester: no Telegram user id,
    username, names, chat id, or message id. Only the coarse `chat_type`
    (private/group/channel) is kept, for usage-shape analytics. The point of this table
    is the links + performance metadata (latency, size, transcode) to drive a future
    popular-link cache — never who asked.
    """

    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Coarse, non-identifying: "private" | "group" | "supergroup" | "channel".
    chat_type: Mapped[str] = mapped_column(String(32), nullable=False)

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    normalized_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    platform: Mapped[str | None] = mapped_column(String(64), default=None)

    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(128), default=None)
    error_message: Mapped[str | None] = mapped_column(String(2048), default=None)

    media_kind: Mapped[str | None] = mapped_column(String(16), default=None)
    title: Mapped[str | None] = mapped_column(String(1024), default=None)
    filesize_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    duration_s: Mapped[float | None] = mapped_column(Float, default=None)
    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    transcoded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Future dedup/cache key: re-send by file_id instead of re-downloading.
    telegram_file_id: Mapped[str | None] = mapped_column(String(256), default=None)

    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    elapsed_s: Mapped[float | None] = mapped_column(Float, default=None)

    __table_args__ = (
        Index("ix_requests_normalized_url", "normalized_url"),
        Index("ix_requests_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DownloadRequest id={self.id} status={self.status} url={self.url!r}>"
