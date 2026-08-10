"""yt-dlp wrapper: error classification, retry/backoff, client fallback.

No ffmpeg or network needed — the blocking `_extract_sync` is monkeypatched.
"""
from __future__ import annotations

import pytest

from tgdl.downloader import ytdlp
from tgdl.downloader.models import (
    ExtractionError,
    TransientExtractionError,
    UnsupportedUrlError,
)


class TestClassify:
    @pytest.mark.parametrize(
        "message",
        [
            "ERROR: unsupported url: https://foo.invalid/x",
            "Unsupported URL: https://foo",
            "No media found",
        ],
    )
    def test_unsupported(self, message):
        assert isinstance(ytdlp._classify(Exception(message)), UnsupportedUrlError)

    @pytest.mark.parametrize(
        "message",
        [
            "ERROR: Private video. Sign in if you've been granted access",
            "Video unavailable. This video has been removed by the user",
            "This video is not available",
            "This live event will begin in 3 hours",
        ],
    )
    def test_permanent_extraction(self, message):
        exc = ytdlp._classify(Exception(message))
        assert isinstance(exc, ExtractionError)
        assert not isinstance(exc, TransientExtractionError)

    @pytest.mark.parametrize(
        "message",
        [
            "ERROR: unable to download webpage: HTTP Error 403: Forbidden",
            "HTTP Error 429: Too Many Requests",
            "Sign in to confirm you're not a bot",
            "HTTP Error 503: Service Unavailable",
            "giving up after 3 fragment retries",
            "The read operation timed out",
            "Requested format is not available",
            "some phrasing we have never catalogued",  # unknown -> transient
        ],
    )
    def test_transient(self, message):
        assert isinstance(ytdlp._classify(Exception(message)), TransientExtractionError)


class TestRetry:
    async def test_retries_transient_then_succeeds(self, monkeypatch, tmp_path):
        calls = {"n": 0}

        def fake_sync(url, opts, *, download):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientExtractionError("HTTP Error 403")
            return {"id": "vid", "title": "ok", "ext": "mp4"}

        monkeypatch.setattr(ytdlp, "_extract_sync", fake_sync)
        monkeypatch.setattr(ytdlp.asyncio, "sleep", _no_sleep)

        entries = await ytdlp.extract(
            "https://youtube.com/shorts/abc", tmp_path, max_height=720
        )
        assert calls["n"] == 3
        assert entries[0]["id"] == "vid"

    async def test_permanent_error_not_retried(self, monkeypatch, tmp_path):
        calls = {"n": 0}

        def fake_sync(url, opts, *, download):
            calls["n"] += 1
            raise UnsupportedUrlError("Unsupported URL: https://x")

        monkeypatch.setattr(ytdlp, "_extract_sync", fake_sync)
        monkeypatch.setattr(ytdlp.asyncio, "sleep", _no_sleep)

        with pytest.raises(UnsupportedUrlError):
            await ytdlp.extract("https://x.invalid/a", tmp_path, max_height=720)
        assert calls["n"] == 1  # no retries

    async def test_gives_up_after_max_attempts_with_transient(self, monkeypatch, tmp_path):
        calls = {"n": 0}

        def fake_sync(url, opts, *, download):
            calls["n"] += 1
            raise TransientExtractionError("HTTP Error 429")

        monkeypatch.setattr(ytdlp, "_extract_sync", fake_sync)
        monkeypatch.setattr(ytdlp.asyncio, "sleep", _no_sleep)

        with pytest.raises(TransientExtractionError):
            await ytdlp.extract("https://youtube.com/x", tmp_path, max_height=720, max_attempts=3)
        assert calls["n"] == 3

    async def test_cycles_youtube_clients_across_attempts(self, monkeypatch, tmp_path):
        seen_clients: list[list[str]] = []

        def fake_sync(url, opts, *, download):
            args = opts.get("extractor_args", {}).get("youtube", {})
            seen_clients.append(args.get("player_client", []))
            raise TransientExtractionError("HTTP Error 403")

        monkeypatch.setattr(ytdlp, "_extract_sync", fake_sync)
        monkeypatch.setattr(ytdlp.asyncio, "sleep", _no_sleep)

        with pytest.raises(TransientExtractionError):
            await ytdlp.extract("https://youtube.com/x", tmp_path, max_height=720, max_attempts=4)

        # First attempt uses the default (no forced client); later attempts force
        # different client lists.
        assert seen_clients[0] == []
        assert seen_clients[1] == ["android", "web"]
        assert seen_clients[2] == ["ios"]
        assert seen_clients[3] == ["tv"]


class TestBuildOptions:
    def test_confined_to_workdir(self, tmp_path):
        opts = ytdlp.build_options(tmp_path, max_height=720)
        assert str(tmp_path) in opts["outtmpl"]
        assert opts["paths"]["home"] == str(tmp_path)
        assert str(tmp_path) in opts["cachedir"]
        assert opts["noplaylist"] is True
        assert "extractor_args" not in opts

    def test_gallery_enables_playlist(self, tmp_path):
        opts = ytdlp.build_options(tmp_path, max_height=720, playlist_items="1-10")
        assert opts["noplaylist"] is False
        assert opts["playlist_items"] == "1-10"

    def test_forced_clients(self, tmp_path):
        opts = ytdlp.build_options(tmp_path, max_height=720, youtube_clients=("ios",))
        assert opts["extractor_args"]["youtube"]["player_client"] == ["ios"]

    def test_format_selector_respects_height(self):
        sel = ytdlp.build_format_selector(480)
        assert "height<=480" in sel
        assert "vcodec^=avc1" in sel  # prefer h264 first


async def _no_sleep(_seconds):
    return None
