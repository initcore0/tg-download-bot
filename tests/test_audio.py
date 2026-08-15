"""download_audio() — the /mp3 pipeline, with yt-dlp mocked out.

Same shape as test_downloader.py: yt-dlp is replaced by a fake that copies a generated
fixture into the workdir, so ffprobe and the m4a step run for real.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from tests.conftest import requires_ffmpeg
from tgdl.downloader import audio as audio_mod
from tgdl.downloader import transcode as tc
from tgdl.downloader import ytdlp
from tgdl.downloader.models import (
    DownloadTimeoutError,
    ExtractionError,
    MediaTooLargeError,
    UnsupportedUrlError,
)

pytestmark = requires_ffmpeg

CAP = 48 * 1024 * 1024


def fake_entry(path: Path, **overrides) -> dict:
    entry = {
        "id": path.stem,
        "title": "A Song",
        "artist": "A Band",
        "duration": 2.0,
        "ext": path.suffix.lstrip("."),
        "requested_downloads": [{"filepath": str(path)}],
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def stub_extract(monkeypatch):
    """Install a fake ytdlp.extract that copies a fixture into the workdir.

    Records the kwargs it was called with, so the format_override plumbing can be
    asserted without touching the real retry ladder.
    """
    seen: dict = {}

    def install(source: Path | None, *, entry_overrides: dict | None = None, error=None):
        async def _extract(url, workdir, **kwargs):
            seen.update(kwargs)
            if error is not None:
                raise error
            dst = Path(workdir) / f"track{source.suffix}"
            shutil.copy(source, dst)
            return [fake_entry(dst, **(entry_overrides or {}))]

        monkeypatch.setattr(audio_mod.ytdlp, "extract", _extract)
        return seen

    install.seen = seen
    return install


class TestHappyPaths:
    async def test_m4a_source_is_sent_untouched(self, stub_extract, aac_m4a, tmp_path):
        """The whole point of shipping m4a: an AAC source costs zero ffmpeg work."""
        stub_extract(aac_m4a)

        result = await audio_mod.download_audio(
            "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
        )

        assert result.path.suffix == ".m4a"
        assert result.path.name == "track.m4a", "converted instead of passing through"
        assert result.filesize == result.path.stat().st_size

    async def test_opus_source_is_converted_to_m4a(self, stub_extract, opus_webm, tmp_path):
        stub_extract(opus_webm)

        result = await audio_mod.download_audio(
            "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
        )

        assert result.path.suffix == ".m4a"
        info = await tc.probe(result.path)
        assert info.audio_codec == "aac"
        assert info.has_video is False

    async def test_metadata_is_carried_through(self, stub_extract, aac_m4a, tmp_path):
        stub_extract(aac_m4a)

        result = await audio_mod.download_audio(
            "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
        )

        assert result.title == "A Song"
        assert result.performer == "A Band"
        assert result.duration_s == pytest.approx(2.0)

    async def test_track_and_uploader_are_used_as_fallbacks(
        self, stub_extract, aac_m4a, tmp_path
    ):
        stub_extract(
            aac_m4a,
            entry_overrides={"track": "Real Title", "artist": None, "uploader": "Chan"},
        )

        result = await audio_mod.download_audio(
            "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
        )

        assert result.title == "Real Title"
        assert result.performer == "Chan"

    async def test_missing_metadata_is_none_not_an_error(
        self, stub_extract, aac_m4a, tmp_path
    ):
        stub_extract(
            aac_m4a,
            entry_overrides={"title": None, "artist": None, "uploader": None, "duration": None},
        )

        result = await audio_mod.download_audio(
            "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
        )

        assert result.title is None
        assert result.performer is None
        assert result.duration_s is None

    async def test_result_lives_inside_the_workdir(self, stub_extract, opus_webm, tmp_path):
        stub_extract(opus_webm)

        result = await audio_mod.download_audio(
            "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
        )

        assert tmp_path in result.path.parents

    async def test_workdir_is_created(self, stub_extract, aac_m4a, tmp_path):
        target = tmp_path / "nested" / "deeper"
        stub_extract(aac_m4a)

        result = await audio_mod.download_audio(
            "https://youtube.com/watch?v=x", target, max_size_bytes=CAP
        )

        assert target.exists() and result.path.exists()


class TestFormatOverride:
    """The audio selector must reach yt-dlp — otherwise we'd download whole videos."""

    async def test_extract_is_asked_for_audio_only(self, stub_extract, aac_m4a, tmp_path):
        seen = stub_extract(aac_m4a)

        await audio_mod.download_audio(
            "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
        )

        assert seen["format_override"] == audio_mod.AUDIO_FORMAT
        assert "m4a" in seen["format_override"]

    async def test_extract_gets_a_bounded_early_abort_ceiling(
        self, stub_extract, aac_m4a, tmp_path
    ):
        """A hostile server can't stream an unbounded file: extract carries a hard cap."""
        seen = stub_extract(aac_m4a)

        await audio_mod.download_audio(
            "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
        )

        assert seen["max_filesize_bytes"] == max(3 * CAP, audio_mod._MIN_AUDIO_CEILING_BYTES)

    def test_build_options_uses_the_override(self, tmp_path):
        opts = ytdlp.build_options(
            tmp_path, max_height=720, format_override="ba[ext=m4a]/bestaudio"
        )

        assert opts["format"] == "ba[ext=m4a]/bestaudio"
        # The video-shaped sort and the mp4 merge would both be wrong here.
        assert opts["format_sort"] == []
        assert "merge_output_format" not in opts

    def test_build_options_is_unchanged_without_an_override(self, tmp_path):
        opts = ytdlp.build_options(tmp_path, max_height=720)

        assert opts["format"] == ytdlp.build_format_selector(720)
        assert opts["format_sort"] == ["res:720", "codec:h264", "br"]
        assert opts["merge_output_format"] == "mp4"

    async def test_extract_passes_the_override_to_build_options(self, monkeypatch, tmp_path):
        seen: dict = {}

        def _build(workdir, **kwargs):
            seen.update(kwargs)
            return {}

        async def _to_thread(fn, *args, **kwargs):
            return {"id": "x", "requested_downloads": []}

        monkeypatch.setattr(ytdlp, "build_options", _build)
        monkeypatch.setattr(ytdlp.asyncio, "to_thread", _to_thread)

        await ytdlp.extract(
            "https://example.com/x", tmp_path, max_height=720, format_override="bestaudio"
        )

        assert seen["format_override"] == "bestaudio"


