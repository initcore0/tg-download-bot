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
    """Purge any legacy identifying data, then create missing tables/indexes. Idempotent."""
    # Migration first, on its own autocommit connection (see _purge_* for why), then
    # the normal schema creation in a transaction.
    async with engine.connect() as conn:
        await conn.run_sync(_purge_legacy_identifying_data)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# Columns that older versions stored on `requests` before the bot went anonymous.
_LEGACY_IDENTIFYING_COLUMNS = ("user_id", "chat_id", "message_id")


#: Scratch name used while rebuilding `requests`; never survives a completed run.
_REBUILD_TABLE = "_requests_rebuild"


def _purge_legacy_identifying_data(sync_conn: Any) -> None:
    """Retire identifying data left by pre-anonymity databases.

    - If `requests` still carries `user_id` / `chat_id` / `message_id`, rebuild it:
      create a fresh table from the current ORM schema, copy the shared columns, and
      swap it in. A rebuild (rather than ALTER TABLE DROP COLUMN) is the only approach
      that works on every SQLite version — older engines (e.g. 3.40 in Debian bookworm)
      refuse to drop a column that participates in a foreign key, and it is also the
      only way to shed the `user_id` FK itself. A lingering FK is fatal: once `users`
      is gone, any write to `requests` fails with "no such table: main.users" under
      `PRAGMA foreign_keys=ON`.
    - Drop the old `users` table entirely.

    Runs with SQLite foreign-key enforcement OFF and in AUTOCOMMIT on the raw DBAPI
    connection (below SQLAlchemy's transaction layer): the legacy FK would otherwise
    make the drops raise, and `PRAGMA foreign_keys` is ignored inside a transaction.
    The schema changes themselves run in one explicit transaction so a crash mid-way
    can't lose the audit data.

    Runs on every startup and is a no-op once the schema is clean. Also repairs
    databases broken by the previous migration (dangling `user_id` FK with `users`
    already dropped).
    """
    from sqlalchemy import MetaData, inspect
    from sqlalchemy.schema import CreateIndex, CreateTable

    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    has_users = "users" in tables
    old_columns: set[str] = set()
    if "requests" in tables:
        old_columns = {col["name"] for col in inspector.get_columns("requests")}
    legacy = [c for c in _LEGACY_IDENTIFYING_COLUMNS if c in old_columns]

    # Fast path: nothing legacy to do (the common case on a clean DB).
    if not has_users and not legacy:
        return

    table = Base.metadata.tables["requests"]
    dialect = sync_conn.dialect

    # Drop below SQLAlchemy to the raw sqlite3 connection so we control the
    # transaction directly: end any open tx, disable FKs, run DDL in autocommit.
    raw = sync_conn.connection.dbapi_connection
    raw.rollback()
    prev_isolation = raw.isolation_level
    raw.isolation_level = None  # autocommit — required for PRAGMA to take effect
    cur = raw.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute(f"DROP TABLE IF EXISTS {_REBUILD_TABLE}")  # crashed earlier run
        cur.execute("BEGIN")
        try:
            if legacy:
                logger.warning(
                    "rebuilding `requests` to drop legacy identifying columns: %s",
                    ", ".join(legacy),
                )
                scratch = table.to_metadata(MetaData(), name=_REBUILD_TABLE)
                cur.execute(str(CreateTable(scratch).compile(dialect=dialect)))
                shared = ", ".join(c.name for c in table.columns if c.name in old_columns)
                cur.execute(
                    f"INSERT INTO {_REBUILD_TABLE} ({shared}) "
                    f"SELECT {shared} FROM requests"
                )
                cur.execute("DROP TABLE requests")
                cur.execute(f"ALTER TABLE {_REBUILD_TABLE} RENAME TO requests")
                for index in table.indexes:
                    cur.execute(str(CreateIndex(index).compile(dialect=dialect)))
            if has_users:
                logger.warning("dropping legacy `users` table (bot is now anonymous)")
                cur.execute("DROP TABLE users")
            cur.execute("COMMIT")
        except Exception:
            raw.rollback()
            raise
        cur.execute("PRAGMA foreign_keys=ON")
    finally:
        cur.close()
        raw.isolation_level = prev_isolation
