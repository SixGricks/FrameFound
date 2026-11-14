"""The motion pass and a local model's looks: when a clip's windows are
rebuilt, looks at shots whose times moved stop counting, on every path that
rebuilds them (the Celery task here; the inline backfill in
test_shots_backfill.py).

The real `processing.shots.analyse_motion` runs, with only the decode, the
probe and the stills replaced, so a step skipped inside the pass is caught
here and not hidden behind a stand-in for it.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Derivative, Library, Look, ShotWindow
from framefound.looking import store
from framefound.processing import shots
from framefound.processing import tasks as task_module

OLD = [(0, 4000), (4000, 9000), (9000, 15000)]
# What the analysis finds this time: the first shot again at the same
# times, the other two merged into one.
NEW = [(0, 4000), (4000, 15000)]
KEY = "google/gemma-4-E4B-it@abc#look-1"


def fake_analysis(monkeypatch: pytest.MonkeyPatch, spans: list[tuple[int, int]] = NEW) -> None:
    """Everything `analyse_motion` reaches outside the database, replaced:
    the probe, the decode and the stills."""

    def windows(*_args: Any) -> tuple[Any, list[Any], float]:
        found = [
            SimpleNamespace(
                start_s=start / 1000,
                end_s=end / 1000,
                move="pan",
                direction="left",
                speed=0.01,
                smooth=0.9,
                smooth_by_speed={"100": 0.9},
                energy="low",
                energy_score=0.1,
                features={},
                look={},
            )
            for start, end in spans
        ]
        return object(), found, 0.5

    monkeypatch.setattr("framefound.processing.probe.probe_ffprobe", lambda _p: {})
    monkeypatch.setattr(shots, "analyse_file", windows)
    monkeypatch.setattr(shots.motion, "track", lambda _analysis: {})
    monkeypatch.setattr(shots, "_extract_stills", lambda *_args: 0)
    monkeypatch.setattr(task_module.describe_shots, "delay", lambda *_a: None)


async def looked_at_clip(db: AsyncSession, library: Library, name: str) -> Asset:
    """A clip with a proxy, the OLD windows and a current look at every
    one of them: complete, so its roll-up says nothing needs looking at."""
    clip = Asset(
        library_id=library.id,
        relative_path=f"Brookside/{name}",
        filename=name,
        extension="mp4",
        media_type="video",
        size_bytes=1,
        mtime=datetime(2026, 11, 2, tzinfo=UTC),
        availability="online",
        processing_status="ready",
        fps=30.0,
        width=1920,
        height=1080,
        duration_s=15.0,
    )
    db.add(clip)
    await db.flush()
    proxy = shots.proxy_path(get_settings().data_dir, clip.id)
    proxy.parent.mkdir(parents=True, exist_ok=True)
    proxy.write_bytes(b"proxy")
    db.add(
        Derivative(
            asset_id=clip.id,
            kind="proxy",
            relative_path="unused.mp4",
            media_format="mp4",
            status="ready",
        )
    )
    for idx, (start, end) in enumerate(OLD):
        db.add(ShotWindow(asset_id=clip.id, idx=idx, start_ms=start, end_ms=end, move="pan"))
        db.add(
            Look(
                asset_id=clip.id,
                start_ms=start,
                end_ms=end,
                caption=f"shot {idx}",
                score=40 + 10 * idx,
                key=KEY,
                model="google/gemma-4-E4B-it",
            )
        )
    await db.flush()
    await store.roll_up(db, clip.id)
    await db.commit()
    await db.refresh(clip)
    assert clip.looked_at is not None and clip.look_score == 60
    return clip


async def assert_looks_follow_the_windows(db: AsyncSession, clip_id: uuid.UUID) -> None:
    """Only the shot found again keeps its look; the clip is no longer
    complete, so its roll-up asks for the new shot to be looked at."""
    windows = (
        await db.execute(
            select(ShotWindow.start_ms, ShotWindow.end_ms).where(ShotWindow.asset_id == clip_id)
        )
    ).all()
    assert sorted(tuple(w) for w in windows) == NEW
    assert set(await store.current(db, clip_id)) == {(0, 4000)}
    clip = await db.get(Asset, clip_id, populate_existing=True)
    assert clip is not None
    assert (clip.look_score, clip.look_caption, clip.looked_at, clip.looked_by) == (
        40,
        "shot 0",
        None,
        None,
    )


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path / "gelco"))
        db.add(library)
        await db.commit()
        yield {"db": db, "library": library}
    await engine.dispose()
    get_settings.cache_clear()


async def test_the_motion_task_retires_looks_of_shots_that_moved(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    db: AsyncSession = env["db"]
    clip = await looked_at_clip(db, env["library"], "DJI_0639.MP4")
    fake_analysis(monkeypatch)

    await task_module._analyze_motion(db, clip, env["library"], Path("unused.MP4"))

    await assert_looks_follow_the_windows(db, clip.id)


async def test_the_looks_step_runs_once_per_pass(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """It moved out of the task into the pass: the task must not run it a
    second time, and the pass must run it after the windows are written."""
    db: AsyncSession = env["db"]
    clip = await looked_at_clip(db, env["library"], "DJI_0639.MP4")
    fake_analysis(monkeypatch)
    seen: list[list[tuple[int, int]]] = []
    real = store.after_reanalysis

    async def counted(session: AsyncSession, asset_id: uuid.UUID, **kwargs: Any) -> None:
        rows = await session.execute(
            select(ShotWindow.start_ms, ShotWindow.end_ms).where(ShotWindow.asset_id == asset_id)
        )
        seen.append(sorted(tuple(r) for r in rows.all()))
        await real(session, asset_id, **kwargs)

    monkeypatch.setattr(store, "after_reanalysis", counted)

    await task_module._analyze_motion(db, clip, env["library"], Path("unused.MP4"))

    assert seen == [NEW]


async def test_a_failing_looks_step_never_fails_the_pass(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    db: AsyncSession = env["db"]
    clip = await looked_at_clip(db, env["library"], "DJI_0639.MP4")
    fake_analysis(monkeypatch)

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(store, "after_reanalysis", broken)

    assert await shots.motion_pass(db, clip, env["library"], Path("unused.MP4")) == 2

    # The windows stand, and the session is usable for whatever follows.
    windows = (await db.execute(select(ShotWindow).where(ShotWindow.asset_id == clip.id))).all()
    assert len(windows) == 2
    assert await shots.settle_looks(db, clip.id) is False


async def test_the_looks_are_retired_before_the_stills_are_grabbed(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stills are minutes of ffmpeg on a long clip. An inline backfill
    stopped during them (Ctrl-C, a restart) has already committed the new
    windows, and its next run skips a clip whose motion pass is done: the
    looks must have followed the windows by then, not after the stills."""
    db: AsyncSession = env["db"]
    clip = await looked_at_clip(db, env["library"], "DJI_0639.MP4")
    fake_analysis(monkeypatch)
    order: list[str] = []
    real = store.after_reanalysis

    async def settled(session: AsyncSession, asset_id: uuid.UUID, **kwargs: Any) -> None:
        order.append("looks")
        await real(session, asset_id, **kwargs)

    def stopped(*_args: Any) -> int:
        order.append("stills")
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "after_reanalysis", settled)
    monkeypatch.setattr(shots, "_extract_stills", stopped)

    with pytest.raises(KeyboardInterrupt):
        await shots.motion_pass(db, clip, env["library"], Path("unused.MP4"))

    assert order == ["looks", "stills"]
    await assert_looks_follow_the_windows(db, clip.id)
