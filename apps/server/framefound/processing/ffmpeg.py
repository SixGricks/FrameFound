"""FFmpeg invocations for derivative generation.

Safety rules as in probe.py: argv arrays only, hard timeouts, no shell.
NVENC is auto-detected once per process and falls back to x264 silently —
the GPU is an accelerator, never a requirement.
"""

import functools
import shutil
import subprocess
from pathlib import Path

import structlog

from framefound.config import get_settings

log = structlog.get_logger()

POSTER_TIMEOUT_S = 120
WAVEFORM_TIMEOUT_S = 300
PROXY_TIMEOUT_S = 4 * 3600  # long-form sermons/auctions at CPU speed

# Stills are read end to end, and library storage is usually a network
# share. Deliberately pessimistic: a floor of 2 MB/s leaves headroom under
# the 5.2 MB/s measured on the reference SMB mount, so a slow evening on
# the network does not turn into a failed thumbnail.
SLOW_STORAGE_BPS = 2 * 1024 * 1024
MAX_STILL_TIMEOUT_S = 30 * 60


class FfmpegError(RuntimeError):
    pass


def _with_thread_cap(argv: list[str]) -> list[str]:
    """Insert -threads after the binary unless the caller already set it.

    FFmpeg defaults to every core, which starves the API and the database on a
    shared host. FRAMEFOUND_FFMPEG_THREADS=0 restores the default for machines
    with cores to spare.
    """
    threads = get_settings().ffmpeg_threads
    if threads <= 0 or "-threads" in argv:
        return argv
    return [argv[0], "-threads", str(threads), *argv[1:]]


def _run(argv: list[str], timeout_s: int) -> None:
    if shutil.which("ffmpeg") is None:
        raise FfmpegError("ffmpeg is not installed")
    argv = _with_thread_cap(argv)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, argv form, no shell
            argv, capture_output=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as err:
        raise FfmpegError("Processing took too long and was stopped") from err
    if completed.returncode != 0:
        tail = completed.stderr.decode("utf-8", errors="replace")[-400:]
        log.warning("ffmpeg.failed", argv0=argv[:6], stderr_tail=tail)
        raise FfmpegError("The file could not be processed")


@functools.cache
def nvenc_available() -> bool:
    """Functionally probe NVENC with a tiny test encode.

    Checking `-encoders` output is NOT sufficient: builds list h264_nvenc
    whenever it was compiled in, even with no NVIDIA GPU present, and then
    fail at open time (found in deployment)."""
    if shutil.which("ffmpeg") is None:
        return False
    try:
        completed = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "nullsrc=s=256x256:d=0.1",
                "-c:v",
                "h264_nvenc",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def extract_poster(src: Path, dst: Path, at_seconds: float, max_width: int = 1920) -> None:
    """Grab one representative frame as a JPEG poster.

    JPEG via the built-in mjpeg encoder: ffmpeg routes .webp through
    libwebp_anim, which fails deterministically on some high-resolution
    sources (found in deployment)."""
    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{max(0.0, at_seconds):.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({max_width},iw)':-2",
            "-c:v",
            "mjpeg",
            # 10-bit/4:2:2 sources (ProRes) need explicit conversion to the
            # JPEG-native full-range 4:2:0 format (found in deployment).
            "-pix_fmt",
            "yuvj420p",
            "-qscale:v",
            "3",
            str(dst),
        ],
        POSTER_TIMEOUT_S,
    )
    # A seek past EOF yields zero frames but exit code 0 (image2 muxer writes
    # nothing). Success means a file exists.
    if not dst.is_file() or dst.stat().st_size == 0:
        raise FfmpegError("No frame could be extracted from the video")


def downscale_still(src: Path, dst: Path, max_edge: int) -> None:
    """Scale a still image down during decode, writing a JPEG.

    For images Pillow cannot hold in memory — multi-gigabyte 16-bit TIFF
    exports, mainly. FFmpeg scales as it decodes rather than materialising the
    full raster and then copying it.

    JPEG rather than WebP for the same reason as `extract_poster`: ffmpeg
    routes .webp through libwebp_anim, which fails on some high-resolution
    sources. The caller re-encodes this much smaller intermediate.

    The timeout scales with file size. A video poster seeks and reads a few
    megabytes, so a flat two minutes is generous; a still has to be read end
    to end, and library storage is a network share. Measured throughput on the
    reference deployment was 5.2 MB/s over SMB, which puts a 1 GB TIFF at
    three minutes of reading before decoding starts.
    """
    try:
        size_bytes = src.stat().st_size
    except OSError:
        size_bytes = 0
    timeout_s = min(MAX_STILL_TIMEOUT_S, POSTER_TIMEOUT_S + int(size_bytes / SLOW_STORAGE_BPS))

    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            f"scale={max_edge}:{max_edge}:force_original_aspect_ratio=decrease",
            "-c:v",
            "mjpeg",
            "-pix_fmt",
            "yuvj420p",
            "-qscale:v",
            "2",
            str(dst),
        ],
        timeout_s,
    )
    if not dst.is_file() or dst.stat().st_size == 0:
        raise FfmpegError("The image could not be decoded")


def transcode_proxy(src: Path, dst: Path, height: int = 1080) -> str:
    """1080p (default) H.264+AAC faststart MP4 proxy. Returns the codec used."""
    scale = f"scale=-2:'min({height},ih)'"
    if nvenc_available():
        codec_args = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26"]
        codec = "h264_nvenc"
    else:
        codec_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        codec = "libx264"
    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src),
            "-vf",
            scale,
            *codec_args,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(dst),
        ],
        PROXY_TIMEOUT_S,
    )
    return codec


def render_waveform(src: Path, dst: Path, width: int = 1200, height: int = 160) -> None:
    """Waveform overview image for audio assets."""
    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src),
            "-filter_complex",
            f"aformat=channel_layouts=mono,showwavespic=s={width}x{height}:colors=#4a9eff",
            "-frames:v",
            "1",
            str(dst),
        ],
        WAVEFORM_TIMEOUT_S,
    )
