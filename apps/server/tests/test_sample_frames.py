"""A sampled frame reaches its name whole, or not at all.

Sampling used to point ffmpeg at the frame's own name. A worker stopped
mid-write (a restart or an update kills them) left half a JPEG there, and a
local model that only looks at stills already on disk
(`nle.shotlist.still_path`) was handed it as the picture of that moment.
Frames now go the way the analysis stills do (`processing.shots._grab`):
written beside their name, read back as a picture, then moved in.
"""

import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Frame, Library
from framefound.processing import scenes
from framefound.processing import tasks as task_module


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 36), (40, 120, 200)).save(out, "JPEG")
    return out.getvalue()


WHOLE = _jpeg()
HALF = WHOLE[: len(WHOLE) // 2]


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'frames.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    get_settings.cache_clear()
    (tmp_path / "data").mkdir()
    for name in ("embed_frames", "detect_faces"):
        monkeypatch.setattr(getattr(task_module, name), "delay", lambda *_a: None)
    monkeypatch.setattr(scenes, "should_scene_detect", lambda *_a, **_k: False)
    monkeypatch.setattr(
        scenes, "plan_samples", lambda *_a, **_k: [(0.0, False), (1.0, False), (2.0, False)]
    )

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        library = Library(name="L", root_path=str(tmp_path / "lib"), generate_proxies=False)
        db.add(library)
        await db.flush()
        asset = Asset(
            library_id=library.id,
            relative_path="walk.mp4",
            filename="walk.mp4",
            extension="mp4",
            media_type="video",
            size_bytes=10,
            mtime=datetime.now(UTC),
            duration_s=3.0,
        )
        db.add(asset)
        await db.commit()
        yield {"db": db, "library": library, "asset": asset, "data": tmp_path / "data"}
    await engine.dispose()
    get_settings.cache_clear()


def _frame_dir(env: dict) -> Path:
    asset_id = env["asset"].id
    return env["data"] / "frames" / str(asset_id)[:2] / str(asset_id)


async def test_ffmpeg_is_never_pointed_at_a_frames_own_name(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[tuple[Path, int]] = []

    def extract(_src: Path, dst: Path, _at: float, max_width: int = 640) -> None:
        written.append((dst, max_width))
        dst.write_bytes(WHOLE)

    monkeypatch.setattr(scenes, "extract_frame", extract)

    await task_module._sample_frames(env["db"], env["asset"], env["library"], Path("walk.mp4"))

    finals = {f"{ms:09d}.jpeg" for ms in (0, 1000, 2000)}
    assert len(written) == 3
    assert all(dst.name not in finals and ".part" in dst.name for dst, _ in written)
    # The size search frames have always been made at.
    assert {width for _, width in written} == {640}
    assert {path.name for path in _frame_dir(env).iterdir()} == finals
    frames = (await env["db"].execute(select(Frame).order_by(Frame.ts_ms))).scalars().all()
    assert [frame.ts_ms for frame in frames] == [0, 1000, 2000]
    for frame in frames:
        with Image.open(env["data"] / frame.relative_path) as picture:
            picture.load()


async def test_a_frame_cut_off_mid_write_never_stands_under_its_name(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def extract(_src: Path, dst: Path, at: float, max_width: int = 640) -> None:
        if at == 0.0:
            dst.write_bytes(WHOLE)
        elif at == 1.0:
            dst.write_bytes(HALF)  # ffmpeg said it worked; the file says otherwise
        else:
            dst.write_bytes(HALF)
            raise RuntimeError("ffmpeg was stopped")

    monkeypatch.setattr(scenes, "extract_frame", extract)

    await task_module._sample_frames(env["db"], env["asset"], env["library"], Path("walk.mp4"))

    assert [path.name for path in _frame_dir(env).iterdir()] == ["000000000.jpeg"]
    frames = (await env["db"].execute(select(Frame))).scalars().all()
    assert [frame.ts_ms for frame in frames] == [0]


async def test_a_frame_that_reads_is_not_lost_when_sampling_it_again_fails(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _frame_dir(env) / "000001000.jpeg"
    target.parent.mkdir(parents=True)
    target.write_bytes(WHOLE)

    def extract(_src: Path, dst: Path, *_a: Any, **_k: Any) -> None:
        dst.write_bytes(HALF)
        raise RuntimeError("ffmpeg was stopped")

    monkeypatch.setattr(scenes, "extract_frame", extract)

    await task_module._sample_frames(env["db"], env["asset"], env["library"], Path("walk.mp4"))

    assert target.read_bytes() == WHOLE
    assert [path.name for path in _frame_dir(env).iterdir()] == ["000001000.jpeg"]
