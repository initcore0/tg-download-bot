"""Tests for the storage/audit layer (M3).

Each test gets a fresh SQLite file under tmp_path and a fresh engine, so module-level
repo state never leaks between tests.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from tgdl.downloader.models import MediaResult
from tgdl.storage import repo
from tgdl.storage.models import DownloadRequest, User


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
        "user_id": None,
        "chat_id": 555,
        "chat_type": "private",
        "message_id": 1,
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
        assert {"users", "requests"} <= tables
    finally:
        await repo.close_db()


async def test_init_db_enables_wal(db_path: Path):
    # Read the pragma with a plain, independent connection.
    with sqlite3.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


async def test_init_db_is_idempotent(db_path: Path):
    await repo.init_db(db_path)  # second call must not blow up or wipe data
    user = await repo.get_or_create_user(1, "u", "f", "l")
    assert user.id is not None


async def test_expected_indexes_exist(db_path: Path):
    async with repo._session() as session:
        conn = await session.connection()
        indexes = await conn.run_sync(
            lambda c: {ix["name"] for ix in inspect(c).get_indexes("requests")}
        )
    assert {
        "ix_requests_normalized_url",
        "ix_requests_user_id",
        "ix_requests_created_at",
    } <= indexes


async def test_close_db_then_reinit_works(db_path: Path):
    user = await repo.get_or_create_user(777, "keep", "Keep", "Me")
    await repo.close_db()

    await repo.close_db()  # safe to call twice / when not initialized

    await repo.init_db(db_path)
    async with repo._session() as session:
        again = (
            await session.execute(select(User).where(User.telegram_id == 777))
        ).scalar_one()
    assert again.id == user.id
    assert again.username == "keep"


async def test_calls_before_init_raise():
    await repo.close_db()
    with pytest.raises(RuntimeError, match="not initialized"):
        await repo.get_or_create_user(1, None, None, None)


# --------------------------------------------------------------------------- users


async def test_get_or_create_user_creates(db_path: Path):
    user = await repo.get_or_create_user(42, "alice", "Alice", "Smith")

    assert user.id is not None
    assert user.telegram_id == 42
    assert (user.username, user.first_name, user.last_name) == ("alice", "Alice", "Smith")
    assert user.request_count == 1
    assert user.first_seen_at.tzinfo is not None
    assert user.last_seen_at.tzinfo is not None


async def test_get_or_create_user_updates_and_bumps_count(db_path: Path):
    first = await repo.get_or_create_user(42, "alice", "Alice", "Smith")
    first_seen = first.first_seen_at

    second = await repo.get_or_create_user(42, "alice_new", "Alicia", "Jones")

    assert second.id == first.id, "must not create a duplicate row"
    assert second.username == "alice_new"
    assert second.first_name == "Alicia"
    assert second.last_name == "Jones"
    assert second.request_count == 2
    assert second.first_seen_at == first_seen, "first_seen_at is immutable"
    assert second.last_seen_at >= first_seen

    third = await repo.get_or_create_user(42, None, None, None)
    assert third.request_count == 3
    assert third.username is None, "cleared profile fields must be reflected"

    async with repo._session() as session:
        count = len((await session.execute(select(User))).scalars().all())
    assert count == 1


async def test_distinct_users_are_separate_rows(db_path: Path):
    a = await repo.get_or_create_user(1, "a", None, None)
    b = await repo.get_or_create_user(2, "b", None, None)
    assert a.id != b.id


# ------------------------------------------------------------------------ requests


async def test_create_request_persists_pending_row(db_path: Path):
    user = await repo.get_or_create_user(42, "alice", "Alice", None)
    request = await new_request(user_id=user.id, chat_type="group", message_id=99)

    assert request.id is not None
    assert request.status == "pending"
    assert request.created_at.tzinfo is not None
    assert request.completed_at is None

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)
        assert stored is not None
        assert stored.user_id == user.id
        assert stored.chat_id == 555
        assert stored.chat_type == "group"
        assert stored.message_id == 99
        assert stored.normalized_url == "https://youtube.com/watch?v=abc"
        assert stored.platform == "youtube"
        assert stored.transcoded is False


async def test_create_request_allows_null_user(db_path: Path):
    request = await new_request(user_id=None, chat_type="channel", message_id=None)
    assert request.id is not None
    assert request.user_id is None


async def test_mark_success_round_trip(db_path: Path):
    user = await repo.get_or_create_user(42, "alice", "Alice", None)
    request = await new_request(user_id=user.id)
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
        "users": 0,
        "requests": 0,
        "pending": 0,
        "success": 0,
        "failed": 0,
    }


async def test_stats_counts(db_path: Path):
    user = await repo.get_or_create_user(1, "a", None, None)
    await repo.get_or_create_user(2, "b", None, None)

    ok = await new_request(user_id=user.id)
    bad = await new_request(user_id=user.id)
    await new_request(user_id=user.id)  # stays pending

    await repo.mark_success(ok.id, make_media(), "fid", 1.0)
    await repo.mark_failure(bad.id, RuntimeError("nope"), 1.0)

    assert await repo.stats() == {
        "users": 2,
        "requests": 3,
        "pending": 1,
        "success": 1,
        "failed": 1,
    }


# ------------------------------------------------------------------- timestamps


async def test_timestamps_are_utc_aware_after_reload(db_path: Path):
    """Values must come back tz-aware from disk, not just from the identity map."""
    before = datetime.now(UTC)
    user = await repo.get_or_create_user(42, "alice", None, None)
    request = await new_request(user_id=user.id)
    await repo.mark_success(request.id, make_media(), "fid", 1.0)

    await repo.close_db()
    await repo.init_db(db_path)

    async with repo._session() as session:
        stored_user = (
            await session.execute(select(User).where(User.telegram_id == 42))
        ).scalar_one()
        stored_request = await session.get(DownloadRequest, request.id)

    for value in (
        stored_user.first_seen_at,
        stored_user.last_seen_at,
        stored_request.created_at,
        stored_request.completed_at,
    ):
        assert value.tzinfo is not None, "timestamps must be timezone-aware"
        assert value.utcoffset().total_seconds() == 0, "timestamps must be UTC"
        assert value >= before
