"""Tests for the anonymous storage/audit layer (M3).

Each test gets a fresh SQLite file under tmp_path and a fresh engine, so module-level
repo state never leaks between tests. The audit table is deliberately anonymous — one
test asserts the schema carries no identifying columns.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect

from tgdl.downloader.models import MediaResult
from tgdl.storage import repo
from tgdl.storage.models import DownloadRequest


@pytest.fixture
async def db_path(tmp_path: Path):
    """Initialized DB in a nested (not-yet-existing) dir; torn down after each test."""
    path = tmp_path / "nested" / "tgdl.db"
    await repo.init_db(path)
    try:
        yield path
    finally:
        await repo.close_db()


def make_media(**overrides) -> MediaResult:
    defaults = {
        "path": Path("/tmp/video.mp4"),
        "kind": "video",
        "source_url": "https://youtube.com/watch?v=abc",
        "platform": "youtube",
        "filesize": 1234567,
        "title": "A test video",
        "width": 1280,
        "height": 720,
        "duration_s": 42.5,
        "transcoded": True,
        "elapsed_s": 3.5,
    }
    defaults.update(overrides)
    return MediaResult(**defaults)


async def new_request(**overrides) -> DownloadRequest:
    kwargs = {
        "chat_type": "private",
        "url": "https://youtube.com/watch?v=abc&utm_source=x",
        "normalized_url": "https://youtube.com/watch?v=abc",
        "platform": "youtube",
    }
    kwargs.update(overrides)
    return await repo.create_request(**kwargs)


# --------------------------------------------------------------------------- init


async def test_init_db_creates_file_parent_dir_and_tables(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "tgdl.db"
    assert not path.parent.exists()

    await repo.init_db(path)
    try:
        assert path.parent.is_dir()
        assert path.exists()

        async with repo._session() as session:
            conn = await session.connection()
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert "requests" in tables
        assert "users" not in tables, "there must be no user table"
    finally:
        await repo.close_db()


async def test_schema_has_no_identifying_columns(db_path: Path):
    """Privacy regression guard: the audit table stores nothing that names a person."""
    async with repo._session() as session:
        conn = await session.connection()
        columns = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("requests")}
        )

    forbidden = {
        "user_id",
        "telegram_id",
        "username",
        "first_name",
        "last_name",
        "chat_id",
        "message_id",
    }
    leaked = forbidden & columns
    assert not leaked, f"identifying columns must not exist: {leaked}"
    # The coarse, non-identifying context field is still present.
    assert "chat_type" in columns


async def test_init_db_enables_wal(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


async def test_init_db_is_idempotent(db_path: Path):
    await repo.init_db(db_path)  # second call must not blow up or wipe data
    request = await new_request()
    assert request.id is not None


async def test_expected_indexes_exist(db_path: Path):
    async with repo._session() as session:
        conn = await session.connection()
        indexes = await conn.run_sync(
            lambda c: {ix["name"] for ix in inspect(c).get_indexes("requests")}
        )
    assert {
        "ix_requests_normalized_url",
        "ix_requests_created_at",
    } <= indexes
    assert "ix_requests_user_id" not in indexes


async def test_close_db_then_reinit_works(db_path: Path):
    request = await new_request()
    await repo.close_db()

    await repo.close_db()  # safe to call twice / when not initialized

    await repo.init_db(db_path)
    async with repo._session() as session:
        again = await session.get(DownloadRequest, request.id)
    assert again is not None
    assert again.url == request.url


async def test_calls_before_init_raise():
    await repo.close_db()
    with pytest.raises(RuntimeError, match="not initialized"):
        await new_request()


# --------------------------------------------------- legacy-data purge (migration)


#: The exact pre-anonymity schema: `requests.user_id` is a REAL foreign key to
#: `users`, which is what made the naive migration crash under FK enforcement.
_LEGACY_SCHEMA = """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY, telegram_id BIGINT, username TEXT,
        first_name TEXT, last_name TEXT
    );
    INSERT INTO users (id, telegram_id, username) VALUES (1, 777, 'alice');
    CREATE TABLE requests (
        id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        chat_id BIGINT NOT NULL, message_id BIGINT,
        chat_type TEXT NOT NULL,
        url TEXT NOT NULL, normalized_url TEXT, platform TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        error_class TEXT, error_message TEXT,
        media_kind TEXT, title TEXT, filesize_bytes BIGINT,
        duration_s FLOAT, width INTEGER, height INTEGER,
        transcoded BOOLEAN NOT NULL DEFAULT 0,
        telegram_file_id TEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at DATETIME, elapsed_s FLOAT
    );
    INSERT INTO requests (user_id, chat_id, message_id, chat_type, url, status)
    VALUES (1, 555, 42, 'private', 'https://youtube.com/watch?v=abc', 'success');
