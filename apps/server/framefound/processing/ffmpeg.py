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

log = structlog.get_logger()

POSTER_TIMEOUT_S = 120
WAVEFORM_TIMEOUT_S = 300
PROXY_TIMEOUT_S = 4 * 3600  # long-form sermons/auctions at CPU speed


class FfmpegError(RuntimeError):
    pass


def _run(argv: list[str], timeout_s: int) -> None:
    if shutil.which("ffmpeg") is None:
        raise FfmpegError("ffmpeg is not installed")
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
    if shutil.which("ffmpeg") is None:
        return False
    try:
        completed = subprocess.run(  # noqa: S603
            ["ffmpeg", "-hide_banner", "-encoders"],  # noqa: S607
            capture_output=True,
            timeout=20,
            check=False,
        )
        return b"h264_nvenc" in completed.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def extract_poster(src: Path, dst: Path, at_seconds: float, max_width: int = 1920) -> None:
    """Grab one representative frame as a WebP poster."""
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
            "-qscale:v",
            "80",
            str(dst),
        ],
        POSTER_TIMEOUT_S,
    )


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
