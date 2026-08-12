"""download_media() orchestration, with yt-dlp mocked out.

yt-dlp is replaced by a fake that copies a generated fixture into the workdir, so the
rest of the pipeline (ffprobe, remux, transcode, size cap) runs for real.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from tests.conftest import requires_ffmpeg
from tgdl.downloader import service
from tgdl.downloader import transcode as tc
from tgdl.downloader.models import (
    DownloadError,
    DownloadTimeoutError,
    ExtractionError,
    MediaTooLargeError,
    TranscodeError,
    UnsupportedUrlError,
)

pytestmark = requires_ffmpeg

CAP = 48 * 1024 * 1024


def fake_entry(path: Path, **overrides) -> dict:
    """Minimal yt-dlp info dict pointing at an already-downloaded file."""
    entry = {
        "id": path.stem,
        "title": "Test Video",
        "ext": path.suffix.lstrip("."),
        "requested_downloads": [{"filepath": str(path)}],
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def stub_extract(monkeypatch):
    """Install a fake ytdlp.extract that copies fixtures into the workdir."""

    def install(sources: list[Path], *, entry_overrides: list[dict] | None = None, error=None):
        async def _extract(url, workdir, *, max_height, playlist_items=None, download=True):
            if error is not None:
                raise error
            entries = []
            for index, src in enumerate(sources):
                dst = Path(workdir) / f"item{index}{src.suffix}"
                shutil.copy(src, dst)
                extra = (entry_overrides or [{}] * len(sources))[index]
                entries.append(fake_entry(dst, **extra))
            return entries

        monkeypatch.setattr(service.ytdlp, "extract", _extract)

    return install


class TestHappyPaths:
    async def test_mp4_passthrough_is_not_transcoded(self, stub_extract, h264_mp4, tmp_path):
        stub_extract([h264_mp4])
        results = await service.download_media(
            "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
        )
        assert len(results) == 1
        result = results[0]
        assert result.kind == "video"
        assert result.transcoded is False
        assert result.path.exists()
        assert result.width == 320 and result.height == 240
        assert result.duration_s == pytest.approx(2.0, abs=0.3)
        assert result.filesize == result.path.stat().st_size
        assert result.platform == "youtube"
        assert result.title == "Test Video"
        assert result.source_url == "https://youtube.com/watch?v=x"
        assert result.elapsed_s > 0

    async def test_mkv_is_remuxed_not_transcoded(self, stub_extract, h264_mkv, tmp_path):
        stub_extract([h264_mkv])
        (result,) = await service.download_media("https://example.com/v", tmp_path, max_size_bytes=CAP)
        # A remux preserves the streams, so `transcoded` stays False.
        assert result.transcoded is False
        assert result.path.suffix == ".mp4"
        info = await tc.probe(result.path)
        assert info.container == "mp4" and info.video_codec == "h264"

    async def test_vp9_is_transcoded(self, stub_extract, vp9_webm, tmp_path):
        stub_extract([vp9_webm])
        (result,) = await service.download_media("https://example.com/v", tmp_path, max_size_bytes=CAP)
        assert result.transcoded is True
        info = await tc.probe(result.path)
        assert info.video_codec == "h264" and info.audio_codec == "aac"

    async def test_oversized_frame_is_downscaled(self, stub_extract, tall_mp4, tmp_path):
        stub_extract([tall_mp4])
        (result,) = await service.download_media(
            "https://example.com/v", tmp_path, max_size_bytes=CAP, max_height=720
        )
        assert result.height is not None and result.height <= 720
        assert result.transcoded is True

    async def test_results_live_inside_workdir(self, stub_extract, vp9_webm, tmp_path):
        stub_extract([vp9_webm])
        (result,) = await service.download_media("https://example.com/v", tmp_path, max_size_bytes=CAP)
        assert tmp_path in result.path.parents

    async def test_workdir_is_created(self, stub_extract, h264_mp4, tmp_path):
        target = tmp_path / "nested" / "deeper"
        stub_extract([h264_mp4])
        results = await service.download_media(
            "https://example.com/v", target, max_size_bytes=CAP
        )
        assert target.exists() and results[0].path.exists()


class TestKinds:
    async def test_silent_short_video_is_video_not_animation(
        self, stub_extract, silent_mp4, tmp_path
    ):
        # Regression: muted videos were being sent as looping GIF-style animations.
        stub_extract([silent_mp4])
        (result,) = await service.download_media("https://example.com/v", tmp_path, max_size_bytes=CAP)
        assert result.kind == "video"

    async def test_gif_becomes_animation_mp4(self, stub_extract, gif_file, tmp_path):
        stub_extract([gif_file])
        (result,) = await service.download_media("https://example.com/g", tmp_path, max_size_bytes=CAP)
        assert result.kind == "animation"
        info = await tc.probe(result.path)
        assert info.video_codec == "h264"

    async def test_jpg_passes_through_as_image(self, stub_extract, jpg_file, tmp_path):
        stub_extract([jpg_file])
        (result,) = await service.download_media(
            "https://instagram.com/p/x", tmp_path, max_size_bytes=CAP
        )
        assert result.kind == "image"
        assert result.transcoded is False
        assert result.duration_s is None
        assert (result.width, result.height) == (320, 240)

    async def test_webp_is_converted_to_jpg(self, stub_extract, webp_file, tmp_path):
        stub_extract([webp_file])
        (result,) = await service.download_media(
            "https://pinterest.com/pin/1", tmp_path, max_size_bytes=CAP
        )
        assert result.kind == "image"
        assert result.path.suffix == ".jpg"
        assert result.path.exists()


class TestGalleries:
    async def test_multiple_images_return_multiple_results(self, stub_extract, jpg_file, tmp_path):
        stub_extract([jpg_file, jpg_file, jpg_file])
        results = await service.download_media(
            "https://instagram.com/p/gallery", tmp_path, max_size_bytes=CAP
        )
        assert len(results) == 3
        assert all(r.kind == "image" for r in results)
        # Distinct files, not the same path repeated.
        assert len({r.path for r in results}) == 3

    async def test_gallery_capped_at_ten(self, stub_extract, jpg_file, tmp_path):
        stub_extract([jpg_file] * 15)
        results = await service.download_media(
            "https://instagram.com/p/big", tmp_path, max_size_bytes=CAP
        )
        assert len(results) == 10

    async def test_playlist_items_requested_for_gallery_platforms(self, monkeypatch, jpg_file, tmp_path):
        seen = {}

        async def _extract(url, workdir, *, max_height, playlist_items=None, download=True):
            seen["playlist_items"] = playlist_items
            dst = Path(workdir) / "a.jpg"
            shutil.copy(jpg_file, dst)
            return [fake_entry(dst)]

        monkeypatch.setattr(service.ytdlp, "extract", _extract)

        await service.download_media("https://instagram.com/p/x", tmp_path, max_size_bytes=CAP)
        assert seen["playlist_items"] == "1-10"

        await service.download_media("https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP)
        assert seen["playlist_items"] is None

    async def test_partial_gallery_failure_still_returns_good_items(
        self, monkeypatch, jpg_file, tmp_path
    ):
        async def _extract(url, workdir, *, max_height, playlist_items=None, download=True):
            good = Path(workdir) / "good.jpg"
            shutil.copy(jpg_file, good)
            bad = Path(workdir) / "bad.jpg"
            bad.write_bytes(b"not an image")
            return [fake_entry(good), fake_entry(bad)]

        monkeypatch.setattr(service.ytdlp, "extract", _extract)
        results = await service.download_media(
            "https://instagram.com/p/x", tmp_path, max_size_bytes=CAP
        )
        assert len(results) == 1
        assert results[0].path.name == "good.jpg"


class TestImageFallback:
    """yt-dlp finds no video -> the gallery-dl image engine takes over."""

    def _stub_fetch(self, monkeypatch, sources: list[Path] | None, *, error=None, calls=None):
        async def _fetch(url, workdir, *, max_items=10):
            if calls is not None:
                calls.append(url)
            if error is not None:
                raise error
            dest = Path(workdir) / service.gallerydl.DEST_SUBDIR
            dest.mkdir(parents=True, exist_ok=True)
            out = []
            for index, src in enumerate(sources or []):
                dst = dest / f"item{index}{src.suffix}"
                shutil.copy(src, dst)
                out.append(dst)
            return out

        monkeypatch.setattr(service.gallerydl, "fetch", _fetch)

    async def test_no_video_post_falls_back_to_images(
        self, stub_extract, monkeypatch, jpg_file, tmp_path
    ):
        stub_extract([], error=ExtractionError("There is no video in this post"))
        self._stub_fetch(monkeypatch, [jpg_file, jpg_file])
        results = await service.download_media(
            "https://instagram.com/p/photos", tmp_path, max_size_bytes=CAP
        )
        assert len(results) == 2
        assert all(r.kind == "image" for r in results)
        assert all(r.platform == "instagram" for r in results)

    async def test_fallback_handles_story_videos_too(
        self, stub_extract, monkeypatch, jpg_file, h264_mp4, tmp_path
    ):
        stub_extract([], error=ExtractionError("no video"))
        self._stub_fetch(monkeypatch, [jpg_file, h264_mp4])
        results = await service.download_media(
            "https://twitter.com/u/status/1", tmp_path, max_size_bytes=CAP
        )
        assert [r.kind for r in results] == ["image", "video"]

    async def test_fallback_failure_reraises_original_error(
        self, stub_extract, monkeypatch, tmp_path
    ):
        original = ExtractionError("There is no video in this post")
        stub_extract([], error=original)
        self._stub_fetch(monkeypatch, None, error=ExtractionError("gallery-dl failed too"))
        with pytest.raises(ExtractionError) as excinfo:
            await service.download_media(
                "https://instagram.com/p/x", tmp_path, max_size_bytes=CAP
            )
        assert excinfo.value is original

    async def test_fallback_auth_error_wins_over_original(
        self, stub_extract, monkeypatch, tmp_path
    ):
        from tgdl.downloader.gallerydl import AuthRequiredError

        stub_extract([], error=ExtractionError("Requested content is not available"))
        self._stub_fetch(monkeypatch, None, error=AuthRequiredError("login required"))
        with pytest.raises(AuthRequiredError):
            await service.download_media(
                "https://instagram.com/p/x", tmp_path, max_size_bytes=CAP
            )

    async def test_unsupported_url_also_tries_fallback(
        self, stub_extract, monkeypatch, jpg_file, tmp_path
    ):
        # gallery-dl covers image hosts yt-dlp has no extractor for.
        stub_extract([], error=UnsupportedUrlError("Unsupported URL"))
        self._stub_fetch(monkeypatch, [jpg_file])
        (result,) = await service.download_media(
            "https://pinterest.com/pin/123", tmp_path, max_size_bytes=CAP
        )
        assert result.kind == "image"

    async def test_success_never_touches_fallback(
        self, stub_extract, monkeypatch, h264_mp4, tmp_path
    ):
        calls: list[str] = []
        stub_extract([h264_mp4])
        self._stub_fetch(monkeypatch, [], calls=calls)
        await service.download_media("https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP)
        assert calls == []

    async def test_corrupt_fallback_file_among_good_ones_is_skipped(
        self, stub_extract, monkeypatch, jpg_file, tmp_path
    ):
        stub_extract([], error=ExtractionError("no video"))

        async def _fetch(url, workdir, *, max_items=10):
            dest = Path(workdir) / service.gallerydl.DEST_SUBDIR
            dest.mkdir(parents=True, exist_ok=True)
            good = dest / "good.jpg"
            shutil.copy(jpg_file, good)
            bad = dest / "bad.jpg"
            bad.write_bytes(b"not an image")
            return [good, bad]

        monkeypatch.setattr(service.gallerydl, "fetch", _fetch)
        results = await service.download_media(
            "https://instagram.com/p/x", tmp_path, max_size_bytes=CAP
        )
        assert [r.path.name for r in results] == ["good.jpg"]


class TestStories:
    async def test_story_without_cookies_fails_fast_with_auth_error(
        self, monkeypatch, tmp_path
    ):
        from tgdl.downloader.gallerydl import AuthRequiredError

        called = []

        async def _extract(*args, **kwargs):
            called.append(True)
            raise AssertionError("ytdlp.extract must not run for cookie-less stories")

        monkeypatch.setattr(service.ytdlp, "extract", _extract)
        monkeypatch.setattr(service.gallerydl, "cookies_configured", lambda: False)
        with pytest.raises(AuthRequiredError):
            await service.download_media(
                "https://www.instagram.com/stories/someuser/123456/",
                tmp_path,
                max_size_bytes=CAP,
            )
        assert not called

    async def test_story_with_cookies_goes_through_normal_pipeline(
        self, stub_extract, monkeypatch, h264_mp4, tmp_path
    ):
        monkeypatch.setattr(service.gallerydl, "cookies_configured", lambda: True)
        stub_extract([h264_mp4])
        (result,) = await service.download_media(
            "https://instagram.com/stories/someuser/123/", tmp_path, max_size_bytes=CAP
        )
        assert result.kind == "video"

    async def test_non_story_instagram_needs_no_cookies(
        self, stub_extract, monkeypatch, h264_mp4, tmp_path
    ):
        monkeypatch.setattr(service.gallerydl, "cookies_configured", lambda: False)
        stub_extract([h264_mp4])
        (result,) = await service.download_media(
            "https://instagram.com/reel/abc/", tmp_path, max_size_bytes=CAP
        )
        assert result.kind == "video"


class TestSizeCap:
    async def test_under_cap_is_untouched(self, stub_extract, h264_mp4, tmp_path):
        stub_extract([h264_mp4])
        (result,) = await service.download_media("https://example.com/v", tmp_path, max_size_bytes=CAP)
        assert result.filesize <= CAP
        assert result.transcoded is False

    async def test_over_cap_triggers_480p_retry(self, stub_extract, tall_mp4, tmp_path):
        # Cap below the natural 720p encode (~21 KB) so exactly one retry is needed,
        # but above what a 480p bitrate-targeted encode produces (~11 KB).
        stub_extract([tall_mp4])
        cap = 16_000
        (result,) = await service.download_media(
            "https://example.com/v", tmp_path, max_size_bytes=cap, max_height=720
        )
        assert result.filesize <= cap
        assert result.transcoded is True
        info = await tc.probe(result.path)
        assert info.height is not None and info.height <= service.RETRY_HEIGHT

    async def test_still_too_large_raises(self, stub_extract, tall_mp4, tmp_path):
        stub_extract([tall_mp4])
        with pytest.raises(MediaTooLargeError) as excinfo:
            await service.download_media(
                "https://example.com/v", tmp_path, max_size_bytes=2_000, max_height=720
            )
        assert "too large" in excinfo.value.user_message.lower()

    async def test_retry_uses_computed_bitrate(self, monkeypatch, stub_extract, tall_mp4, tmp_path):
        calls = []
        real_transcode = tc.transcode

        async def spy(src, dst=None, **kwargs):
            calls.append(kwargs)
            return await real_transcode(src, dst, **kwargs)

        monkeypatch.setattr(service.tc, "transcode", spy)
        stub_extract([tall_mp4])
        cap = 16_000
        await service.download_media(
            "https://example.com/v", tmp_path, max_size_bytes=cap, max_height=720
        )
        retry = [c for c in calls if c.get("video_bitrate") is not None]
        assert len(retry) == 1, "expected exactly one bitrate-targeted retry"
        assert retry[0]["max_height"] == service.RETRY_HEIGHT
        # Bitrate must match the documented formula for the probed duration.
        info = await tc.probe(tall_mp4)
        assert retry[0]["video_bitrate"] == tc.target_video_bitrate(cap, info.duration_s)

    async def test_only_one_retry_attempted(self, monkeypatch, stub_extract, tall_mp4, tmp_path):
        attempts = []
        real_transcode = tc.transcode

        async def spy(src, dst=None, **kwargs):
            if kwargs.get("video_bitrate") is not None:
                attempts.append(kwargs)
            return await real_transcode(src, dst, **kwargs)

        monkeypatch.setattr(service.tc, "transcode", spy)
        stub_extract([tall_mp4])
        with pytest.raises(MediaTooLargeError):
            await service.download_media(
                "https://example.com/v", tmp_path, max_size_bytes=2_000, max_height=720
            )
        assert len(attempts) == 1

    async def test_no_duration_cannot_retry(self, monkeypatch, stub_extract, jpg_file, tmp_path):
        # An oversized video with unknown duration has no bitrate target to compute,
        # so it must fail fast rather than attempt a meaningless retry.
        stub_extract([jpg_file])

        async def fake_probe(path):
            return tc.MediaInfo(
                path=path,
                container="mp4",
                video_codec="h264",
                audio_codec="aac",
                width=320,
                height=240,
                duration_s=None,
                has_video=True,
                has_audio=True,
                is_image=False,
            )

        monkeypatch.setattr(service.tc, "probe", fake_probe)
        with pytest.raises(MediaTooLargeError):
            await service.download_media("https://example.com/v", tmp_path, max_size_bytes=10)


class TestErrorMapping:
    async def test_empty_url_is_unsupported(self, tmp_path):
        with pytest.raises(UnsupportedUrlError):
            await service.download_media("", tmp_path, max_size_bytes=CAP)
        with pytest.raises(UnsupportedUrlError):
            await service.download_media("   ", tmp_path, max_size_bytes=CAP)

    async def test_unsupported_url_propagates(self, stub_extract, tmp_path):
        stub_extract([], error=UnsupportedUrlError("Unsupported URL: https://x"))
        with pytest.raises(UnsupportedUrlError):
            await service.download_media("https://x.invalid/a", tmp_path, max_size_bytes=CAP)

    async def test_extraction_error_propagates(self, stub_extract, tmp_path):
        stub_extract([], error=ExtractionError("video is private"))
        with pytest.raises(ExtractionError):
            await service.download_media("https://example.com/a", tmp_path, max_size_bytes=CAP)

    async def test_unexpected_exception_becomes_download_error(self, monkeypatch, tmp_path):
        async def boom(*args, **kwargs):
            raise ValueError("something odd happened")

        monkeypatch.setattr(service.ytdlp, "extract", boom)
        with pytest.raises(DownloadError) as excinfo:
            await service.download_media("https://example.com/a", tmp_path, max_size_bytes=CAP)
        # Must be normalized, not a bare ValueError.
        assert isinstance(excinfo.value, DownloadError)
        assert not isinstance(excinfo.value, ValueError)
        assert excinfo.value.user_message

    async def test_keyerror_from_ytdlp_is_wrapped(self, monkeypatch, tmp_path):
        async def boom(*args, **kwargs):
            raise KeyError("formats")

        monkeypatch.setattr(service.ytdlp, "extract", boom)
        with pytest.raises(DownloadError):
            await service.download_media("https://example.com/a", tmp_path, max_size_bytes=CAP)

    async def test_no_file_produced_raises_extraction_error(self, monkeypatch, tmp_path):
        async def _extract(url, workdir, *, max_height, playlist_items=None, download=True):
            return [{"id": "x", "title": "t"}]  # no requested_downloads / missing file

        monkeypatch.setattr(service.ytdlp, "extract", _extract)
        with pytest.raises(ExtractionError):
            await service.download_media("https://example.com/a", tmp_path, max_size_bytes=CAP)

    async def test_corrupt_media_raises_transcode_error(self, monkeypatch, tmp_path):
        async def _extract(url, workdir, *, max_height, playlist_items=None, download=True):
            bad = Path(workdir) / "bad.mp4"
            bad.write_bytes(b"not real media")
            return [fake_entry(bad)]

        monkeypatch.setattr(service.ytdlp, "extract", _extract)
        with pytest.raises(TranscodeError):
            await service.download_media("https://example.com/a", tmp_path, max_size_bytes=CAP)

    async def test_every_error_has_user_message(self):
        for cls in (
            UnsupportedUrlError,
            ExtractionError,
            MediaTooLargeError,
            TranscodeError,
            DownloadTimeoutError,
        ):
            assert cls("detail").user_message
            assert issubclass(cls, DownloadError)


class TestTimeout:
    async def test_slow_download_raises_timeout(self, monkeypatch, tmp_path):
        async def slow(*args, **kwargs):
            await asyncio.sleep(5)
            return []

        monkeypatch.setattr(service.ytdlp, "extract", slow)
        with pytest.raises(DownloadTimeoutError):
            await service.download_media(
                "https://example.com/a", tmp_path, max_size_bytes=CAP, timeout_s=1
            )

    async def test_timeout_error_has_user_message(self, monkeypatch, tmp_path):
        async def slow(*args, **kwargs):
            await asyncio.sleep(5)

        monkeypatch.setattr(service.ytdlp, "extract", slow)
        with pytest.raises(DownloadTimeoutError) as excinfo:
            await service.download_media(
                "https://example.com/a", tmp_path, max_size_bytes=CAP, timeout_s=1
            )
        assert "too long" in excinfo.value.user_message.lower()

    async def test_fast_download_does_not_time_out(self, stub_extract, h264_mp4, tmp_path):
        stub_extract([h264_mp4])
        results = await service.download_media(
            "https://example.com/a", tmp_path, max_size_bytes=CAP, timeout_s=60
        )
        assert len(results) == 1

    async def test_cancellation_is_not_swallowed(self, monkeypatch, tmp_path):
        async def slow(*args, **kwargs):
            await asyncio.sleep(30)

        monkeypatch.setattr(service.ytdlp, "extract", slow)
        task = asyncio.create_task(
            service.download_media("https://example.com/a", tmp_path, max_size_bytes=CAP)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestMetadata:
    async def test_platform_from_url(self, stub_extract, h264_mp4, tmp_path):
        for url, platform in [
            ("https://www.tiktok.com/@u/video/1", "tiktok"),
            ("https://x.com/u/status/1", "twitter"),
            ("https://clips.twitch.tv/abc", "twitch"),
            ("https://example.org/v.mp4", "other"),
        ]:
            stub_extract([h264_mp4])
            (result,) = await service.download_media(url, tmp_path, max_size_bytes=CAP)
            assert result.platform == platform

    async def test_title_from_ytdlp_info(self, stub_extract, h264_mp4, tmp_path):
        stub_extract([h264_mp4], entry_overrides=[{"title": "My Great Clip"}])
        (result,) = await service.download_media("https://example.com/v", tmp_path, max_size_bytes=CAP)
        assert result.title == "My Great Clip"

    async def test_missing_title_is_none(self, stub_extract, h264_mp4, tmp_path):
        stub_extract([h264_mp4], entry_overrides=[{"title": None, "description": None}])
        (result,) = await service.download_media("https://example.com/v", tmp_path, max_size_bytes=CAP)
        assert result.title is None

    async def test_long_title_is_truncated(self, stub_extract, h264_mp4, tmp_path):
        stub_extract([h264_mp4], entry_overrides=[{"title": "x" * 500}])
        (result,) = await service.download_media("https://example.com/v", tmp_path, max_size_bytes=CAP)
        assert result.title is not None and len(result.title) <= 200

    async def test_dimensions_come_from_ffprobe_not_ytdlp(self, stub_extract, h264_mp4, tmp_path):
        # yt-dlp claims 9999x9999; ffprobe is authoritative post-processing.
        stub_extract([h264_mp4], entry_overrides=[{"width": 9999, "height": 9999}])
        (result,) = await service.download_media("https://example.com/v", tmp_path, max_size_bytes=CAP)
        assert (result.width, result.height) == (320, 240)

    async def test_elapsed_shared_across_gallery_results(self, stub_extract, jpg_file, tmp_path):
        stub_extract([jpg_file, jpg_file])
        results = await service.download_media(
            "https://instagram.com/p/x", tmp_path, max_size_bytes=CAP
        )
        assert all(r.elapsed_s > 0 for r in results)
        assert len({r.elapsed_s for r in results}) == 1


class TestFormatSelection:
    def test_selector_prefers_h264_mp4_then_falls_back(self):
        from tgdl.downloader.ytdlp import build_format_selector

        selector = build_format_selector(720)
        parts = selector.split("/")
        assert parts[0] == "bv*[height<=720][ext=mp4][vcodec^=avc1]+ba[ext=m4a]"
        assert parts[1] == "b[height<=720][ext=mp4]"
        assert parts[-1] == "b"

    def test_selector_honours_max_height(self):
        from tgdl.downloader.ytdlp import build_format_selector

        assert "height<=480" in build_format_selector(480)

    def test_options_stay_inside_workdir(self, tmp_path):
        from tgdl.downloader.ytdlp import build_options

        opts = build_options(tmp_path, max_height=720)
        assert str(tmp_path) in opts["outtmpl"]
        assert str(tmp_path) in str(opts["cachedir"])
        assert opts["quiet"] is True
        assert opts["noplaylist"] is True
        assert opts["restrictfilenames"] is True

    def test_playlist_items_disables_noplaylist(self, tmp_path):
        from tgdl.downloader.ytdlp import build_options

        opts = build_options(tmp_path, max_height=720, playlist_items="1-10")
        assert opts["playlist_items"] == "1-10"
        assert opts["noplaylist"] is False
