"""Scene detection and frame sampling.

Two signals combine (brief §4.7): FFmpeg's scene-change score catches cuts,
and a duration-aware interval guarantees coverage across long unbroken takes
(a 40-minute locked-off auction camera has almost no cuts but plenty of
content). Sampling is capped so one pathological file cannot flood the table.
"""

import re
import subprocess
from pathlib import Path

import structlog

from framefound.processing.ffmpeg import FfmpegError, _run

log = structlog.get_logger()

SCENE_THRESHOLD = 0.4
DETECT_TIMEOUT_S = 1800
MAX_FRAMES_PER_ASSET = 240

_SHOWINFO_TS = re.compile(rb"pts_time:([0-9.]+)")


def interval_for(duration_s: float) -> float:
    """Duration-aware cadence: dense for clips, sparse for long-form."""
    if duration_s <= 60:
        return 5.0
    if duration_s <= 600:
        return 10.0
    if duration_s <= 3600:
        return 30.0
    return 60.0


def detect_scene_timestamps(src: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Timestamps (seconds) where FFmpeg reports a scene change."""
    try:
        completed = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffmpeg",
                "-v",
                "info",
                "-i",
                str(src),
                "-filter:v",
                f"select='gt(scene,{threshold})',showinfo",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=DETECT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("scenes.detect_failed", file=src.name)
        return []
    return [float(m.group(1)) for m in _SHOWINFO_TS.finditer(completed.stderr)]


def plan_samples(
    duration_s: float, scene_times: list[float], max_frames: int = MAX_FRAMES_PER_ASSET
) -> list[tuple[float, bool]]:
    """Merge scene changes with interval ticks into an ordered sample plan.

    Returns (timestamp, is_scene_change) pairs. Scene changes win when they
    collide with an interval tick, and ticks nearer than half the interval to
    a detected cut are dropped so the same shot is not sampled twice.
    """
    if duration_s <= 0:
        return [(0.0, False)]
    step = interval_for(duration_s)
    planned: dict[int, tuple[float, bool]] = {}

    for ts in scene_times:
        if 0 <= ts < duration_s:
            planned[int(ts * 4)] = (round(ts, 3), True)  # quarter-second buckets

    guard = step / 2
    tick = 0.0
    while tick < duration_s:
        if not any(abs(tick - ts) < guard for ts, _ in planned.values()):
            planned.setdefault(int(tick * 4), (round(tick, 3), False))
        tick += step

    ordered = sorted(planned.values())
    if len(ordered) <= max_frames:
        return ordered
    # Thin evenly rather than truncating, so coverage stays spread across
    # the whole runtime instead of stopping partway through.
    stride = len(ordered) / max_frames
    return [ordered[int(i * stride)] for i in range(max_frames)]


def extract_frame(src: Path, dst: Path, at_seconds: float, max_width: int = 640) -> None:
    """Single JPEG frame at a timestamp (same codec path as posters)."""
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
            "-pix_fmt",
            "yuvj420p",
            "-qscale:v",
            "5",
            str(dst),
        ],
        120,
    )
    if not dst.is_file() or dst.stat().st_size == 0:
        raise FfmpegError("No frame could be extracted at that timestamp")
