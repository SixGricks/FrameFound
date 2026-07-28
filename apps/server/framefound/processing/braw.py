"""Blackmagic RAW decode via the community braw-decode CLI + Blackmagic SDK.

Licensing boundary (docs/licensing.md): the Blackmagic RAW SDK is EULA-bound
and must NEVER ship in this repository or in published images. The decoder is
built locally on the host (infrastructure/braw/build.sh) and mounted into
workers at /opt/braw via the docker-compose.braw.yml overlay. Everything here
degrades cleanly when the decoder is absent.

braw-decode contract (github.com/AkBKukU/braw-decode):
  braw-decode -c rgba -f FILE     -> prints ffmpeg input args, e.g.
                                     "-f rawvideo -pixel_format rgba -s 3840x2160 -r 60 -i pipe:0"
  braw-decode -c rgba -i 0 -o 1 FILE  -> rawvideo frames on stdout
"""

import contextlib
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import structlog

from framefound.config import get_settings
from framefound.processing.ffmpeg import FfmpegError

log = structlog.get_logger()

BRAW_PROBE_TIMEOUT_S = 60
BRAW_POSTER_TIMEOUT_S = 600  # single-frame decode is slow on CPU-only hosts


def braw_decoder() -> Path | None:
    decoder = get_settings().braw_decoder
    return decoder if decoder and decoder.is_file() else None


def _run_decoder(argv: list[str], timeout_s: int) -> bytes:
    decoder = braw_decoder()
    if decoder is None:
        raise FfmpegError("BRAW decoder is not installed on this server")
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(decoder.parent / "Libraries")
    completed = subprocess.run(  # noqa: S603 - fixed local binary, argv form
        [str(decoder), *argv],
        capture_output=True,
        timeout=timeout_s,
        check=False,
        cwd=decoder.parent,  # braw-decode resolves ./Libraries from cwd
        env=env,
    )
    if completed.returncode != 0:
        tail = completed.stderr.decode("utf-8", errors="replace")[-300:]
        log.warning("braw.decoder_failed", stderr_tail=tail)
        raise FfmpegError("The BRAW file could not be decoded")
    return completed.stdout


def parse_stream_args(printed: str) -> list[str]:
    """The -f output is a ready-made ffmpeg input argument string."""
    args = shlex.split(printed.strip())
    if "-i" not in args or "pipe:0" not in args:
        raise FfmpegError("Unexpected BRAW stream description")
    return args


def parse_dimensions(printed: str) -> dict[str, Any]:
    """Extract width/height/fps from the printed '-s WxH -r FPS' tokens."""
    fields: dict[str, Any] = {}
    tokens = shlex.split(printed.strip())
    for flag, value in zip(tokens, tokens[1:], strict=False):
        if flag == "-s" and "x" in value:
            w, _, h = value.partition("x")
            if w.isdigit() and h.isdigit():
                fields["width"], fields["height"] = int(w), int(h)
        elif flag == "-r":
            with contextlib.suppress(ValueError):
                fields["fps"] = round(float(value), 3)
    if fields:
        fields["video_codec"] = "braw"
    return fields


def probe_braw(src: Path) -> dict[str, Any]:
    if braw_decoder() is None:
        return {}
    try:
        printed = _run_decoder(["-c", "rgba", "-f", str(src)], BRAW_PROBE_TIMEOUT_S).decode(
            "utf-8", errors="replace"
        )
        return parse_dimensions(printed)
    except (FfmpegError, subprocess.TimeoutExpired, OSError):
        return {}


def extract_poster_braw(src: Path, dst: Path, max_width: int = 1920) -> None:
    """Decode frame 0 through braw-decode and encode a JPEG poster."""
    decoder = braw_decoder()
    if decoder is None:
        raise FfmpegError("BRAW decoder is not installed on this server")
    printed = _run_decoder(["-c", "rgba", "-f", str(src)], BRAW_PROBE_TIMEOUT_S).decode(
        "utf-8", errors="replace"
    )
    stream_args = parse_stream_args(printed)

    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(decoder.parent / "Libraries")
    producer = subprocess.Popen(  # noqa: S603
        [str(decoder), "-c", "rgba", "-i", "0", "-o", "1", str(src)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=decoder.parent,
        env=env,
    )
    try:
        consumer = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffmpeg",
                "-y",
                "-v",
                "error",
                *stream_args,
                "-frames:v",
                "1",
                "-vf",
                f"scale='min({max_width},iw)':-2",
                "-c:v",
                "mjpeg",
                "-pix_fmt",
                "yuvj420p",
                "-qscale:v",
                "3",
                str(dst),
            ],
            stdin=producer.stdout,
            capture_output=True,
            timeout=BRAW_POSTER_TIMEOUT_S,
            check=False,
        )
    finally:
        if producer.stdout is not None:
            producer.stdout.close()
        producer.terminate()
        producer.wait(timeout=30)
    if consumer.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        tail = consumer.stderr.decode("utf-8", errors="replace")[-300:]
        log.warning("braw.poster_failed", stderr_tail=tail)
        raise FfmpegError("A poster could not be generated from the BRAW file")
