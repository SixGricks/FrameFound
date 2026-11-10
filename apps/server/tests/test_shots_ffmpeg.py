"""Shot intelligence on real encoded video: synthetic clips made with FFmpeg
(a smooth pan by a moving crop, the same pan with jitter, a push-in by
zoompan, a static shot with something moving in it), decoded and analysed
the way a proxy is, and the motion pass end to end against a database.

Skipped where FFmpeg is not installed; the VM gate and CI have it.
"""

import json
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Derivative, Library, ShotWindow
from framefound.processing import shots

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, timeout=120)  # noqa: S603, S607


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("clips")
    rng = np.random.default_rng(7)
    coarse = Image.fromarray((rng.random((60, 120, 3)) * 255).astype(np.uint8))
    base = np.asarray(coarse.resize((1920, 960), Image.Resampling.BICUBIC)).astype(int)
    fine = (rng.random((960, 1920, 3)) * 50).astype(int)
    texture = out / "texture.png"
    Image.fromarray(np.clip(base * 0.8 + fine, 0, 255).astype(np.uint8)).save(texture)
    still = ["-loop", "1", "-framerate", "30", "-t", "6", "-i", str(texture)]
    made = {name: out / f"{name}.mp4" for name in ("pan", "jitter", "push", "busy")}
    _ffmpeg(*still, "-vf", "crop=640:360:x='100+t*120':y=200", *ENCODE, str(made["pan"]))
    # Whole-pixel jitter (a 7.5 Hz square wave): a crop rounds fractional
    # positions, and that rounding alone is broadband shake at the
    # steadiness real gimbal footage reaches.
    _ffmpeg(
        *still,
        "-vf",
        "crop=640:360:x='100+4*n+4*mod(floor(n/2),2)':y='200+2*mod(floor(n/2),2)'",
        *ENCODE,
        str(made["jitter"]),
    )
    _ffmpeg(
        *still,
        "-vf",
        "scale=3840:1920,zoompan=z='1+0.004*on':d=1:x='iw/2-(iw/zoom/2)'"
        ":y='ih/2-(ih/zoom/2)':s=640x360:fps=30",
        *ENCODE,
        str(made["push"]),
    )
    _ffmpeg(
        *still,
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=200x150:r=30:d=6",
        "-filter_complex",
        "[0]crop=640:360:400:300[bg];[bg][1]overlay=x='60+t*60':y=100:shortest=1",
        *ENCODE,
        str(made["busy"]),
    )
    return made


def _analyse(path: Path) -> list:  # type: ignore[type-arg]
    _, windows, _ = shots.analyse_file(path, 30.0, 640, 360, 6.0)
    return windows


def test_a_smooth_pan_by_a_moving_crop(clips: dict[str, Path]) -> None:
    [window] = _analyse(clips["pan"])
    assert (window.move, window.direction) == ("pan", "right")
    # 120 px/s across a 640 px frame.
    assert window.speed == pytest.approx(120 / 640, rel=0.05)
    assert min(window.smooth_by_speed.values()) > 0.9


def test_the_same_pan_with_jitter_scores_by_speed(clips: dict[str, Path]) -> None:
    """Shake at 7.5 Hz: 3.75 Hz on screen at 50% (plain to see), 30 Hz at
    400% (too fast to see)."""
    [window] = _analyse(clips["jitter"])
    assert window.move == "pan"
    assert window.smooth_by_speed["50"] < 0.3
    assert window.smooth_by_speed["100"] < 0.3
    assert window.smooth_by_speed["400"] > 0.8


def test_a_push_in_by_zoompan(clips: dict[str, Path]) -> None:
    [window] = _analyse(clips["push"])
    assert (window.move, window.direction) == ("push-in", "in")


def test_a_static_shot_with_moving_content(clips: dict[str, Path]) -> None:
    [window] = _analyse(clips["busy"])
    assert window.move == "static"
    assert window.energy == "high"
    calm = _analyse(clips["pan"])[0]
    assert calm.energy == "low", "a pan over still ground is calm, however fast"


# --- the motion pass, end to end -----------------------------------------------


@pytest.fixture()
async def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[AsyncSession]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'shots.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "shots-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()
    get_settings.cache_clear()


async def test_the_motion_pass_reads_the_proxy_and_lights_each_window(
    db: AsyncSession, clips: dict[str, Path], tmp_path: Path
) -> None:
    """Brookside's drone clips carry no GPS; the course's photographs do.
    The clip borrows its folder's position for the sun, and the analysis
    reads the proxy, never the original (which here does not even exist)."""
    library = Library(name="GELCO", root_path=str(tmp_path / "gelco"))
    db.add(library)
    await db.flush()
    clip = Asset(
        library_id=library.id,
        relative_path="Brookside Country Club/2026/Drone/DJI_20261103051509_0639_D.MP4",
        filename="DJI_20261103051509_0639_D.MP4",
        extension="mp4",
        media_type="video",
        size_bytes=1,
        mtime=datetime(2026, 11, 2, tzinfo=UTC),
        fps=30.0,
        width=640,
        height=360,
        duration_s=6.0,
        # 4:15 pm EST: the sun about seven degrees up over Philadelphia.
        captured_at=datetime(2026, 11, 2, 21, 15, tzinfo=UTC),
    )
    photo = Asset(
        library_id=library.id,
        relative_path="Brookside Country Club/2026/Photo/IMG_0001.JPG",
        filename="IMG_0001.JPG",
        extension="jpg",
        media_type="image",
        size_bytes=1,
        mtime=datetime(2026, 11, 2, tzinfo=UTC),
        gps_lat=40.2,
        gps_lon=-75.3,
        gps_source="exif",
    )
    db.add_all([clip, photo])
    await db.flush()
    data_dir = get_settings().data_dir
    proxy = shots.proxy_path(data_dir, clip.id)
    proxy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(clips["pan"], proxy)
    await db.commit()

    count = await shots.analyse_motion(db, clip, library, tmp_path / "missing-original.MP4")
    assert count == 1
    [window] = (await db.execute(select(ShotWindow))).scalars().all()
    assert window.move == "pan" and window.source_fps == pytest.approx(30.0)
    assert window.sun_elevation is not None and 4 < window.sun_elevation < 10
    assert window.light in ("golden hour", "low sun")
    assert window.look["located_by"] == "folder"
    track = (await db.execute(select(Derivative).where(Derivative.kind == "motion"))).scalar_one()
    assert track.status == "ready"
    stored = json.loads((data_dir / track.relative_path).read_text(encoding="utf-8"))
    assert stored["hz"] == 10 and len(stored["vx"]) == 60
    for ts_ms in shots.window_moments(window.start_ms, window.end_ms):
        assert (data_dir / shots.still_relpath(clip.id, ts_ms)).is_file()

    # Re-running rebuilds rather than piling up.
    assert await shots.analyse_motion(db, clip, library, tmp_path / "x") == 1
    assert len((await db.execute(select(ShotWindow))).scalars().all()) == 1


async def test_no_proxy_means_no_analysis(db: AsyncSession, tmp_path: Path) -> None:
    library = Library(name="Intel", root_path=str(tmp_path))
    db.add(library)
    await db.flush()
    clip = Asset(
        library_id=library.id,
        relative_path="a.mp4",
        filename="a.mp4",
        extension="mp4",
        media_type="video",
        size_bytes=1,
        mtime=datetime(2026, 11, 2, tzinfo=UTC),
    )
    db.add(clip)
    await db.commit()
    with pytest.raises(shots.NoProxy):
        await shots.analyse_motion(db, clip, library, tmp_path / "a.mp4")
    assert uuid.UUID(str(clip.id))
