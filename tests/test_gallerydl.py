"""gallery-dl wrapper: command construction, error classification, file collection.

No network and no real gallery-dl runs — the subprocess helper is monkeypatched.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tgdl.downloader import gallerydl
from tgdl.downloader.models import (
    ExtractionError,
    TransientExtractionError,
    UnsupportedUrlError,
)


@pytest.fixture(autouse=True)
def no_cookies():
    """Each test starts without a configured cookies file."""
    gallerydl.configure(cookies_file=None)
    yield
    gallerydl.configure(cookies_file=None)


class TestBuildCommand:
    def test_basic_shape(self, tmp_path):
        args = gallerydl.build_command("https://x.com/u/status/1", tmp_path, max_items=10)
        assert args[-1] == "https://x.com/u/status/1"
        assert "--range" in args and args[args.index("--range") + 1] == "1-10"
        assert "--directory" in args and args[args.index("--directory") + 1] == str(tmp_path)
        assert "--config-ignore" in args  # never read the host user's config
        assert "--no-input" in args  # never block on a password prompt
        assert "--cookies" not in args

    def test_cache_stays_inside_dest(self, tmp_path):
        args = gallerydl.build_command("https://u.rl/x", tmp_path, max_items=5)
        cache = args[args.index("--cache-file") + 1]
        assert str(tmp_path) in cache

    def test_cookies_flag_when_configured(self, tmp_path):
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        gallerydl.configure(cookies_file=cookies)
        assert gallerydl.cookies_configured()
        args = gallerydl.build_command("https://u.rl/x", tmp_path, max_items=10)
        assert args[args.index("--cookies") + 1] == str(cookies)

    def test_missing_cookies_file_is_ignored(self, tmp_path):
        gallerydl.configure(cookies_file=tmp_path / "nope.txt")
        assert not gallerydl.cookies_configured()


class TestClassifyFailure:
    @pytest.mark.parametrize(
        "stderr",
        [
            "[instagram][error] AuthorizationError: Login required",
            "HTTP redirect to login page (401 Unauthorized)",
            "[twitter][error] authentication required",
        ],
    )
    def test_auth(self, stderr):
        exc = gallerydl.classify_failure(stderr, "https://u.rl/x")
        assert isinstance(exc, gallerydl.AuthRequiredError)
        assert exc.message_key == "error.login_required"

    @pytest.mark.parametrize(
        "stderr",
        [
            "error: Unsupported URL 'https://example.org/a'",
            "No suitable extractor found for 'https://example.org/a'",
        ],
    )
    def test_unsupported(self, stderr):
        assert isinstance(
            gallerydl.classify_failure(stderr, "https://u.rl/x"), UnsupportedUrlError
        )

    @pytest.mark.parametrize(
        "stderr",
        [
            "[downloader.http][warning] HTTP Error 429: Too Many Requests",
            "rate limit exceeded, try again later",
            "connection to server timed out",
        ],
    )
    def test_transient(self, stderr):
        assert isinstance(
            gallerydl.classify_failure(stderr, "https://u.rl/x"), TransientExtractionError
        )

    def test_unknown_is_generic_extraction_error(self):
        exc = gallerydl.classify_failure("some novel failure", "https://u.rl/x")
        assert isinstance(exc, ExtractionError)
        assert not isinstance(exc, TransientExtractionError)
        assert not isinstance(exc, gallerydl.AuthRequiredError)


class TestCollectFiles:
    def test_orders_by_mtime(self, tmp_path):
        second = tmp_path / "b.jpg"
        second.write_bytes(b"2")
        first = tmp_path / "z.jpg"  # name would sort last; mtime must win
        first.write_bytes(b"1")
        now = time.time()
        os.utime(first, (now - 100, now - 100))
        os.utime(second, (now, now))
        assert gallerydl.collect_files(tmp_path) == [first, second]

    def test_skips_hidden_part_and_dirs(self, tmp_path):
        keep = tmp_path / "photo.jpg"
        keep.write_bytes(b"x")
        (tmp_path / ".gdl-cache.sqlite3").write_bytes(b"x")
        (tmp_path / "video.mp4.part").write_bytes(b"x")
        (tmp_path / "subdir").mkdir()
        assert gallerydl.collect_files(tmp_path) == [keep]

    def test_missing_dir_is_empty(self, tmp_path):
        assert gallerydl.collect_files(tmp_path / "nope") == []


class TestFetch:
    def _stub_run(self, monkeypatch, *, code=0, stderr="", files=()):
        async def fake_run(args):
            dest = Path(args[args.index("--directory") + 1])
            for name in files:
                (dest / name).write_bytes(b"data")
            return code, stderr

        monkeypatch.setattr(gallerydl, "_run", fake_run)

    async def test_success_returns_files(self, monkeypatch, tmp_path):
        self._stub_run(monkeypatch, files=("a.jpg", "b.jpg"))
        paths = await gallerydl.fetch("https://u.rl/x", tmp_path)
        assert [p.name for p in paths] == ["a.jpg", "b.jpg"]
        assert all(p.parent == tmp_path / gallerydl.DEST_SUBDIR for p in paths)

    async def test_failure_with_no_files_is_classified(self, monkeypatch, tmp_path):
        self._stub_run(monkeypatch, code=32, stderr="AuthorizationError: Login required")
        with pytest.raises(gallerydl.AuthRequiredError):
            await gallerydl.fetch("https://u.rl/x", tmp_path)

    async def test_zero_files_without_error_raises(self, monkeypatch, tmp_path):
        self._stub_run(monkeypatch, code=0, stderr="")
        with pytest.raises(ExtractionError):
            await gallerydl.fetch("https://u.rl/x", tmp_path)

    async def test_partial_failure_keeps_downloaded_files(self, monkeypatch, tmp_path):
        self._stub_run(monkeypatch, code=4, stderr="HTTP Error 500", files=("a.jpg",))
        paths = await gallerydl.fetch("https://u.rl/x", tmp_path)
        assert [p.name for p in paths] == ["a.jpg"]

    async def test_respects_max_items(self, monkeypatch, tmp_path):
        self._stub_run(monkeypatch, files=tuple(f"{i:02d}.jpg" for i in range(15)))
        paths = await gallerydl.fetch("https://u.rl/x", tmp_path, max_items=10)
        assert len(paths) == 10
