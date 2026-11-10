"""Shot intelligence at ingest: decode the proxy once, describe every window.

Two passes, on two queues, because they need different things:

- **Motion** (`frames` queue, after the proxy exists): decodes the 1080p
  proxy at 320 px — never the original — and runs media/motion.py over it.
  Writes the motion track (a derived file, kind "motion"), the windows, a
  still at the start, middle and end of each (for contact sheets), the sun's
  position for each window, and the measured look.
- **Descriptors** (`vision` queue, once the clip's frames are embedded):
  framing and subjects from CLIP, scored against media/shot_vocabulary.py.
  Re-labels a move when framing says the camera looked straight down,
  which motion alone cannot see.

Both rebuild everything they own from scratch, so either can be re-run at
any time (`python -m framefound.ops.shots`).
"""

import asyncio
import json
import math
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.config import get_settings
from framefound.db.models import Asset, Derivative, Frame, Library, ShotWindow
from framefound.media import motion
from framefound.media.sun import light_class, sun_position
from framefound.processing import derivatives as deriv

log = structlog.get_logger()

STILL_WIDTH = 480
# Window stills sit a quarter second inside each end, clear of the cut.
STILL_INSET_S = 0.25
# A slow box decodes a 1080p H.264 proxy at well over real time; four times
# the clip's length plus a margin is a hang, not a slow file.
DECODE_TIMEOUT_FACTOR = 4.0
DECODE_TIMEOUT_MIN_S = 120


class NoProxy(RuntimeError):
    """The clip has no proxy yet; the analysis never reads an original."""


def proxy_path(data_dir: Path, asset_id: uuid.UUID) -> Path:
    return data_dir / deriv.derivative_relpath(asset_id, "proxy", "mp4")


def still_relpath(asset_id: uuid.UUID, ts_ms: int) -> str:
    return f"shots/{str(asset_id)[:2]}/{asset_id}/{ts_ms:09d}.jpeg"


def analysis_size(width: int, height: int) -> tuple[int, int]:
    """The analysis frame: 320 px wide, the proxy's shape, even height."""
    if width <= 0 or height <= 0:
        return motion.ANALYSIS_WIDTH, 180
    w = motion.ANALYSIS_WIDTH if width >= height else 192
    h = max(32, int(round(w * height / width / 2)) * 2)
    return w, h