class TestErrors:
    async def test_empty_url_is_unsupported(self, tmp_path):
        with pytest.raises(UnsupportedUrlError):
            await audio_mod.download_audio("   ", tmp_path, max_size_bytes=CAP)

    async def test_internal_host_is_blocked(self, tmp_path):
        """Same SSRF guard as download_media — the extractor never sees localhost."""
        with pytest.raises(UnsupportedUrlError):
            await audio_mod.download_audio(
                "http://127.0.0.1/secret.mp3", tmp_path, max_size_bytes=CAP
            )

    async def test_over_the_cap_raises_too_large(self, stub_extract, aac_m4a, tmp_path):
        """No retry ladder for audio: over the cap is simply over the cap."""
        stub_extract(aac_m4a)

        with pytest.raises(MediaTooLargeError):
            await audio_mod.download_audio(
                "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=10
            )

    async def test_extraction_error_propagates(self, stub_extract, aac_m4a, tmp_path):
        stub_extract(aac_m4a, error=ExtractionError("no audio here"))

        with pytest.raises(ExtractionError):
            await audio_mod.download_audio(
                "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
            )

    async def test_no_file_produced_is_an_extraction_error(self, monkeypatch, tmp_path):
        async def _extract(url, workdir, **kwargs):
            return [{"id": "x", "requested_downloads": []}]

        monkeypatch.setattr(audio_mod.ytdlp, "extract", _extract)

        with pytest.raises(ExtractionError):
            await audio_mod.download_audio(
                "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
            )

    async def test_timeout_is_wrapped(self, monkeypatch, tmp_path):
        async def _slow(url, workdir, **kwargs):
            await asyncio.sleep(5)

        monkeypatch.setattr(audio_mod.ytdlp, "extract", _slow)

        with pytest.raises(DownloadTimeoutError):
            await audio_mod.download_audio(
                "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP, timeout_s=0.05
            )

    async def test_unexpected_error_becomes_a_download_error(self, monkeypatch, tmp_path):
        async def _boom(url, workdir, **kwargs):
            raise ValueError("something odd")

        monkeypatch.setattr(audio_mod.ytdlp, "extract", _boom)

        from tgdl.downloader.models import DownloadError

        with pytest.raises(DownloadError):
            await audio_mod.download_audio(
                "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
            )


class TestToM4a:
    async def test_aac_track_is_stream_copied(self, aac_m4a, tmp_path):
        src = tmp_path / "in.m4a"
        shutil.copy(aac_m4a, src)

        out = await tc.to_m4a(src)

        info = await tc.probe(out)
        assert info.audio_codec == "aac"
        assert out.suffix == ".m4a"

    async def test_video_stream_is_dropped(self, h264_mp4, tmp_path):
        """A music video's picture is dead weight in an audio message."""
        src = tmp_path / "in.mp4"
        shutil.copy(h264_mp4, src)

        out = await tc.to_m4a(src)

        info = await tc.probe(out)
        assert info.has_video is False
        assert info.has_audio is True

    async def test_mp4_with_video_is_not_passed_through(self, stub_extract, h264_mp4, tmp_path):
        """`.mp4` is in the passthrough extension set, but only audio-only files qualify."""
        stub_extract(h264_mp4)

        result = await audio_mod.download_audio(
            "https://youtube.com/watch?v=x", tmp_path, max_size_bytes=CAP
        )

        info = await tc.probe(result.path)
        assert info.has_video is False
