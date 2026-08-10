"""Live end-to-end downloads against real sites.

Marked `network` and deselected by default (`-m "not network"`). These are the only tests
that prove the yt-dlp format selection and the ffmpeg pipeline work against real sources.
"""
from __future__ import annotations

import pytest

from tgdl.downloader import transcode as tc
from tgdl.downloader.models import DownloadError
from tgdl.downloader.service import download_media

pytestmark = [pytest.mark.network, pytest.mark.asyncio]

CAP = 48 * 1024 * 1024

# "Me at the zoo" — the first YouTube video, ~19s, stable and unlikely to disappear.
YOUTUBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
# A short Creative Commons clip served as a plain MP4 by a non-YouTube host.
DIRECT_MP4_URL = (
    "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4"
)


async def assert_telegram_ready(result, *, expect_audio: bool | None = None):
    """Every live download must satisfy the same Telegram-compatibility contract."""
    assert result.path.exists(), "downloaded file is missing"
    assert result.filesize > 0
    assert result.filesize == result.path.stat().st_size
    assert result.filesize <= CAP, f"{result.filesize} bytes exceeds the 48 MB cap"

    info = await tc.probe(result.path)
    if result.kind in ("video", "animation"):
        assert info.container in {"mp4", "mov"}, f"container {info.container} is not mp4"
        assert info.video_codec == "h264", f"codec {info.video_codec} is not h264"
        if info.has_audio:
            assert info.audio_codec == "aac"
        assert info.height is not None and info.height <= 720
        # moov atom must precede mdat so Telegram can stream it.
        data = result.path.read_bytes()
        assert data.index(b"moov") < data.index(b"mdat"), "missing +faststart"
    if expect_audio is not None:
        assert info.has_audio is expect_audio


class TestYouTube:
    async def test_downloads_me_at_the_zoo(self, tmp_path):
        results = await download_media(
            YOUTUBE_URL, tmp_path, max_size_bytes=CAP, max_height=720, timeout_s=180
        )
        assert len(results) == 1
        result = results[0]

        await assert_telegram_ready(result)
        assert result.kind == "video"
        assert result.platform == "youtube"
        assert result.source_url == YOUTUBE_URL
        assert result.title and "zoo" in result.title.lower()
        # The clip is ~19 seconds.
        assert result.duration_s is not None and 15 < result.duration_s < 25
        assert result.width and result.height
        assert result.elapsed_s > 0
        assert tmp_path in result.path.parents

    async def test_respects_lower_max_height(self, tmp_path):
        results = await download_media(
            YOUTUBE_URL, tmp_path, max_size_bytes=CAP, max_height=360, timeout_s=180
        )
        assert results[0].height is not None and results[0].height <= 360

    async def test_size_cap_forces_compression(self, tmp_path):
        """A tiny cap must still yield a playable file via the 480p retry.

        The source is only ~500 KB at 240p, so the cap has to be under that to exercise
        the retry rather than passing straight through — but above the ~313 KB implied by
        the encoder bitrate floors for a 19s clip, which no re-encode can beat.
        """
        cap = 400_000
        results = await download_media(
            YOUTUBE_URL, tmp_path, max_size_bytes=cap, max_height=720, timeout_s=240
        )
        result = results[0]
        assert result.filesize <= cap
        assert result.transcoded is True
        info = await tc.probe(result.path)
        assert info.video_codec == "h264"
        assert info.height is not None and info.height <= 480


class TestDirectMp4:
    async def test_direct_mp4_url(self, tmp_path):
        results = await download_media(
            DIRECT_MP4_URL, tmp_path, max_size_bytes=CAP, max_height=720, timeout_s=180
        )
        assert len(results) == 1
        result = results[0]
        await assert_telegram_ready(result)
        assert result.platform == "other"
        assert result.duration_s is not None and result.duration_s > 0


class TestLiveErrors:
    async def test_nonexistent_video_raises_download_error(self, tmp_path):
        with pytest.raises(DownloadError) as excinfo:
            await download_media(
                "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                tmp_path,
                max_size_bytes=CAP,
                timeout_s=90,
            )
        assert excinfo.value.user_message

    async def test_non_media_page_raises_download_error(self, tmp_path):
        with pytest.raises(DownloadError) as excinfo:
            await download_media(
                "https://example.com/", tmp_path, max_size_bytes=CAP, timeout_s=90
            )
        assert excinfo.value.user_message

    async def test_short_timeout_raises(self, tmp_path):
        from tgdl.downloader.models import DownloadTimeoutError

        with pytest.raises(DownloadTimeoutError):
            await download_media(
                YOUTUBE_URL, tmp_path, max_size_bytes=CAP, max_height=720, timeout_s=1
            )
