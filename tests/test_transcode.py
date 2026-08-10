"""ffprobe/ffmpeg layer: compat decisions and the actual remux/transcode operations.

These run offline but use the real ffmpeg binary against tiny generated fixtures, so the
decision logic is validated against ffprobe's actual output rather than a mock of it.
"""
from __future__ import annotations

import pytest

from tests.conftest import requires_ffmpeg
from tgdl.downloader import transcode as tc
from tgdl.downloader.models import TranscodeError

pytestmark = requires_ffmpeg


class TestProbe:
    async def test_h264_mp4(self, h264_mp4):
        info = await tc.probe(h264_mp4)
        assert info.container == "mp4"
        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"
        assert (info.width, info.height) == (320, 240)
        assert info.duration_s == pytest.approx(2.0, abs=0.3)
        assert info.has_video and info.has_audio
        assert not info.is_image

    async def test_mkv_container_detected(self, h264_mkv):
        info = await tc.probe(h264_mkv)
        assert info.container == "matroska"
        assert info.video_codec == "h264"

    async def test_vp9_webm(self, vp9_webm):
        info = await tc.probe(vp9_webm)
        assert info.container == "webm"
        assert info.video_codec == "vp9"
        assert info.audio_codec == "opus"

    async def test_silent_video_has_no_audio(self, silent_mp4):
        info = await tc.probe(silent_mp4)
        assert info.has_video
        assert not info.has_audio
        assert info.audio_codec is None

    async def test_image_detected(self, jpg_file):
        info = await tc.probe(jpg_file)
        assert info.is_image
        assert (info.width, info.height) == (320, 240)

    async def test_webp_detected_as_image(self, webp_file):
        info = await tc.probe(webp_file)
        assert info.is_image

    async def test_gif_is_not_an_image(self, gif_file):
        info = await tc.probe(gif_file)
        assert not info.is_image
        assert info.container == "gif"

    async def test_missing_file_raises_transcode_error(self, tmp_path):
        with pytest.raises(TranscodeError):
            await tc.probe(tmp_path / "nope.mp4")

    async def test_garbage_file_raises_transcode_error(self, tmp_path):
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"this is definitely not a video")
        with pytest.raises(TranscodeError):
            await tc.probe(bad)


class TestDecide:
    async def test_mp4_h264_aac_is_passthrough(self, h264_mp4):
        assert tc.decide(await tc.probe(h264_mp4)) == tc.Decision.PASSTHROUGH

    async def test_mkv_h264_aac_is_remux(self, h264_mkv):
        info = await tc.probe(h264_mkv)
        assert tc.decide(info) == tc.Decision.REMUX
        assert info.needs_remux and not info.needs_transcode

    async def test_vp9_is_transcode(self, vp9_webm):
        info = await tc.probe(vp9_webm)
        assert tc.decide(info) == tc.Decision.TRANSCODE
        assert info.needs_transcode

    async def test_silent_h264_mp4_is_passthrough(self, silent_mp4):
        assert tc.decide(await tc.probe(silent_mp4)) == tc.Decision.PASSTHROUGH

    async def test_image_is_passthrough(self, jpg_file):
        info = await tc.probe(jpg_file)
        assert tc.decide(info) == tc.Decision.PASSTHROUGH
        assert not info.needs_transcode and not info.needs_remux

    def test_incompatible_audio_forces_transcode(self):
        info = tc.MediaInfo(
            path=__import__("pathlib").Path("x.mp4"),
            container="mp4",
            video_codec="h264",
            audio_codec="opus",
            width=320,
            height=240,
            duration_s=2.0,
            has_video=True,
            has_audio=True,
            is_image=False,
        )
        assert tc.decide(info) == tc.Decision.TRANSCODE

    def test_hevc_forces_transcode(self):
        info = tc.MediaInfo(
            path=__import__("pathlib").Path("x.mp4"),
            container="mp4",
            video_codec="hevc",
            audio_codec="aac",
            width=320,
            height=240,
            duration_s=2.0,
            has_video=True,
            has_audio=True,
            is_image=False,
        )
        assert tc.decide(info) == tc.Decision.TRANSCODE


