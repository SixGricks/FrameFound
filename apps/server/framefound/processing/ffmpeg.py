"""FFmpeg invocations for derivative generation.

Safety rules as in probe.py: argv arrays only, hard timeouts, no shell.
NVENC (and VideoToolbox, on a macOS worker) is auto-detected once per
process and falls back to x264 silently — hardware is an accelerator, never
a requirement.
"""

import functools
import shutil
import subprocess
import sys
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
    """Cap threads for both the decoder and the encoder.

    FFmpeg defaults to every core, which starves the API and the database on a
    shared host. FRAMEFOUND_FFMPEG_THREADS=0 restores the default for machines
    with cores to spare.

    `-threads` is a *per-file* option, not a global one: before `-i` it
    configures the decoder, before the output path it configures the encoder.
    This originally set only the first, so x264 kept running a thread per core
    — measured on the reference deployment as 810 MB peak for one 1080p
    slideshow slide against 470 MB with the encoder actually capped, because
    each encoder thread carries its own lookahead queue of decoded frames. That
    is most of the distance to the worker's memory limit, paid on every proxy
    transcode, with the setting appearing to be in force.

    Both positions are set, so the name means what it says.
    """
    threads = get_settings().ffmpeg_threads
    if threads <= 0 or "-threads" in argv or len(argv) < 2:
        return argv
    # Last element is the output path by convention across every caller here;
    # output options belong before it.
    return [argv[0], "-threads", str(threads), *argv[1:-1], "-threads", str(threads), argv[-1]]


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
        log.warning(
            "ffmpeg.failed", argv0=argv[:6], returncode=completed.returncode, stderr_tail=tail
        )
        # A negative return code means a signal. Empty stderr alongside it is
        # the OOM killer: ffmpeg never got to say anything. Reporting that as
        # "could not be processed" sends the operator hunting for a corrupt
        # file when the answer is that the frame did not fit in the container.
        if completed.returncode < 0 and not tail.strip():
            raise FfmpegError(
                "Ran out of memory. This file is too large to process within "
                "the worker's memory limit."
            )
        damage = damage_verdict(tail)
        if damage:
            raise FfmpegError(damage)
        raise FfmpegError("The file could not be processed")


# Signatures that are a verdict about the *file*, not about our processing of
# it. Found in deployment: 21 assets in one library were interrupted recordings
# — a drone battery dying mid-flight, a card pulled before the container was
# finalised — and reporting them as "could not be processed" told the operator
# nothing they could act on. These files will not open in Premiere either, and
# some are recoverable with a repair tool, so saying so plainly matters.
DAMAGE_SIGNATURES = (
    ("moov atom not found", "unfinished"),
    ("Invalid data found when processing input", "unreadable"),
    ("could not find codec parameters", "unreadable"),
    ("Format not recognised", "unreadable"),
)


def damage_verdict(stderr_tail: str) -> str | None:
    """A plain-language verdict when ffmpeg says the file itself is broken."""
    lowered = stderr_tail.lower()
    for signature, kind in DAMAGE_SIGNATURES:
        if signature.lower() in lowered:
            if kind == "unfinished":
                return (
                    "This file is damaged: its index is missing, which usually "
                    "means the recording was interrupted before the camera "
                    "finished writing it. It will not open in an editor either. "
                    "Repair tools can sometimes recover the footage."
                )
            return (
                "This file is damaged or in a format ffmpeg cannot read. It is "
                "unlikely to open in an editor either."
            )
    return None


def _encoder_works(codec: str) -> bool:
    """Functionally probe a hardware encoder with a tiny test encode.

    Checking `-encoders` output is NOT sufficient: builds list h264_nvenc
    whenever it was compiled in, even with no NVIDIA GPU present, and then
    fail at open time (found in deployment). VideoToolbox is the same shape:
    listed by every macOS build, usable only where the media engine is."""
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
                codec,
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


@functools.cache
def nvenc_available() -> bool:
    """NVIDIA's encoder, on the server with a GPU passed through."""
    return _encoder_works("h264_nvenc")


@functools.cache
def videotoolbox_available() -> bool:
    """Apple's media engine, on the native Mac install (docs/mac.md)."""
    return sys.platform == "darwin" and _encoder_works("h264_videotoolbox")


