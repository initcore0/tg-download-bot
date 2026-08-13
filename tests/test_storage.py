"""Tests for the anonymous storage/audit layer (M3).

Each test gets a fresh SQLite file under tmp_path and a fresh engine, so module-level
repo state never leaks between tests. The audit table is deliberately anonymous — one
test asserts the schema carries no identifying columns.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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


async def test_startup_repairs_dangling_user_id_fk(tmp_path: Path):
    """Repair a DB broken by the old migration: `users` dropped, FK column left behind.

    Older SQlite (e.g. 3.40 in Debian bookworm) refuses `DROP COLUMN user_id` because
    of its FK, and the previous migration then dropped `users` anyway. That left every
    INSERT into `requests` failing with "no such table: main.users" in production.
    """
    path = tmp_path / "broken.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_LEGACY_SCHEMA)
        # Reproduce the old migration's fallback end-state.
        conn.execute("UPDATE requests SET user_id = NULL")
        conn.execute("DROP TABLE users")

    await repo.init_db(path)
    try:
        async with repo._session() as session:
            conn = await session.connection()
            columns = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("requests")}
            )
        # The write that failed in production must now succeed.
        await repo.create_request(
            chat_type="private",
            url="https://x.com/i/status/9",
            normalized_url="https://x.com/i/status/9",
            platform="twitter",
        )
    finally:
        await repo.close_db()

    assert {"user_id", "chat_id", "message_id"}.isdisjoint(columns)
    # The dangling FK is really gone from the schema, not just the column list.
    with sqlite3.connect(path) as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='requests'"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT chat_type, url, status FROM requests WHERE id = 1"
        ).fetchone()
    assert "users" not in ddl
    assert row == ("private", "https://youtube.com/watch?v=abc", "success")


# ------------------------------------------------- additive column migration

#: An anonymous-era `requests` table from before the cache columns existed. It is
#: already clean of identifying data, so only the ADD COLUMN pass has work to do.
_PRE_CACHE_COLUMNS_SCHEMA = """
    CREATE TABLE requests (
        id INTEGER PRIMARY KEY,
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
    INSERT INTO requests (chat_type, url, normalized_url, status, media_kind,
                          telegram_file_id)
    VALUES ('private', 'https://youtube.com/watch?v=old',
            'https://youtube.com/watch?v=old', 'success', 'video', 'old-fid');
"""


async def test_startup_adds_missing_cache_columns(tmp_path: Path):
    """An old DB gains the new columns on init, keeping every existing row."""
    path = tmp_path / "pre-cache.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_PRE_CACHE_COLUMNS_SCHEMA)

    await repo.init_db(path)
    try:
        async with repo._session() as session:
            conn = await session.connection()
            columns = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("requests")}
            )
        # The migrated DB is fully writable, new columns included.
        request = await new_request()
        await repo.mark_success(
            request.id, make_media(kind="image"), "new-1", 1.0,
            telegram_file_ids=["new-1", "new-2"], cache_hit=True,
        )
        row = await repo.find_cached("https://youtube.com/watch?v=abc")
    finally:
        await repo.close_db()

    assert {"telegram_file_ids", "cache_hit"} <= columns
    assert repo.decode_file_ids(row) == ["new-1", "new-2"]
    # The pre-existing row survived untouched, and its NOT NULL cache_hit backfilled.
    with sqlite3.connect(path) as conn:
        old = conn.execute(
            "SELECT url, status, telegram_file_id, telegram_file_ids, cache_hit "
            "FROM requests WHERE id = 1"
        ).fetchone()
    assert old == ("https://youtube.com/watch?v=old", "success", "old-fid", None, 0)


async def test_column_migration_is_idempotent(tmp_path: Path):
    """Init over an already-current schema adds nothing and must not raise."""
    path = tmp_path / "pre-cache2.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_PRE_CACHE_COLUMNS_SCHEMA)

    await repo.init_db(path)
    await repo.close_db()
    await repo.init_db(path)  # second pass: schema already matches
    try:
        async with repo._session() as session:
            conn = await session.connection()
            columns = await conn.run_sync(
                lambda c: [col["name"] for col in inspect(c).get_columns("requests")]
            )
    finally:
        await repo.close_db()

    assert columns.count("cache_hit") == 1
    assert columns.count("telegram_file_ids") == 1


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
        "cache_hits": 0,
        "hit_rate": 0.0,
        "platforms": {},
    }


async def test_stats_counts(db_path: Path):
    ok = await new_request()
    bad = await new_request()
    await new_request()  # stays pending

    await repo.mark_success(ok.id, make_media(), "fid", 1.0)
    await repo.mark_failure(bad.id, RuntimeError("nope"), 1.0)

    data = await repo.stats()
    assert data["requests"] == 3
    assert data["pending"] == 1
    assert data["success"] == 1
    assert data["failed"] == 1


async def test_stats_hit_rate(db_path: Path):
    """cache_hits / success — the metric §6.1 was built to make possible."""
    for _ in range(3):
        row = await new_request()
        await repo.mark_success(row.id, make_media(), "fid", 1.0)
    hit = await new_request()
    await repo.mark_success(hit.id, make_media(), "fid", 0.1, cache_hit=True)

    data = await repo.stats()

    assert data["success"] == 4
    assert data["cache_hits"] == 1
    assert data["hit_rate"] == pytest.approx(0.25)


async def test_stats_hit_rate_is_zero_when_nothing_succeeded(db_path: Path):
    bad = await new_request()
    await repo.mark_failure(bad.id, RuntimeError("nope"), 1.0)

    data = await repo.stats()

    assert data["success"] == 0
    assert data["hit_rate"] == 0.0  # guarded, not a ZeroDivisionError


async def test_stats_platform_percentiles_use_nearest_rank(db_path: Path):
    """p50 of 4 samples is the 2nd value, p95 the 4th — no interpolation."""
    for elapsed in (1.0, 2.0, 3.0, 40.0):
        row = await new_request()
        await repo.mark_success(row.id, make_media(), "fid", elapsed)

    entry = (await repo.stats())["platforms"]["youtube"]

    assert entry["count"] == 4
    assert entry["p50_s"] == pytest.approx(2.0)
    assert entry["p95_s"] == pytest.approx(40.0)


async def test_stats_platforms_are_separated(db_path: Path):
    yt = await new_request()
    await repo.mark_success(yt.id, make_media(), "fid", 5.0)
    tt = await new_request(platform="tiktok", normalized_url="https://tiktok.com/v/1")
    await repo.mark_success(tt.id, make_media(platform="tiktok"), "fid", 1.0)

    platforms = (await repo.stats())["platforms"]

    assert platforms["youtube"]["p50_s"] == pytest.approx(5.0)
    assert platforms["tiktok"]["p50_s"] == pytest.approx(1.0)


async def test_stats_platforms_exclude_failures_and_old_rows(db_path: Path):
    bad = await new_request()
    await repo.mark_failure(bad.id, RuntimeError("nope"), 9.0)
    old = await new_request()
    await repo.mark_success(old.id, make_media(), "fid", 9.0)
    await _age_request(old.id, days=repo.STATS_PLATFORM_WINDOW_DAYS + 1)

    assert (await repo.stats())["platforms"] == {}


def test_percentile_nearest_rank_edges():
    assert repo._percentile([], 0.5) == 0.0
    assert repo._percentile([7.0], 0.95) == 7.0
    # 20 samples: p95 -> rank 19 (the 19th smallest), p50 -> rank 10.
    values = [float(i) for i in range(1, 21)]
    assert repo._percentile(values, 0.95) == 19.0
    assert repo._percentile(values, 0.50) == 10.0


# ------------------------------------------------------------------ file_id cache


async def _age_request(request_id: int, *, days: float) -> None:
    """Backdate a row's created_at so age-sensitive queries can be exercised."""
    async with repo._session() as session, session.begin():
        stored = await session.get(DownloadRequest, request_id)
        stored.created_at = datetime.now(UTC) - timedelta(days=days)


async def test_find_cached_file_id_returns_recent_success(db_path: Path):
    request = await new_request()
    await repo.mark_success(request.id, make_media(), "fid-video", 1.0)

    row = await repo.find_cached_file_id("https://youtube.com/watch?v=abc")

    assert row is not None
    assert row.telegram_file_id == "fid-video"
    assert row.media_kind == "video"
    assert row.width == 1280 and row.height == 720
    assert row.duration_s == pytest.approx(42.5)


async def test_find_cached_file_id_returns_most_recent_of_several(db_path: Path):
    old = await new_request()
    await repo.mark_success(old.id, make_media(), "fid-old", 1.0)
    await _age_request(old.id, days=5)

    fresh = await new_request()
    await repo.mark_success(fresh.id, make_media(), "fid-fresh", 1.0)

    row = await repo.find_cached_file_id("https://youtube.com/watch?v=abc")
    assert row.telegram_file_id == "fid-fresh"


async def test_find_cached_file_id_misses_when_too_old(db_path: Path):
    request = await new_request()
    await repo.mark_success(request.id, make_media(), "fid-stale", 1.0)
    await _age_request(request.id, days=45)

    assert await repo.find_cached_file_id("https://youtube.com/watch?v=abc") is None
    # ...but a caller willing to trust older file_ids still finds it.
    row = await repo.find_cached_file_id(
        "https://youtube.com/watch?v=abc", max_age_days=90
    )
    assert row.telegram_file_id == "fid-stale"


async def test_find_cached_skips_images_without_a_file_id_list(db_path: Path):
    """A pre-migration image row holds only the first carousel item — never replay it."""
    request = await new_request()
    await repo.mark_success(
        request.id, make_media(kind="image"), "fid-photo", 1.0, telegram_file_ids=[]
    )

    assert await repo.find_cached("https://youtube.com/watch?v=abc") is None


async def test_find_cached_returns_image_rows_with_a_file_id_list(db_path: Path):
    request = await new_request()
    await repo.mark_success(
        request.id,
        make_media(kind="image"),
        "fid-1",
        1.0,
        telegram_file_ids=["fid-1", "fid-2", "fid-3"],
    )

    row = await repo.find_cached("https://youtube.com/watch?v=abc")

    assert row is not None
    assert row.media_kind == "image"
    assert repo.decode_file_ids(row) == ["fid-1", "fid-2", "fid-3"]
    # The single-id column keeps holding the first item, exactly as before.
    assert row.telegram_file_id == "fid-1"


async def test_find_cached_returns_single_image_rows(db_path: Path):
    """One image is just a list of one — still replayable."""
    request = await new_request()
    await repo.mark_success(
        request.id, make_media(kind="image"), "solo", 1.0, telegram_file_ids=["solo"]
    )

    row = await repo.find_cached("https://youtube.com/watch?v=abc")
    assert repo.decode_file_ids(row) == ["solo"]


async def test_mark_success_defaults_the_list_to_the_single_file_id(db_path: Path):
    """Callers that don't pass a list still get a usable one (video/animation path)."""
    request = await new_request()
    await repo.mark_success(request.id, make_media(), "only-one", 1.0)

    row = await repo.find_cached("https://youtube.com/watch?v=abc")
    assert repo.decode_file_ids(row) == ["only-one"]


async def test_cache_hit_flag_round_trip(db_path: Path):
    fresh = await new_request()
    await repo.mark_success(fresh.id, make_media(), "fid", 1.0)
    replayed = await new_request()
    await repo.mark_success(replayed.id, make_media(), "fid", 0.1, cache_hit=True)

    async with repo._session() as session:
        assert (await session.get(DownloadRequest, fresh.id)).cache_hit is False
        assert (await session.get(DownloadRequest, replayed.id)).cache_hit is True


async def test_decode_file_ids_tolerates_corrupt_json(db_path: Path):
    """A garbled blob degrades to the single-id column, never raises."""
    row = SimpleNamespace(id=1, telegram_file_ids="{not json", telegram_file_id="fallback")
    assert repo.decode_file_ids(row) == ["fallback"]

    empty = SimpleNamespace(id=2, telegram_file_ids=None, telegram_file_id=None)
    assert repo.decode_file_ids(empty) == []


async def test_find_cached_file_id_is_an_alias_of_find_cached(db_path: Path):
    """The old name still works for any caller that hasn't moved over."""
    request = await new_request()
    await repo.mark_success(request.id, make_media(), "fid-video", 1.0)

    row = await repo.find_cached_file_id("https://youtube.com/watch?v=abc")
    assert row.telegram_file_id == "fid-video"


async def test_find_cached_file_id_accepts_animations(db_path: Path):
    request = await new_request()
    await repo.mark_success(request.id, make_media(kind="animation"), "fid-anim", 1.0)

    row = await repo.find_cached_file_id("https://youtube.com/watch?v=abc")
    assert row.media_kind == "animation"


async def test_find_cached_file_id_skips_rows_without_file_id(db_path: Path):
    request = await new_request()
    await repo.mark_success(request.id, make_media(), None, 1.0)

    assert await repo.find_cached_file_id("https://youtube.com/watch?v=abc") is None


async def test_find_cached_file_id_skips_pending_and_failed(db_path: Path):
    await new_request()  # pending
    failed = await new_request()
    await repo.mark_failure(failed.id, RuntimeError("nope"), 1.0)

    assert await repo.find_cached_file_id("https://youtube.com/watch?v=abc") is None


async def test_find_cached_file_id_misses_on_different_url(db_path: Path):
    request = await new_request()
    await repo.mark_success(request.id, make_media(), "fid", 1.0)

    assert await repo.find_cached_file_id("https://youtube.com/watch?v=other") is None
    assert await repo.find_cached_file_id("") is None


# ------------------------------------------------------- audio rows & kind filter


async def test_mark_success_records_the_media_kind_override(db_path: Path):
    """models.MediaKind has no "audio", so /mp3 rows get their kind explicitly."""
    request = await new_request()

    await repo.mark_success(
        request.id, make_media(), "fid-audio", 1.0, media_kind_override="audio"
    )

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)
    assert stored.media_kind == "audio"
    # Everything else still comes from the MediaResult.
    assert stored.title == "A test video"