"""


async def test_startup_purges_legacy_user_data(tmp_path: Path):
    """A pre-anonymity DB (users table + FK'd identifying columns) is cleaned on init.

    This reproduces the real production failure: dropping `users` or `user_id` with the
    foreign key in place raises under `PRAGMA foreign_keys=ON`.
    """
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_LEGACY_SCHEMA)

    await repo.init_db(path)
    try:
        async with repo._session() as session:
            conn = await session.connection()
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
            columns = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("requests")}
            )
        # The DB is fully usable after migration: a fresh write must succeed.
        await repo.create_request(
            chat_type="private",
            url="https://x.com/i/status/9",
            normalized_url="https://x.com/i/status/9",
            platform="twitter",
        )
    finally:
        await repo.close_db()

    assert "users" not in tables, "legacy users table must be dropped"
    assert {"user_id", "chat_id", "message_id"}.isdisjoint(columns), (
        "legacy identifying columns must be gone"
    )
    # The pre-existing request row survives, minus its identifying fields.
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT chat_type, url, status FROM requests WHERE id = 1"
        ).fetchone()
    assert row == ("private", "https://youtube.com/watch?v=abc", "success")


async def test_legacy_purge_is_idempotent(tmp_path: Path):
    """Running init twice over a legacy DB must not fail the second time."""
    path = tmp_path / "legacy2.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_LEGACY_SCHEMA)

    await repo.init_db(path)
    await repo.close_db()
    # Second init over the now-clean DB: no-op, must not raise.
    await repo.init_db(path)
    try:
        async with repo._session() as session:
            conn = await session.connection()
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    finally:
        await repo.close_db()
    assert "users" not in tables


# ------------------------------------------------------------------------ requests


async def test_create_request_persists_anonymous_pending_row(db_path: Path):
    request = await new_request(chat_type="group")

    assert request.id is not None
    assert request.status == "pending"
    assert request.created_at.tzinfo is not None
    assert request.completed_at is None

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)
        assert stored is not None
        assert stored.chat_type == "group"
        assert stored.url == "https://youtube.com/watch?v=abc&utm_source=x"
        assert stored.normalized_url == "https://youtube.com/watch?v=abc"
        assert stored.platform == "youtube"
        assert stored.transcoded is False


async def test_mark_success_round_trip(db_path: Path):
    request = await new_request()
    media = make_media()

    await repo.mark_success(request.id, media, "BAADBAADfile_id_123", 7.25)

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)

    assert stored is not None
    assert stored.status == "success"
    assert stored.media_kind == "video"
    assert stored.title == "A test video"
    assert stored.filesize_bytes == 1234567
    assert stored.duration_s == pytest.approx(42.5)
    assert stored.width == 1280
    assert stored.height == 720
    assert stored.transcoded is True
    assert stored.telegram_file_id == "BAADBAADfile_id_123"
    assert stored.elapsed_s == pytest.approx(7.25)
    assert stored.completed_at is not None
    assert stored.completed_at.tzinfo is not None
    assert stored.completed_at >= stored.created_at
    assert stored.error_class is None and stored.error_message is None


async def test_mark_success_without_file_id_and_sparse_metadata(db_path: Path):
    request = await new_request()
    media = make_media(
        kind="image",
        title=None,
        width=None,
        height=None,
        duration_s=None,
        transcoded=False,
    )

    await repo.mark_success(request.id, media, None, 1.0)

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)

    assert stored.status == "success"
    assert stored.media_kind == "image"
    assert stored.telegram_file_id is None
    assert stored.title is None
    assert stored.transcoded is False


async def test_mark_success_truncates_overlong_title(db_path: Path):
    request = await new_request()
    await repo.mark_success(request.id, make_media(title="T" * 5000), "fid", 1.0)

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)
    assert len(stored.title) <= 1000


async def test_mark_failure_persists_error(db_path: Path):
    request = await new_request()
    error = ValueError("this link is private")

    await repo.mark_failure(request.id, error, 2.5)

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)

    assert stored.status == "failed"
    assert stored.error_class == "ValueError"
    assert stored.error_message == "this link is private"
    assert stored.elapsed_s == pytest.approx(2.5)
    assert stored.completed_at is not None
    assert stored.completed_at.tzinfo is not None


async def test_mark_failure_with_download_error_subclass(db_path: Path):
    from tgdl.downloader.models import MediaTooLargeError

    request = await new_request()
    await repo.mark_failure(request.id, MediaTooLargeError("380MB > 48MB"), 9.0)

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)
    assert stored.error_class == "MediaTooLargeError"
    assert "380MB" in stored.error_message


async def test_mark_failure_truncates_huge_message(db_path: Path):
    request = await new_request()
    await repo.mark_failure(request.id, RuntimeError("x" * 10_000), 1.0)

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)
    assert len(stored.error_message) <= 2000


# ------------------------------------------------- audit must never break the flow


async def test_mark_success_with_bogus_request_id_does_not_raise(db_path: Path):
    await repo.mark_success(999_999, make_media(), "fid", 1.0)


async def test_mark_failure_with_bogus_request_id_does_not_raise(db_path: Path):
    await repo.mark_failure(999_999, RuntimeError("boom"), 1.0)


async def test_mark_helpers_do_not_raise_when_db_uninitialized():
    await repo.close_db()
    await repo.mark_success(1, make_media(), "fid", 1.0)
    await repo.mark_failure(1, RuntimeError("boom"), 1.0)


async def test_mark_success_swallows_unexpected_media_errors(db_path: Path):
    """A malformed MediaResult must be logged, not propagated to the bot."""
    request = await new_request()

    class Exploding:
        kind = "video"

        def __getattr__(self, name):
            raise RuntimeError("bad media object")

    await repo.mark_success(request.id, Exploding(), "fid", 1.0)

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)
    assert stored.status == "pending", "failed audit write must not half-commit"


# ----------------------------------------------------------------------- stats


async def test_stats_on_empty_db(db_path: Path):
    assert await repo.stats() == {
        "requests": 0,
        "pending": 0,
        "success": 0,
        "failed": 0,
    }


async def test_stats_counts(db_path: Path):
    ok = await new_request()
    bad = await new_request()
    await new_request()  # stays pending

    await repo.mark_success(ok.id, make_media(), "fid", 1.0)
    await repo.mark_failure(bad.id, RuntimeError("nope"), 1.0)

    assert await repo.stats() == {
        "requests": 3,
        "pending": 1,
        "success": 1,
        "failed": 1,
    }


# ------------------------------------------------------------------- timestamps


async def test_timestamps_are_utc_aware_after_reload(db_path: Path):
    """Values must come back tz-aware from disk, not just from the identity map."""
    before = datetime.now(UTC)
    request = await new_request()
    await repo.mark_success(request.id, make_media(), "fid", 1.0)

    await repo.close_db()
    await repo.init_db(db_path)

    async with repo._session() as session:
        stored_request = await session.get(DownloadRequest, request.id)

    for value in (stored_request.created_at, stored_request.completed_at):
        assert value.tzinfo is not None, "timestamps must be timezone-aware"
        assert value.utcoffset().total_seconds() == 0, "timestamps must be UTC"
        assert value >= before