class TestRemux:
    async def test_mkv_to_mp4_copies_streams(self, h264_mkv, tmp_path):
        dst = tmp_path / "out.mp4"
        result = await tc.remux(h264_mkv, dst)
        assert result == dst and dst.exists() and dst.stat().st_size > 0

        info = await tc.probe(dst)
        assert info.container == "mp4"
        # Stream copy: codecs and dimensions must be byte-identical in kind.
        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"
        assert (info.width, info.height) == (320, 240)

    async def test_remux_writes_faststart(self, h264_mkv, tmp_path):
        dst = tmp_path / "fs.mp4"
        await tc.remux(h264_mkv, dst)
        # With +faststart the moov atom precedes mdat.
        head = dst.read_bytes()
        assert head.index(b"moov") < head.index(b"mdat")

    async def test_remux_failure_raises(self, tmp_path):
        bad = tmp_path / "bad.mkv"
        bad.write_bytes(b"garbage")
        with pytest.raises(TranscodeError):
            await tc.remux(bad, tmp_path / "out.mp4")

    async def test_default_destination_is_sibling(self, h264_mkv):
        result = await tc.remux(h264_mkv)
        assert result.suffix == ".mp4"
        assert result.parent == h264_mkv.parent
        assert result != h264_mkv
        result.unlink()


class TestTranscode:
    async def test_vp9_becomes_h264_aac_mp4(self, vp9_webm, tmp_path):
        dst = await tc.transcode(vp9_webm, tmp_path / "out.mp4")
        info = await tc.probe(dst)
        assert info.container == "mp4"
        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"

    async def test_downscales_to_max_height(self, tall_mp4, tmp_path):
        dst = await tc.transcode(tall_mp4, tmp_path / "small.mp4", max_height=720)
        info = await tc.probe(dst)
        assert info.height is not None and info.height <= 720
        # Aspect ratio preserved (16:9 -> 1280x720).
        assert info.width == 1280 and info.height == 720

    async def test_dimensions_are_even(self, odd_size_mp4, tmp_path):
        dst = await tc.transcode(odd_size_mp4, tmp_path / "even.mp4", max_height=360)
        info = await tc.probe(dst)
        assert info.width % 2 == 0 and info.height % 2 == 0

    async def test_does_not_upscale_small_video(self, h264_mp4, tmp_path):
        dst = await tc.transcode(h264_mp4, tmp_path / "same.mp4", max_height=720)
        info = await tc.probe(dst)
        assert (info.width, info.height) == (320, 240)

    async def test_faststart_present(self, vp9_webm, tmp_path):
        dst = await tc.transcode(vp9_webm, tmp_path / "fs.mp4")
        data = dst.read_bytes()
        assert data.index(b"moov") < data.index(b"mdat")

    async def test_drop_audio(self, h264_mp4, tmp_path):
        dst = await tc.transcode(h264_mp4, tmp_path / "mute.mp4", drop_audio=True)
        info = await tc.probe(dst)
        assert not info.has_audio

    async def test_bitrate_mode_reduces_size(self, tall_mp4, tmp_path):
        big = await tc.transcode(tall_mp4, tmp_path / "big.mp4", max_height=720, crf=18)
        small = await tc.transcode(
            tall_mp4, tmp_path / "tiny.mp4", max_height=480, video_bitrate=120_000
        )
        assert small.stat().st_size < big.stat().st_size
        info = await tc.probe(small)
        assert info.height is not None and info.height <= 480

    async def test_transcode_failure_raises(self, tmp_path):
        bad = tmp_path / "bad.webm"
        bad.write_bytes(b"nonsense")
        with pytest.raises(TranscodeError):
            await tc.transcode(bad, tmp_path / "out.mp4")