async def test_mark_success_without_an_override_uses_the_media_kind(db_path: Path):
    request = await new_request()

    await repo.mark_success(request.id, make_media(kind="animation"), "fid", 1.0)

    async with repo._session() as session:
        stored = await session.get(DownloadRequest, request.id)
    assert stored.media_kind == "animation"


async def test_find_cached_filters_by_media_kind(db_path: Path):
    """A video and an /mp3 row share one URL; neither may stand in for the other."""
    video = await new_request()
    await repo.mark_success(video.id, make_media(), "fid-video", 1.0)
    audio = await new_request()
    await repo.mark_success(
        audio.id, make_media(), "fid-audio", 1.0, media_kind_override="audio"
    )
    url = "https://youtube.com/watch?v=abc"

    audio_row = await repo.find_cached(url, media_kinds=("audio",))
    video_row = await repo.find_cached(url, media_kinds=("video", "animation", "image"))

    assert audio_row.telegram_file_id == "fid-audio"
    assert video_row.telegram_file_id == "fid-video"


async def test_find_cached_without_a_filter_accepts_audio(db_path: Path):
    """Inline mode can render every cacheable kind, so it passes no filter."""
    request = await new_request()
    await repo.mark_success(
        request.id, make_media(), "fid-audio", 1.0, media_kind_override="audio"
    )

    row = await repo.find_cached("https://youtube.com/watch?v=abc")

    assert row is not None and row.media_kind == "audio"


