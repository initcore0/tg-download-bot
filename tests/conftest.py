"""Shared fixtures: tiny real media files generated with ffmpeg lavfi sources.

Generating fixtures (rather than committing binaries) keeps the repo small and exercises
the same ffmpeg the production code calls. Fixtures are session-scoped since encoding a
2-second clip still costs a few hundred milliseconds.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
HAVE_FFMPEG = Path(FFMPEG).exists()

requires_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"fixture generation failed: {result.stderr.decode()[:500]}")


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("media-fixtures")


@pytest.fixture(scope="session")
def h264_mp4(media_dir: Path) -> Path:
    """2s 320x240 H.264 + AAC MP4 — the happy path (pass-through)."""
    out = media_dir / "h264_aac.mp4"
    if not out.exists():
        _run_ffmpeg(
            [
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "64k", "-shortest",
                str(out),
            ]
        )
    return out


@pytest.fixture(scope="session")
def h264_mkv(media_dir: Path) -> Path:
    """2s H.264 + AAC in a Matroska container — should remux, never re-encode."""
    out = media_dir / "h264_aac.mkv"
    if not out.exists():
        _run_ffmpeg(
            [
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "64k", "-shortest",
                str(out),
            ]
        )
    return out


@pytest.fixture(scope="session")
def vp9_webm(media_dir: Path) -> Path:
    """2s VP9 + Opus WebM — must be transcoded."""
    out = media_dir / "vp9_opus.webm"
    if not out.exists():
        _run_ffmpeg(
            [
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libvpx-vp9", "-b:v", "200k", "-deadline", "realtime", "-cpu-used", "8",
                "-c:a", "libopus", "-b:a", "48k", "-shortest",
                str(out),
            ]
        )
    return out


@pytest.fixture(scope="session")
def silent_mp4(media_dir: Path) -> Path:
    """2s H.264 MP4 with no audio track — animation-shaped."""
    out = media_dir / "silent.mp4"
    if not out.exists():
        _run_ffmpeg(
            [
                "-f", "lavfi", "-i", "testsrc=size=240x160:rate=15:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an",
                str(out),
            ]
        )
    return out


@pytest.fixture(scope="session")
def tall_mp4(media_dir: Path) -> Path:
    """2s 1080p H.264 MP4 — exceeds the 720p cap, must be downscaled."""
    out = media_dir / "tall_1080.mp4"
    if not out.exists():
        _run_ffmpeg(
            [
                "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=10:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an",
                str(out),
            ]
        )
    return out


@pytest.fixture(scope="session")
def odd_size_mp4(media_dir: Path) -> Path:
    """H.264 MP4 whose aspect ratio produces a fractional width when downscaled.

    642x482 scaled to height 360 gives width 479.5 — the scale filter must round both
    dimensions to even numbers or libx264 refuses the encode.
    """
    out = media_dir / "odd.mp4"
    if not out.exists():
        _run_ffmpeg(
            [
                "-f", "lavfi", "-i", "testsrc=size=642x482:rate=10:duration=1",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an",
                str(out),
            ]
        )
    return out


@pytest.fixture(scope="session")
def gif_file(media_dir: Path) -> Path:
    """1s animated GIF."""
    out = media_dir / "anim.gif"
    if not out.exists():
        _run_ffmpeg(
            [
                "-f", "lavfi", "-i", "testsrc=size=120x90:rate=10:duration=1",
                str(out),
            ]
        )
    return out


@pytest.fixture(scope="session")
def jpg_file(media_dir: Path) -> Path:
    out = media_dir / "still.jpg"
    if not out.exists():
        _run_ffmpeg(
            ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=1:duration=1", "-frames:v", "1", str(out)]
        )
    return out


@pytest.fixture(scope="session")
def webp_file(media_dir: Path) -> Path:
    out = media_dir / "still.webp"
    if not out.exists():
        _run_ffmpeg(
            ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=1:duration=1", "-frames:v", "1", str(out)]
        )
    return out
