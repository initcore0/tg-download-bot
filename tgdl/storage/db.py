"""Async SQLite engine + session factory (aiosqlite).

Owns the low-level database plumbing: URL construction, WAL/pragma setup on every
new connection, and schema creation. `repo.py` holds the module-level engine state.
"""
from __future__ import annotations

import logging
import sqlite3
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
    """Purge any legacy identifying data, then create missing tables/indexes. Idempotent."""
    # Migration first, on its own autocommit connection (see _purge_* for why), then
    # the normal schema creation in a transaction.
    async with engine.connect() as conn:
        await conn.run_sync(_purge_legacy_identifying_data)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# Columns that older versions stored on `requests` before the bot went anonymous.
_LEGACY_IDENTIFYING_COLUMNS = ("user_id", "chat_id", "message_id")


def _purge_legacy_identifying_data(sync_conn: Any) -> None:
    """Retire identifying data left by pre-anonymity databases.

    - Drop `user_id` / `chat_id` / `message_id` from `requests` (SQLite >= 3.35 supports
      DROP COLUMN; on older engines we fall back to nulling the values so nothing
      identifying survives even if the column can't be removed). `user_id` carries a
      foreign key to `users`, so it must go before that table can be dropped.
    - Drop the old `users` table entirely.

    Runs the DDL with SQLite foreign-key enforcement OFF and in AUTOCOMMIT: the legacy
    `requests.user_id` FK references `users`, so with FKs on the drops would raise, and
    `PRAGMA foreign_keys` is ignored inside a transaction. Executing on a raw DBAPI
    connection (below SQLAlchemy's transaction layer) keeps this migration from
    colliding with the ORM's transaction management.

    Runs on every startup and is a no-op once the schema is clean.
    """
    from sqlalchemy import inspect

    def has_users() -> bool:
        return "users" in set(inspect(sync_conn).get_table_names())

    def legacy_columns() -> list[str]:
        if "requests" not in set(inspect(sync_conn).get_table_names()):
            return []
        existing = {col["name"] for col in inspect(sync_conn).get_columns("requests")}
        return [c for c in _LEGACY_IDENTIFYING_COLUMNS if c in existing]

    # Fast path: nothing legacy to do (the common case on a clean DB).
    if not has_users() and not legacy_columns():
        return

    # Drop below SQLAlchemy to the raw sqlite3 connection so we control the
    # transaction directly: end any open tx, disable FKs, run DDL in autocommit.
    raw = sync_conn.connection.dbapi_connection
    raw.rollback()
    prev_isolation = raw.isolation_level
    raw.isolation_level = None  # autocommit — required for DDL + PRAGMA to take effect
    cur = raw.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        for column in legacy_columns():
            try:
                cur.execute(f"ALTER TABLE requests DROP COLUMN {column}")
                logger.warning("dropped legacy identifying column requests.%s", column)
            except sqlite3.OperationalError:
                # Old SQLite without DROP COLUMN: at least erase the values.
                cur.execute(f"UPDATE requests SET {column} = NULL")
                logger.warning(
                    "could not drop requests.%s (old SQLite); nulled its values instead",
                    column,
                )
        if has_users():
            logger.warning("dropping legacy `users` table (bot is now anonymous)")
            cur.execute("DROP TABLE users")
        cur.execute("PRAGMA foreign_keys=ON")
    finally:
        cur.close()
        raw.isolation_level = prev_isolation