class TestTargetVideoBitrate:
    def test_matches_architecture_formula(self):
        cap = 48 * 1024 * 1024
        duration = 600.0
        expected = int(0.92 * cap * 8 / duration - 128_000)
        assert tc.target_video_bitrate(cap, duration) == expected

    def test_encoded_size_lands_under_cap(self):
        cap = 10_000_000
        duration = 60.0
        bitrate = tc.target_video_bitrate(cap, duration)
        total_bits = (bitrate + 128_000) * duration
        assert total_bits / 8 <= cap

    def test_floor_for_very_long_video(self):
        assert tc.target_video_bitrate(1_000_000, 100_000.0) == 100_000

    def test_zero_duration_returns_floor(self):
        assert tc.target_video_bitrate(1_000_000, 0.0) == 100_000

    def test_shorter_video_gets_more_bitrate(self):
        cap = 48 * 1024 * 1024
        assert tc.target_video_bitrate(cap, 30.0) > tc.target_video_bitrate(cap, 300.0)

    def test_accepts_custom_audio_bitrate(self):
        cap = 10_000_000
        loud = tc.target_video_bitrate(cap, 60.0, 128_000)
        quiet = tc.target_video_bitrate(cap, 60.0, 32_000)
        assert quiet > loud


class TestTargetAudioBitrate:
    def test_generous_budget_keeps_128k(self):
        assert tc.target_audio_bitrate(48 * 1024 * 1024, 60.0) == 128_000

    def test_tight_budget_steps_audio_down(self):
        # 19s under a 400 KB cap cannot afford a 128k track plus any picture.
        assert tc.target_audio_bitrate(400_000, 19.0) < 128_000

    def test_never_below_floor(self):
        assert tc.target_audio_bitrate(1_000, 3600.0) == 32_000

    def test_zero_duration_returns_default(self):
        assert tc.target_audio_bitrate(1_000_000, 0.0) == 128_000

    def test_combined_bitrates_fit_budget_when_feasible(self):
        cap, duration = 5_000_000, 60.0
        audio = tc.target_audio_bitrate(cap, duration)
        video = tc.target_video_bitrate(cap, duration, audio)
        assert (audio + video) * duration / 8 <= cap


class TestConvertImage:
    async def test_webp_to_jpg(self, webp_file, tmp_path):
        dst = await tc.convert_image(webp_file, tmp_path / "out.jpg")
        assert dst.exists() and dst.stat().st_size > 0
        info = await tc.probe(dst)
        assert info.is_image
        assert (info.width, info.height) == (320, 240)

    async def test_failure_raises(self, tmp_path):
        bad = tmp_path / "bad.webp"
        bad.write_bytes(b"nope")
        with pytest.raises(TranscodeError):
            await tc.convert_image(bad, tmp_path / "out.jpg")


class TestGifAndAnimation:
    async def test_gif_to_mp4_is_silent_h264(self, gif_file, tmp_path):
        dst = await tc.gif_to_mp4(gif_file, tmp_path / "anim.mp4")
        info = await tc.probe(dst)
        assert info.video_codec == "h264"
        assert not info.has_audio

    async def test_gif_source_is_animation(self, gif_file):
        info = await tc.probe(gif_file)
        assert tc.is_animation(info, ".gif")

    async def test_silent_short_video_is_animation(self, silent_mp4):
        info = await tc.probe(silent_mp4)
        assert tc.is_animation(info, ".mp4")

    async def test_video_with_audio_is_not_animation(self, h264_mp4):
        info = await tc.probe(h264_mp4)
        assert not tc.is_animation(info, ".mp4")

    async def test_image_is_not_animation(self, jpg_file):
        info = await tc.probe(jpg_file)
        assert not tc.is_animation(info, ".jpg")

    def test_long_silent_video_is_not_animation(self):
        info = tc.MediaInfo(
            path=__import__("pathlib").Path("x.mp4"),
            container="mp4",
            video_codec="h264",
            audio_codec=None,
            width=320,
            height=240,
            duration_s=600.0,
            has_video=True,
            has_audio=False,
            is_image=False,
        )
        assert not tc.is_animation(info, ".mp4")
