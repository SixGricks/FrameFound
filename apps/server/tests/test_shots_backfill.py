"""`python -m framefound.ops.shots backfill --inline`: the motion pass run
here, one clip at a time, must leave a local model's looks as the Celery
task does — looks of shots that moved retired, the roll-up made again.

It used to call `analyse_motion` alone, so a clip analysed inline kept the
looks of shots that no longer exist current and its `looked_at` set:
nothing asked about the new shots, and the stale score still nudged search
and decided culling.
"""

import argparse
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_analyse_motion import (
    NEW,
    assert_looks_follow_the_windows,
    fake_analysis,
    looked_at_clip,
)

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Library, ShotWindow
from framefound.looking import scope, store
from framefound.ops import shots as ops_shots
from framefound.processing import shots
from framefound.processing import tasks as task_module


@pytest.fixture()
async def catalogue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """A file database the backfill opens itself, as it does on a server."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'backfill.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path / "gelco"))
        db.add(library)
        await db.commit()
    yield factory, library
    await engine.dispose()
    get_settings.cache_clear()


def _inline(**options: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "library": "GELCO",
        "folder": None,
        "make_proxies": False,
        "inline": True,
        "redo": True,
        "limit": 0,
    }
    return argparse.Namespace(**(defaults | options))


async def test_an_inline_backfill_retires_looks_of_shots_that_moved(
    catalogue: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    factory, library = catalogue
    async with factory() as db:
        clip_id = (await looked_at_clip(db, library, "DJI_0639.MP4")).id
    fake_analysis(monkeypatch)

    assert await ops_shots.backfill(_inline()) == 0

    assert "DJI_0639.MP4, 15.0," in capsys.readouterr().out
    async with factory() as db:
        await assert_looks_follow_the_windows(db, clip_id)


async def test_a_failing_looks_step_does_not_stop_the_next_clip(
    catalogue: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first clip's looks step fails after touching the database, so
    its rollback expires every row the loop loaded; the loop must still
    report it and analyse the second clip, whose looks follow as usual."""
    factory, library = catalogue
    async with factory() as db:
        first = (await looked_at_clip(db, library, "DJI_0001.MP4")).id
        second = (await looked_at_clip(db, library, "DJI_0002.MP4")).id
    fake_analysis(monkeypatch)
    real = store.after_reanalysis

    async def flaky(db: AsyncSession, asset_id: uuid.UUID, **kwargs: Any) -> None:
        if asset_id == first:
            await db.execute(select(ShotWindow.id))  # a transaction is open
            raise RuntimeError("database went away")
        await real(db, asset_id, **kwargs)

    monkeypatch.setattr(store, "after_reanalysis", flaky)

    assert await ops_shots.backfill(_inline()) == 0

    out = capsys.readouterr().out
    assert "DJI_0001.MP4, 15.0," in out and "DJI_0002.MP4, 15.0," in out
    async with factory() as db:
        await assert_looks_follow_the_windows(db, second)
        windows = (
            await db.execute(
                select(ShotWindow.start_ms, ShotWindow.end_ms).where(ShotWindow.asset_id == first)
            )
        ).all()
        assert sorted(tuple(w) for w in windows) == NEW, "the analysis itself was kept"


async def test_a_backfill_leaves_a_clip_with_windows_and_a_ready_analysis_file(
    catalogue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The way the Mac's clips got their shots (`framefound ops shots
    backfill`, per library, 2026-11-13): it writes the analysis file and
    marks it ready before it writes the windows, so it never leaves a clip
    with windows and no ready file — the state a folder review once asked
    about for ever. A clip already in that state (here: windows, no file)
    is picked up by a plain backfill, not only `--redo`, queued or inline."""
    factory, library = catalogue
    async with factory() as db:
        clip_id = (await looked_at_clip(db, library, "DJI_0639.MP4")).id
        assert not await shots.motion_ready(db, clip_id)
    fake_analysis(monkeypatch)
    queued: list[str] = []
    monkeypatch.setattr(task_module.analyze_motion, "delay", queued.append)

    assert await ops_shots.backfill(_inline(inline=False, redo=False)) == 0
    assert queued == [str(clip_id)]

    assert await ops_shots.backfill(_inline(redo=False)) == 0
    async with factory() as db:
        assert await shots.motion_ready(db, clip_id)
        assert await shots.has_windows(db, clip_id)
        state = await db.scalar(select(scope._state_sql()).where(Asset.id == clip_id))
    assert state == "shots"


async def test_a_backfill_with_no_library_named_does_every_library(
    catalogue: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`framefound ops shots backfill` with nothing after it, as the Mac's
    instructions give it: each library in turn, by name, not a usage error."""
    factory, library = catalogue
    async with factory() as db:
        other = Library(name="Intel", root_path=str(Path(library.root_path).parent / "intel"))
        db.add(other)
        await db.commit()
        await looked_at_clip(db, library, "DJI_0639.MP4")
        await looked_at_clip(db, other, "DJI_0700.MP4")
    fake_analysis(monkeypatch)

    assert await ops_shots.backfill(_inline(library=None)) == 0

    out = capsys.readouterr().out
    assert out.index("GELCO: 1 videos") < out.index("Intel: 1 videos")
    assert "DJI_0639.MP4, 15.0," in out and "DJI_0700.MP4, 15.0," in out


async def test_a_library_that_fails_does_not_stop_the_next_one(
    catalogue: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    factory, library = catalogue
    async with factory() as db:
        db.add(Library(name="Intel", root_path=str(Path(library.root_path).parent / "intel")))
        await db.commit()
    seen: list[str] = []

    async def one(args: argparse.Namespace) -> int:
        seen.append(args.library)
        if args.library == "GELCO":
            raise RuntimeError("share not mounted")
        return 0

    monkeypatch.setattr(ops_shots, "_backfill_library", one)

    assert await ops_shots.backfill(_inline(library=None)) == 1

    assert seen == ["GELCO", "Intel"]
    assert "GELCO: failed (share not mounted)" in capsys.readouterr().out


def test_the_command_line_takes_backfill_without_a_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[argparse.Namespace] = []

    async def record(args: argparse.Namespace) -> int:
        asked.append(args)
        return 0

    monkeypatch.setattr(ops_shots, "backfill", record)

    assert ops_shots.main(["backfill"]) == 0

    assert asked[0].library is None