def decode_yuv(
    src: Path, width: int, height: int, fps: float | None, timeout_s: float
) -> Iterator[motion.YuvFrame]:
    """Frames of `src` as full-resolution YUV planes, scaled down by FFmpeg
    to `width` x `height`, at the source's own rate (capped at 60 fps).
    Planar YUV rather than RGB: the motion step needs only luma, and
    reading it straight from the decoder saves converting every frame."""
    import numpy as np

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not installed")
    filters = []
    if fps and fps > motion.MAX_ANALYSIS_FPS + 0.5:
        filters.append(f"fps={motion.MAX_ANALYSIS_FPS:g}")
    filters.append(f"scale={width}:{height}:flags=area")
    argv = [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-threads",
        str(get_settings().ffmpeg_threads),
        "-i",
        str(src),
        "-an",
        "-vf",
        ",".join(filters),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv444p",
        "-",
    ]
    plane = width * height
    frame_bytes = plane * 3
    started = datetime.now(UTC)
    process = subprocess.Popen(  # noqa: S603 - fixed binary, argv form, no shell
        argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(frame_bytes)
            if len(chunk) < frame_bytes:
                break
            data = np.frombuffer(chunk, dtype=np.uint8)
            yield motion.YuvFrame(
                data[:plane].reshape(height, width),
                data[plane : 2 * plane].reshape(height, width),
                data[2 * plane :].reshape(height, width),
            )
            if (datetime.now(UTC) - started).total_seconds() > timeout_s:
                raise RuntimeError("Decoding the proxy took too long and was stopped")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def analyse_file(
    src: Path,
    fps: float,
    width: int,
    height: int,
    duration_s: float = 0.0,
    extra_cuts_s: list[float] | None = None,
) -> tuple[motion.Analysis, list[motion.Window], float]:
    """Decode and analyse one file: (analysis, windows, seconds taken).
    Pure apart from the decode, so the backfill's validation can run it on
    any file."""
    started = datetime.now(UTC)
    aw, ah = analysis_size(width, height)
    rate = min(fps, motion.MAX_ANALYSIS_FPS) if fps else 30.0
    timeout = max(DECODE_TIMEOUT_MIN_S, (duration_s or 600.0) * DECODE_TIMEOUT_FACTOR)
    analysis = motion.analyse(decode_yuv(src, aw, ah, fps, timeout), rate)
    if analysis.frames < 2:
        raise RuntimeError(f"{src.name}: no frames could be decoded")
    found = motion.windows(analysis, extra_cuts_s or [])
    return analysis, found, (datetime.now(UTC) - started).total_seconds()


# --- where and when ------------------------------------------------------------


def _folders(relative_path: str) -> list[str]:
    """The folders a file sits in, nearest first: a/b/c.mp4 -> a/b, a."""
    parents = [str(p) for p in PurePosixPath(relative_path).parents if str(p) not in ("", ".")]
    return parents


async def location_for(db: AsyncSession, asset: Asset) -> tuple[float, float, str] | None:
    """The clip's position, or its folder's, or its library's.

    Drone video rarely carries GPS in the container FrameFound reads, but
    the photographs of the same course do. The nearest folder with located
    assets in it stands in (the median of their positions), then the
    library's. Labelled with where it came from."""
    if asset.gps_lat is not None and asset.gps_lon is not None:
        return asset.gps_lat, asset.gps_lon, asset.gps_source or "exif"
    for folder in _folders(asset.relative_path):
        prefix = folder.rstrip("/") + "/"
        rows = (
            await db.execute(
                select(Asset.gps_lat, Asset.gps_lon)
                .where(
                    Asset.library_id == asset.library_id,
                    Asset.relative_path.startswith(prefix, autoescape=True),
                    Asset.gps_lat.is_not(None),
                    Asset.gps_lon.is_not(None),
                )
                .limit(500)
            )
        ).all()
        if rows:
            return _median(rows) + ("folder",)
    rows = (
        await db.execute(
            select(Asset.gps_lat, Asset.gps_lon)
            .where(
                Asset.library_id == asset.library_id,
                Asset.gps_lat.is_not(None),
                Asset.gps_lon.is_not(None),
            )
            .limit(2000)
        )
    ).all()
    if rows:
        return _median(rows) + ("library",)
    return None


def _median(rows: Any) -> tuple[float, float]:
    lats = sorted(float(r[0]) for r in rows)
    lons = sorted(float(r[1]) for r in rows)
    return lats[len(lats) // 2], lons[len(lons) // 2]


def light_for(elevation: float, look: dict[str, Any]) -> str:
    """The light class for a window: from the sun's elevation alone.

    The measured look is kept beside it (overcast or direct, warmth) rather
    than folded in. Measured warmth is no sign of golden light: on
    Brookside's autumn course the golden-hour clips (sun at 3-4 degrees)
    measured cooler than the ones at 11, because the leaves set the colour
    at any hour. `look` stays a parameter for when a better signal exists."""
    del look
    return light_class(elevation)


# --- the motion pass -----------------------------------------------------------


async def _scene_cuts(db: AsyncSession, asset_id: uuid.UUID) -> list[float]:
    rows = (
        await db.execute(
            select(Frame.ts_ms).where(Frame.asset_id == asset_id, Frame.is_scene_change.is_(True))
        )
    ).all()
    return [int(r[0]) / 1000 for r in rows]


def _extract_stills(src: Path, data_dir: Path, asset_id: uuid.UUID, moments: list[int]) -> int:
    from framefound.processing import scenes

    made = 0
    for ts_ms in moments:
        target = data_dir / still_relpath(asset_id, ts_ms)
        if target.is_file():
            made += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            scenes.extract_frame(src, target, ts_ms / 1000, max_width=STILL_WIDTH)
            made += 1
        except Exception as exc:  # one bad seek must not lose the analysis
            log.warning("shots.still_skipped", ts_ms=ts_ms, reason=str(exc)[:120])
    return made


def window_moments(start_ms: int, end_ms: int) -> tuple[int, int, int]:
    """Start, middle and end moments for a window's stills."""
    inset = int(STILL_INSET_S * 1000)
    length = end_ms - start_ms
    if length <= 2 * inset:
        mid = (start_ms + end_ms) // 2
        return mid, mid, mid
    return start_ms + inset, (start_ms + end_ms) // 2, end_ms - inset


async def analyse_motion(db: AsyncSession, asset: Asset, library: Library, path: Path) -> int:
    """The motion pass for one clip. Returns how many windows it wrote."""
    from framefound.processing.probe import probe_ffprobe

    if asset.media_type != "video":
        return 0
    data_dir = get_settings().data_dir
    proxy = proxy_path(data_dir, asset.id)
    if not proxy.is_file():
        raise NoProxy(f"{asset.filename} has no proxy yet")
    probed = await asyncio.to_thread(probe_ffprobe, proxy)
    fps = float(probed.get("fps") or asset.fps or 30.0)
    width = int(probed.get("width") or asset.width or 1920)
    height = int(probed.get("height") or asset.height or 1080)
    duration = float(probed.get("duration_s") or asset.duration_s or 0.0)
    cuts = await _scene_cuts(db, asset.id)

    analysis, found, seconds = await asyncio.to_thread(
        analyse_file, proxy, fps, width, height, duration, cuts
    )
    track = motion.track(analysis)
    track["source_fps"] = round(fps, 3)
    track["analysis_seconds"] = round(seconds, 2)

    deriv.ensure_space(data_dir)
    record = await deriv._upsert(db, asset.id, "motion", "json")
    target = deriv._abs(data_dir, record)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(track, separators=(",", ":")), encoding="utf-8")
    await deriv._finish(db, record, target)

    place = await location_for(db, asset)
    await db.execute(delete(ShotWindow).where(ShotWindow.asset_id == asset.id))
    moments: list[int] = []
    for index, window in enumerate(found):
        start_ms, end_ms = int(window.start_s * 1000), int(window.end_s * 1000)
        elevation: float | None = None
        light: str | None = None
        if place is not None and asset.captured_at is not None:
            when = asset.captured_at + timedelta(seconds=(window.start_s + window.end_s) / 2)
            elevation = round(sun_position(when, place[0], place[1])[0], 2)
            light = light_for(elevation, window.look)
        look = dict(window.look)
        if place is not None:
            look["located_by"] = place[2]
        db.add(
            ShotWindow(
                asset_id=asset.id,
                idx=index,
                start_ms=start_ms,
                end_ms=end_ms,
                move=window.move,
                direction=window.direction,
                speed=window.speed,
                smooth=window.smooth,
                smooth_by_speed=window.smooth_by_speed,
                energy=window.energy,
                energy_score=window.energy_score,
                source_fps=round(fps, 3),
                features=window.features,
                sun_elevation=elevation,
                light=light,
                look=look,
                framing={},
                subjects={},
                analysis_version=motion.VERSION,
            )
        )
        moments.extend(window_moments(start_ms, end_ms))
    await db.commit()
    stills = await asyncio.to_thread(
        _extract_stills, proxy, data_dir, asset.id, sorted(set(moments))
    )
    log.info(
        "shots.analysed",
        asset_id=str(asset.id),
        windows=len(found),
        stills=stills,
        seconds=round(seconds, 1),
        realtime=round(duration / seconds, 2) if seconds else None,
    )
    return len(found)


# --- the descriptor pass -------------------------------------------------------

_prompt_cache: dict[tuple[str, str], list[float]] = {}


def _embed_prompts(provider: Any, prompts: list[str]) -> list[list[float]]:
    model = str(getattr(provider, "model_name", "clip"))
    out = []
    for prompt in prompts:
        key = (model, prompt)
        if key not in _prompt_cache:
            _prompt_cache[key] = list(provider.embed_text(prompt).vector)
        out.append(_prompt_cache[key])
    return out


def is_drone(asset: Asset) -> bool:
    make = (asset.camera_make or "").lower()
    model = (asset.camera_model or "").upper()
    return "dji" in make or model.startswith("FC") or asset.filename.upper().startswith("DJI_")


def _mean_vector(vectors: list[list[float]]) -> list[float] | None:
    if not vectors:
        return None
    dims = len(vectors[0])
    total = [0.0] * dims
    for vector in vectors:
        if len(vector) != dims:
            continue
        for i, v in enumerate(vector):
            total[i] += v
    norm = math.sqrt(sum(v * v for v in total)) or 1.0
    return [v / norm for v in total]


async def describe_shots(db: AsyncSession, asset: Asset) -> int:
    """Framing and subjects for every window of one clip, from its
    embedded frames. Returns how many windows it described (0 when the clip
    has no windows or no embeddings yet)."""
    from framefound.ai.embeddings import EmbeddingUnavailable, get_embedding_provider
    from framefound.media import shot_vocabulary as vocab

    windows = (
        (await db.execute(select(ShotWindow).where(ShotWindow.asset_id == asset.id)))
        .scalars()
        .all()
    )
    if not windows:
        return 0
    frames = (
        await db.execute(
            select(Frame.ts_ms, Frame.embedding).where(
                Frame.asset_id == asset.id, Frame.embedding.is_not(None)
            )
        )
    ).all()
    if not frames:
        return 0
    provider = get_embedding_provider()
    vocabularies = vocab.load_vocabularies(get_settings().data_dir)
    subjects = vocab.all_subjects(vocabularies)
    try:
        framing_vecs = {
            question: {
                label: await asyncio.to_thread(_embed_prompts, provider, prompts)
                for label, prompts in labels.items()
            }
            for question, labels in vocab.FRAMING.items()
        }
        subject_vecs = {
            name: await asyncio.to_thread(_embed_prompts, provider, prompts)
            for name, prompts in subjects.items()
        }
    except EmbeddingUnavailable as err:
        log.warning("shots.describe_unavailable", reason=str(err)[:120])
        return 0
    drone = is_drone(asset)
    timed = sorted((int(ts), list(vec)) for ts, vec in frames if vec is not None)
    now = datetime.now(UTC)
    for window in windows:
        inside = [vec for ts, vec in timed if window.start_ms <= ts <= window.end_ms]
        if not inside:
            middle = (window.start_ms + window.end_ms) / 2
            inside = [min(timed, key=lambda item: abs(item[0] - middle))[1]]
        vector = _mean_vector(inside)
        if vector is None:
            continue
        framing = vocab.describe_framing(vector, framing_vecs, is_drone=drone)
        window.framing = framing
        window.subjects = vocab.describe_subjects(vector, subject_vecs)
        top_down = (
            framing.get("angle") == "top-down"
            and float(
                framing.get("angle_conf") or 0  # type: ignore[arg-type]
            )
            >= 0.5
        )
        if top_down and window.features:
            window.move, window.direction = motion.classify(window.features, top_down=True)
        window.described_at = now
    await db.commit()
    log.info("shots.described", asset_id=str(asset.id), windows=len(windows))
    return len(windows)


async def has_embeddings(db: AsyncSession, asset_id: uuid.UUID) -> bool:
    count = (
        await db.execute(
            select(func.count())
            .select_from(Frame)
            .where(Frame.asset_id == asset_id, Frame.embedding.is_not(None))
        )
    ).scalar_one()
    return bool(count)


async def has_windows(db: AsyncSession, asset_id: uuid.UUID) -> bool:
    return (
        await db.execute(select(ShotWindow.id).where(ShotWindow.asset_id == asset_id).limit(1))
    ).first() is not None


async def motion_ready(db: AsyncSession, asset_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(Derivative.id).where(
                Derivative.asset_id == asset_id,
                Derivative.kind == "motion",
                Derivative.status == "ready",
            )
        )
    ).first() is not None