async def test_find_cached_with_an_empty_filter_matches_nothing(db_path: Path):
    request = await new_request()
    await repo.mark_success(request.id, make_media(), "fid", 1.0)

    assert await repo.find_cached("https://youtube.com/watch?v=abc", media_kinds=()) is None


# --------------------------------------------------------------- audit retention


async def test_prune_audit_deletes_expired_rows(db_path: Path):
    old = await new_request()
    await repo.mark_success(old.id, make_media(), "fid", 1.0)
    await _age_request(old.id, days=200)
    recent = await new_request()
    await repo.mark_success(recent.id, make_media(), "fid2", 1.0)

    result = await repo.prune_audit()

    assert result["deleted"] == 1
    async with repo._session() as session:
        assert await session.get(DownloadRequest, old.id) is None
        assert await session.get(DownloadRequest, recent.id) is not None


async def test_prune_audit_marks_stale_pending_failed(db_path: Path):
    stale = await new_request()
    await _age_request(stale.id, days=1)

    result = await repo.prune_audit()

    assert result["stale_pending"] == 1
    async with repo._session() as session:
        stored = await session.get(DownloadRequest, stale.id)
    assert stored.status == "failed"
    assert stored.error_class == "StaleRequest"
    assert stored.completed_at is not None


async def test_prune_audit_leaves_in_flight_pending_alone(db_path: Path):
    live = await new_request()

    result = await repo.prune_audit()

    assert result == {"deleted": 0, "stale_pending": 0}
    async with repo._session() as session:
        stored = await session.get(DownloadRequest, live.id)
    assert stored.status == "pending"


async def test_prune_audit_does_not_touch_completed_rows(db_path: Path):
    done = await new_request()
    await repo.mark_success(done.id, make_media(), "fid", 1.0)
    await _age_request(done.id, days=1)

    assert (await repo.prune_audit())["stale_pending"] == 0
    async with repo._session() as session:
        stored = await session.get(DownloadRequest, done.id)
    assert stored.status == "success"


async def test_prune_audit_honours_custom_windows(db_path: Path):
    request = await new_request()
    await repo.mark_success(request.id, make_media(), "fid", 1.0)
    await _age_request(request.id, days=2)

    assert (await repo.prune_audit(retention_days=1))["deleted"] == 1


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