def videotoolbox_proxy_args(height: int) -> list[str] | None:
    """FFmpeg video-codec arguments for a proxy made on Apple's media engine.

    Return the argv slice that replaces libx264's
    `["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]`, or None to
    keep using libx264 on this machine.

    The trade-off: h264_videotoolbox has no CRF. It takes either a
    constant quality (`-q:v 1..100`, higher is better; Apple silicon only)
    or a target bitrate (`-b:v`). Constant quality tracks the footage —
    a static interior gets a small file, a drone pass over trees a big one —
    which is what CRF 23 did. A bitrate is predictable on disk but either
    starves busy shots (blocky foliage, which the motion analysis reads as
    texture) or wastes space on still ones. `height` is the proxy's
    target height (1080 by default) if you want the answer to scale.

    Proxies feed the browser player, contact sheets and the shot motion
    pass; Premiere's own proxies are separate files and unaffected.
    """
    # TODO(operator): choose the media-engine settings for proxies.
    return None


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


def extract_still(src: Path, dst: Path, at_seconds: float) -> None:
    """One frame at full size and full depth, for an editor: a 16-bit TIFF.

    GELCO's drone clips are 10-bit HEVC. A JPEG grab keeps 256 levels a
    channel of their 1,024 (and a first try came out at quality ~75), so a
    sky pushed in Lightroom bands; a 16-bit TIFF keeps them all. Deflate
    compression brings a 4K frame to a few tens of megabytes."""
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
            "-pix_fmt",
            "rgb48le",
            "-compression_algo",
            "deflate",
            str(dst),
        ],
        POSTER_TIMEOUT_S,
    )
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
    apple = videotoolbox_proxy_args(height) if videotoolbox_available() else None
    if nvenc_available():
        codec_args = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26"]
        codec = "h264_nvenc"
    elif apple is not None:
        codec_args = apple
        codec = "h264_videotoolbox"
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


# Speech audio is decoded at the rate Whisper works at; mixing to mono there
# is a few megabytes a minute of WAV rather than the source's full PCM.
SPEECH_SAMPLE_RATE = 16000
SPEECH_TIMEOUT_S = 2 * 3600


def audio_stream_count(src: Path) -> int:
    """How many audio streams the container holds (0 when ffprobe cannot say)."""
    from framefound.processing.probe import _run_json_tool

    if shutil.which("ffprobe") is None:
        return 0
    data = _run_json_tool(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-print_format",
            "json",
            str(src),
        ]
    )
    if not isinstance(data, dict):
        return 0
    return len(data.get("streams") or [])


def speech_mix_args(src: Path, dst: Path, streams: int) -> list[str]:
    """ffmpeg argv: every audio stream of `src` mixed to 16 kHz mono WAV.

    Whisper's own decoder hears audio stream 0 only. Cinema and broadcast
    cameras record the on-camera mic there and the lavalier or the board
    feed on streams 2 and up, so the person actually speaking was the part
    the transcript left out.
    """
    argv = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn"]
    if streams > 1:
        inputs = "".join(f"[0:a:{i}]" for i in range(streams))
        # normalize=0: the default divides each input by the count, which
        # turns a quiet lav on one of four streams into a whisper. Summing
        # at unity has the opposite failure: cameras often carry one mic on
        # CH1 and a safety copy on CH2, so a hot PA feed of the auctioneer
        # added to itself goes past full scale and hard-clips in the s16 WAV
        # Whisper hears. The limiter, still in float, catches only those
        # peaks; level=0 stops it adding make-up gain to everything else.
        mix = f"amix=inputs={streams}:normalize=0,alimiter=limit=0.95:level=0"
        argv += ["-filter_complex", f"{inputs}{mix}[speech]"]
        argv += ["-map", "[speech]"]
    else:
        argv += ["-map", "0:a:0"]
    argv += ["-ac", "1", "-ar", str(SPEECH_SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)]
    return argv


def extract_speech_audio(src: Path, dst: Path, streams: int) -> None:
    """Write the mixed speech track for transcription (see speech_mix_args)."""
    _run(speech_mix_args(src, dst, streams), SPEECH_TIMEOUT_S)
