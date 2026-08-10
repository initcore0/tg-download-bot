"""ffprobe/ffmpeg helpers: inspect media, remux when possible, transcode when required.

All subprocess work is async (`asyncio.create_subprocess_exec`) so the event loop stays free.
Latency policy (ARCHITECTURE.md §5.2): pass through > remux (`-c copy`) > transcode.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from tgdl.downloader.models import TranscodeError

log = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

# Containers Telegram plays back reliably without a rewrite.
_OK_CONTAINERS = {"mp4", "mov", "m4a", "3gp", "mj2"}
_OK_VIDEO_CODECS = {"h264", "avc1"}
_OK_AUDIO_CODECS = {"aac", "mp4a"}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp"}
_AUDIO_BITRATE_BPS = 128_000
_MIN_AUDIO_BITRATE_BPS = 32_000
_MIN_VIDEO_BITRATE_BPS = 100_000

# A short, silent, looping clip is an "animation" (Telegram sends it as a GIF-like MP4).
ANIMATION_MAX_DURATION_S = 30.0


@dataclass(slots=True)
class MediaInfo:
    """Facts about a media file as reported by ffprobe."""

    path: Path
    container: str  # normalized format short name, e.g. "mp4", "webm"
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    duration_s: float | None
    has_video: bool
    has_audio: bool
    is_image: bool

    @property
    def needs_transcode(self) -> bool:
        """True when codecs are not Telegram-friendly and a re-encode is unavoidable."""
        if self.is_image:
            return False
        if self.video_codec not in _OK_VIDEO_CODECS:
            return True
        return self.has_audio and self.audio_codec not in _OK_AUDIO_CODECS

    @property
    def needs_remux(self) -> bool:
        """True when codecs are fine but the container must be rewritten to MP4."""
        if self.is_image or self.needs_transcode:
            return False
        return self.container not in _OK_CONTAINERS


class Decision:
    """Outcome of the compat check."""

    PASSTHROUGH = "passthrough"
    REMUX = "remux"
    TRANSCODE = "transcode"


def decide(info: MediaInfo) -> str:
    """Choose the cheapest processing step that yields Telegram-ready media."""
    if info.needs_transcode:
        return Decision.TRANSCODE
    if info.needs_remux:
        return Decision.REMUX
    return Decision.PASSTHROUGH


async def _run(args: list[str], *, what: str) -> tuple[int, bytes, bytes]:
    """Run a subprocess, returning (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:  # ffmpeg/ffprobe missing
        raise TranscodeError(f"{what}: executable not found ({exc})") from exc
    except OSError as exc:
        raise TranscodeError(f"{what}: failed to start ({exc})") from exc
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


def _normalize_container(format_name: str, path: Path) -> str:
    """ffprobe reports comma-joined format lists; pick a stable single name."""
    names = [n.strip() for n in (format_name or "").split(",") if n.strip()]
    if not names:
        return path.suffix.lstrip(".").lower()
    # mp4 family reports "mov,mp4,m4a,3gp,3g2,mj2" — prefer the actual extension if it is in there.
    ext = path.suffix.lstrip(".").lower()
    if ext in names:
        return ext
    if "mp4" in names:
        return "mp4"
    return names[0]


def _to_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


async def probe(path: Path) -> MediaInfo:
    """ffprobe `path` and return normalized metadata. Raises TranscodeError on failure."""
    args = [
        FFPROBE,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    code, stdout, stderr = await _run(args, what="ffprobe")
    if code != 0:
        raise TranscodeError(f"ffprobe failed for {path.name}: {stderr.decode(errors='replace')[:500]}")
    try:
        data = json.loads(stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise TranscodeError(f"ffprobe returned invalid JSON for {path.name}: {exc}") from exc

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    container = _normalize_container(fmt.get("format_name", ""), path)

    if video is None and audio is None:
        raise TranscodeError(f"{path.name} contains no audio or video streams")
    # ffprobe tolerates truncated/corrupt images, reporting 0x0 instead of failing.
    if video is not None and (video.get("width") in (0, None)) and audio is None:
        raise TranscodeError(f"{path.name} has an unreadable video stream (0x0)")

    duration = _to_float(fmt.get("duration"))
    if duration is None and video is not None:
        duration = _to_float(video.get("duration"))

    # Still images decode as a single-frame video stream in an image container.
    image_containers = {"image2", "png_pipe", "webp_pipe", "jpeg_pipe", "mjpeg", "bmp_pipe"}
    is_image = bool(
        video is not None
        and audio is None
        and (
            container in image_containers
            or (path.suffix.lower() in IMAGE_EXTENSIONS and path.suffix.lower() != ".gif")
        )
        and (video.get("nb_frames") in (None, "1", 1) or duration is None)
    )

    return MediaInfo(
        path=path,
        container=container,
        video_codec=(video or {}).get("codec_name"),
        audio_codec=(audio or {}).get("codec_name"),
        width=(video or {}).get("width"),
        height=(video or {}).get("height"),
        duration_s=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        is_image=is_image,
    )


def _scale_filter(max_height: int) -> str:
    """Downscale to at most `max_height`, keeping aspect ratio and even dimensions."""
    return (
        f"scale=-2:'min({max_height},ih)':force_original_aspect_ratio=decrease,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )


def _output_path(src: Path, suffix: str, ext: str = ".mp4") -> Path:
    """Sibling path with a distinguishing suffix, never colliding with the source."""
    candidate = src.with_name(f"{src.stem}{suffix}{ext}")
    counter = 1
    while candidate == src or candidate.exists():
        candidate = src.with_name(f"{src.stem}{suffix}{counter}{ext}")
        counter += 1
    return candidate


async def remux(src: Path, dst: Path | None = None) -> Path:
    """Stream-copy `src` into a faststart MP4. Cheapest fix for a wrong container."""
    dst = dst or _output_path(src, "_remux")
    args = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    code, _, stderr = await _run(args, what="ffmpeg remux")
    if code != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise TranscodeError(f"remux failed for {src.name}: {stderr.decode(errors='replace')[:500]}")
    return dst


async def faststart(src: Path, dst: Path | None = None) -> Path:
    """Rewrite an MP4 with the moov atom up front, without re-encoding."""
    return await remux(src, dst)


async def transcode(
    src: Path,
    dst: Path | None = None,
    *,
    max_height: int = 720,
    crf: int = 26,
    preset: str = "veryfast",
    video_bitrate: int | None = None,
    audio_bitrate: int = _AUDIO_BITRATE_BPS,
    drop_audio: bool = False,
) -> Path:
    """Re-encode to H.264/AAC MP4 at ≤`max_height`.

    When `video_bitrate` (bps) is given, encode in constrained-bitrate mode instead of
    CRF — used by the size-cap retry to hit a specific target size.
    """
    dst = dst or _output_path(src, "_x264")
    args: list[str] = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        _scale_filter(max_height),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
    ]
    if video_bitrate is not None:
        bitrate = max(int(video_bitrate), 100_000)
        args += [
            "-b:v",
            str(bitrate),
            "-maxrate",
            str(int(bitrate * 1.2)),
            "-bufsize",
            str(int(bitrate * 2)),
        ]
    else:
        args += ["-crf", str(crf), "-maxrate", "2500k", "-bufsize", "5000k"]

    if drop_audio:
        args += ["-an"]
    else:
        args += ["-c:a", "aac", "-b:a", f"{audio_bitrate // 1000}k", "-ac", "2"]

    args += ["-movflags", "+faststart", str(dst)]

    code, _, stderr = await _run(args, what="ffmpeg transcode")
    if code != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise TranscodeError(
            f"transcode failed for {src.name}: {stderr.decode(errors='replace')[:500]}"
        )
    return dst


def target_video_bitrate(
    max_size_bytes: int, duration_s: float, audio_bitrate: int = _AUDIO_BITRATE_BPS
) -> int:
    """Video bitrate (bps) that lands a `duration_s` clip just under the cap.

    `0.92 * cap * 8 / duration - audio_bitrate` (ARCHITECTURE.md §5.3); the 0.92 factor
    leaves headroom for container overhead and rate-control overshoot.
    """
    if duration_s <= 0:
        return _MIN_VIDEO_BITRATE_BPS
    budget = 0.92 * max_size_bytes * 8 / duration_s - audio_bitrate
    return max(int(budget), _MIN_VIDEO_BITRATE_BPS)


def target_audio_bitrate(max_size_bytes: int, duration_s: float) -> int:
    """Audio bitrate (bps) to pair with the video target.

    Normally 128k, but for a very tight budget (short cap, long clip) a fixed 128k track
    would consume the whole allowance on its own; step down so video still gets a share.
    """
    if duration_s <= 0:
        return _AUDIO_BITRATE_BPS
    total_bps = 0.92 * max_size_bytes * 8 / duration_s
    for candidate in (_AUDIO_BITRATE_BPS, 96_000, 64_000, 48_000, 32_000):
        # Leave at least the video floor for the picture.
        if total_bps - candidate >= _MIN_VIDEO_BITRATE_BPS:
            return candidate
    return _MIN_AUDIO_BITRATE_BPS


async def convert_image(src: Path, dst: Path | None = None) -> Path:
    """Convert an image (e.g. webp) to JPEG for maximum Telegram compatibility."""
    dst = dst or _output_path(src, "_conv", ext=".jpg")
    args = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(dst),
    ]
    code, _, stderr = await _run(args, what="ffmpeg image convert")
    if code != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise TranscodeError(
            f"image conversion failed for {src.name}: {stderr.decode(errors='replace')[:500]}"
        )
    return dst


async def gif_to_mp4(src: Path, dst: Path | None = None, *, max_height: int = 720) -> Path:
    """Convert a GIF to a silent H.264 MP4 (Telegram's animation format)."""
    return await transcode(src, dst, max_height=max_height, drop_audio=True)


def is_animation(info: MediaInfo, source_ext: str | None = None) -> bool:
    """A silent, short clip — or anything that came from a .gif — is an animation."""
    if info.is_image or not info.has_video:
        return False
    if (source_ext or "").lower() == ".gif" or info.container == "gif":
        return True
    if info.has_audio:
        return False
    duration = info.duration_s
    return duration is not None and duration <= ANIMATION_MAX_DURATION_S
