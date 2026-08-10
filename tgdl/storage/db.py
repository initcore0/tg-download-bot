"""Async SQLite engine + session factory (aiosqlite).

Owns the low-level database plumbing: URL construction, WAL/pragma setup on every
new connection, and schema creation. `repo.py` holds the module-level engine state.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from tgdl.storage.models import Base

logger = logging.getLogger(__name__)

# Applied to every new DBAPI connection. WAL gives us concurrent readers alongside the
# single writer; NORMAL sync is the standard, safe-enough pairing with WAL.
_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)


def build_url(database_path: Path) -> str:
    """SQLAlchemy async URL for a SQLite file path."""
    return f"sqlite+aiosqlite:///{Path(database_path).resolve()}"


def _register_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            for pragma in _PRAGMAS:
                cursor.execute(pragma)
        finally:
            cursor.close()


def create_engine(database_path: Path) -> AsyncEngine:
    """Create the async engine, ensuring the parent directory exists."""
    path = Path(database_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(build_url(path), echo=False, future=True)
    _register_pragmas(engine)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    """Session factory that keeps attributes loaded after commit."""
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def create_all(engine: AsyncEngine) -> None:
    """Create any missing tables/indexes, then purge legacy identifying data. Idempotent."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_purge_legacy_identifying_data)


# Columns that older versions stored on `requests` before the bot went anonymous.
_LEGACY_IDENTIFYING_COLUMNS = ("user_id", "chat_id", "message_id")


def _purge_legacy_identifying_data(sync_conn: Any) -> None:
    """Retire identifying data left by pre-anonymity databases.

    - Drop the old `users` table entirely.
    - Drop `user_id` / `chat_id` / `message_id` from `requests` (SQLite >= 3.35 supports
      DROP COLUMN; on older engines we fall back to nulling the values so nothing
      identifying survives even if the column can't be removed).

    Runs on every startup and is a no-op once the schema is clean.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import OperationalError

    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())

    if "users" in tables:
        logger.warning("dropping legacy `users` table (bot is now anonymous)")
        sync_conn.execute(text("DROP TABLE users"))

    if "requests" not in tables:
        return

    existing = {col["name"] for col in inspector.get_columns("requests")}
    to_remove = [c for c in _LEGACY_IDENTIFYING_COLUMNS if c in existing]
    for column in to_remove:
        try:
            sync_conn.execute(text(f"ALTER TABLE requests DROP COLUMN {column}"))
            logger.warning("dropped legacy identifying column requests.%s", column)
        except OperationalError:
            # Old SQLite without DROP COLUMN: at least erase the values.
            sync_conn.execute(text(f"UPDATE requests SET {column} = NULL"))
            logger.warning(
                "could not drop requests.%s (old SQLite); nulled its values instead",
                column,
            )
