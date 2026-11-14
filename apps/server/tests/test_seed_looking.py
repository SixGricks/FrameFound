"""The seed's looking part (`infrastructure/scripts/seed_dev.py` `seed_looking`, L3 design 8.9).

A folder review by the local model looks at a clip shot by shot only when
the catalogue says the clip's shots have been found, and it says that with a
ready motion file. The seed's clips had shot windows and no such file, so
on the review copy every one read as "needs a playable copy first" — which
the seed cannot make. These tests run the script as a reviewer does and read
the result through the real API: the dialog's counts, a clip's shot list,
and then a review of a seeded folder in demo mode with nothing handed in,
which is the first place the owner sees one.

The seed adds no look itself, makes no job, and adds its files once per
catalogue. Starting a program or reaching a real address fails the test.
"""

import asyncio
import json
import runpy
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.api.v1 import local_models as models_api
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    AppSetting,
    Asset,
    Derivative,
    Library,
    LocalAiJob,
    Look,
    LookRequest,
    ShotWindow,
)
from framefound.local_models import ask, demo
from framefound.looking import demo as looking_demo
from framefound.looking import scope
from framefound.media import motion
from framefound.storage import stores

SEED = Path(__file__).resolve().parents[3] / "infrastructure" / "scripts" / "seed_dev.py"
LOOKS = "/api/v1/looks"
ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
PART = "looking_motion"
ADDED = (
    "Looking: added a motion file for each of the 5 clips that have shots, "
    "so a review looks at them."
)
NOTHING_ADDED = "Looking: everything it needs is already in this catalogue; nothing was added."
# The seeded GELCO folders: (photographs, clips), three shots a clip.
FOLDERS = {"LedgeRock": (8, 2), "Pete Dye": (6, 2), "Hudson National": (6, 1)}
_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
    "FRAMEFOUND_MIN_FREE_GB",
)


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may start a program or reach a real address; trying to is a failure."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a test tried to start a process or reach a real address")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    for name in _OURS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ask, "_FAKE", None)
    monkeypatch.setattr(demo, "SECONDS_PER_CHUNK", 0.0)
    monkeypatch.setattr(demo, "ANSWER_SECONDS", 0.0)

    def clear() -> None:
        get_settings.cache_clear()
        stores._CACHE.clear()
        models_api._STATUS.clear()
        models_api._LOOKING.clear()

    clear()
    assert demo.stop(), "a demo lane left by an earlier test did not stop"
    yield
    # Reading a page that shows reviews in demo mode starts the demo's lane;
    # it must not outlive the test.
    assert demo.stop(), "the demo's lane did not stop"
    clear()


def _run_seed(root: Path, monkeypatch: pytest.MonkeyPatch, *more: str) -> None:
    """`python infrastructure/scripts/seed_dev.py <root> …`, in this process."""
    for name, value in {
        "FRAMEFOUND_DATABASE_URL": f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}",
        "FRAMEFOUND_DATA_DIR": str(root / "data"),
        "FRAMEFOUND_MEDIA_ROOT": str(root / "media"),
        "FRAMEFOUND_MODELS_DIR": str(root / "models"),
        "FRAMEFOUND_LOCAL_AI_DEMO": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", [str(SEED), str(root), *more])
    failed: list[BaseException] = []

    def run() -> None:
        try:
            runpy.run_path(str(SEED), run_name="__main__")
        except BaseException as err:  # noqa: BLE001 - handed back to the test to raise
            failed.append(err)

    # The script runs its own event loop; here it must have a thread of its own.
    worker = threading.Thread(target=run)
    worker.start()
    worker.join(300)
    assert not worker.is_alive(), "the seed did not finish"
    if failed:
        raise failed[0]
    get_settings.cache_clear()


async def _empty_catalogue(tmp_path: Path) -> Path:
    root = tmp_path / "ff-ui"
    root.mkdir()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return root


@asynccontextmanager
async def _review(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    """The real app on the seeded catalogue, signed in as its first administrator."""
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "seed-looking-secret")
    get_settings.cache_clear()
    engine = create_async_engine(get_settings().db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            setup = await client.post(
                "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
            )
            if setup.status_code != 201:  # already set up: sign in instead
                signed = await client.post("/api/v1/auth/login", json=ADMIN)
                assert signed.status_code == 200, signed.text
            yield client, factory
            # The page's reads started the demo's lane; it ends before its database does.
            assert demo.stop()
    finally:
        await engine.dispose()


async def _get(client: AsyncClient, url: str, **params: Any) -> Any:
    answer = await client.get(url, params=params or None)
    assert answer.status_code == 200, answer.text
    return answer.json()


async def _until(
    client: AsyncClient, url: str, done: Callable[[Any], bool], what: str, seconds: float = 120.0
) -> Any:
    """Read `url` until `done` says yes, as the page's own polling does."""
    give_up = asyncio.get_running_loop().time() + seconds
    while True:
        found = await _get(client, url)
        if done(found):
            return found
        if asyncio.get_running_loop().time() > give_up:
            raise AssertionError(f"{what} never happened: {json.dumps(found)[:4000]}")
        await asyncio.sleep(0.05)


async def _gelco(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[str, dict[str, list[Asset]]]:
    """GELCO's id and its clips by folder, each folder's by path."""
    async with factory() as db:
        library = await db.scalar(select(Library).where(Library.name == "GELCO"))
        assert library is not None
        clips = (
            await db.scalars(
                select(Asset)
                .where(Asset.library_id == library.id, Asset.media_type == "video")
                .order_by(Asset.relative_path)
            )
        ).all()
    by_folder: dict[str, list[Asset]] = {}
    for clip in clips:
        by_folder.setdefault(clip.relative_path.split("/", 1)[0], []).append(clip)
    return str(library.id), by_folder


async def _count(factory: async_sessionmaker[AsyncSession], table: Any, *where: Any) -> int:
    async with factory() as db:
        return int(await db.scalar(select(func.count()).select_from(table).where(*where)) or 0)


async def _motion_rows(factory: async_sessionmaker[AsyncSession]) -> list[Derivative]:
    async with factory() as db:
        return list((await db.scalars(select(Derivative).where(Derivative.kind == "motion"))).all())


async def test_a_seeded_folder_has_shots_for_a_review_and_a_demo_review_looks_at_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = await _empty_catalogue(tmp_path)

    # The plain seed, with no flag: this part is not asked for separately.
    _run_seed(root, monkeypatch)
    said = capsys.readouterr().out.splitlines()
    assert [line for line in said if line.startswith("Looking:")] == [ADDED]

    async with _review(monkeypatch) as (client, factory):
        gelco, clips = await _gelco(factory)
        assert {folder: len(found) for folder, found in clips.items()} == {
            folder: count for folder, (_photos, count) in FOLDERS.items()
        }

        # What was added: one ready motion file a clip, a real track's header
        # with nothing sampled, where every other derived file of the clip is.
        rows = await _motion_rows(factory)
        assert {row.asset_id for row in rows} == {c.id for found in clips.values() for c in found}
        for row in rows:
            assert (row.status, row.media_format) == ("ready", "json")
            assert (
                row.relative_path
                == f"derivatives/{str(row.asset_id)[:2]}/{row.asset_id}/motion.json"
            )
            written = (root / "data" / row.relative_path).read_bytes()
            assert row.size_bytes == len(written) and b"\r" not in written
            track = json.loads(written)
            assert (track["version"], track["hz"]) == (motion.VERSION, motion.TRACK_HZ)
            assert track["duration_s"] > 0 and "vx" not in track

        # It added no look, no review and no job: every job is the demo seed's own.
        assert await _count(factory, Look) == 0 and await _count(factory, LookRequest) == 0
        async with factory() as db:
            kinds = set((await db.scalars(select(LocalAiJob.kind))).all())
        assert kinds == {"runtime_install", "download", "test"}

        # The dialog's counts: every seeded folder's clips are ready, their
        # shots counted, and nothing has to be made first.
        status = await _get(client, f"{LOOKS}/status")
        assert (status["mode"], status["can_manage"]) == ("demo", True)
        assert status["looker"]["problem"] is None and status["pending_suggestions"] == 0
        for folder, (photos, count) in FOLDERS.items():
            preview = await _get(client, f"{LOOKS}/preview", library_id=gelco, folder=folder)
            assert preview["todo"] == {
                "photos": photos,
                "clips": count,
                "shots": 3 * count,
                "stills_only": 0,
            }
            assert preview["first"]["need_copy"] == preview["first"]["need_shots"] == 0
            assert preview["first"]["still_processing"] == preview["first"]["copy_away"] == 0
            assert preview["already"] == {"items": 0, "by_another_model": 0}
        whole = await _get(client, f"{LOOKS}/preview", library_id=gelco, folder="")
        assert (whole["todo"]["clips"], whole["todo"]["shots"]) == (5, 15)

        # A clip's shot list has rows, in order, and nothing has looked yet.
        [clip] = clips["Hudson National"]
        shots = await _get(client, f"/api/v1/assets/{clip.id}/shots")
        assert [shot["idx"] for shot in shots] == [0, 1, 2]
        assert [shot["move"] for shot in shots] == ["top-down drift", "pan", "static"]
        assert all(shot["look"] is None for shot in shots)
        assert all(shot["start_ms"] < shot["still_ms"] < shot["end_ms"] for shot in shots)

        # The looks are made by using the page: Start now on a seeded folder,
        # with the demo's own lane and model server and nothing handed in.
        started = await client.post(
            f"{LOOKS}/reviews", json={"library_id": gelco, "folder": "Hudson National"}
        )
        assert started.status_code == 202, started.text
        review = started.json()
        assert (review["total"], review["preparing"]["state"]) == (9, "")

        def ended(page: Any) -> bool:
            [one] = [r for r in page["reviews"] if r["id"] == review["id"]]
            return bool(one["state"] not in ("waiting", "looking"))

        page = await _until(client, f"{LOOKS}/reviews", ended, "the review finishing")
        [done] = [r for r in page["reviews"] if r["id"] == review["id"]]
        assert (done["state"], done["done"], done["failed"]) == ("done", 9, 0)
        assert done["message"] == "Finished: 9 of 9 were looked at."
        # No clip had to be judged from one still: that is what the file is for.
        assert done["stills_only"] == 0 and done["demo"] is True

        # Each shot was looked at by itself, and says it was made up.
        shots = await _get(client, f"/api/v1/assets/{clip.id}/shots")
        assert len(shots) == 3
        for shot in shots:
            look = shot["look"]
            assert look["demo"] is True and look["reason"] == looking_demo.REASON
            assert (look["start_ms"], look["end_ms"]) == (shot["start_ms"], shot["end_ms"])
            assert look["model"] == demo.LOOKER_MODEL and look["whole"] is False
        assert await _count(factory, Look, Look.asset_id == clip.id, Look.start_ms.is_(None)) == 0
        again = await _get(client, f"{LOOKS}/preview", library_id=gelco, folder="Hudson National")
        assert again["todo"] == {"photos": 0, "clips": 0, "shots": 0, "stills_only": 0}
        assert again["already"]["items"] == 9

    # Seeded again, as the review catalogue is: nothing added, and what the
    # review wrote is as it was.
    async with _review(monkeypatch) as (_client, factory):
        looks = await _count(factory, Look)
    _run_seed(root, monkeypatch, "--local-ai-only")
    assert NOTHING_ADDED in capsys.readouterr().out.splitlines()
    async with _review(monkeypatch) as (_client, factory):
        assert len(await _motion_rows(factory)) == 5
        assert await _count(factory, Look) == looks == 9


async def test_a_review_catalogue_seeded_before_this_part_gets_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The review catalogue as it stands: shot windows from an earlier seed,
    no motion files and no note of them. One clip has a motion file of the
    pipeline's own that failed, which is not the seed's to replace, and a
    clip in another library has never been analysed: a motion file there
    would say its analysis ran and found no shots."""
    root = await _empty_catalogue(tmp_path)
    _run_seed(root, monkeypatch)
    capsys.readouterr()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        _gelco_id, clips = await _gelco(factory)
        [theirs] = clips["Hudson National"]
        async with factory() as db:
            intel = await db.scalar(select(Library).where(Library.name == "Intel"))
            assert intel is not None
            unanalysed = Asset(
                library_id=intel.id,
                relative_path="2026/walkthrough.mp4",
                filename="walkthrough.mp4",
                extension="mp4",
                media_type="video",
                size_bytes=1024,
                mtime=theirs.mtime,
                duration_s=12.0,
            )
            db.add(unanalysed)
            note = await db.get(AppSetting, demo.SEEDED_KEY)
            assert note is not None and note.value[PART] is True
            note.value = {name: True for name in note.value if name != PART}
            await db.execute(
                delete(Derivative).where(
                    Derivative.kind == "motion", Derivative.asset_id != theirs.id
                )
            )
            await db.execute(
                update(Derivative)
                .where(Derivative.kind == "motion")
                .values(status="failed", error="ffmpeg could not read it")
            )
            await db.commit()
        for row in await _motion_rows(factory):
            (root / "data" / row.relative_path).unlink()
        jobs_before = await _count(factory, LocalAiJob)

        _run_seed(root, monkeypatch, "--local-ai-only")
        [line] = [
            line for line in capsys.readouterr().out.splitlines() if line.startswith("Looking:")
        ]
        assert line == ADDED.replace("the 5 clips", "the 4 clips")
        rows = {row.asset_id: row for row in await _motion_rows(factory)}
        assert len(rows) == 5 and rows[theirs.id].status == "failed"
        assert not (root / "data" / rows[theirs.id].relative_path).exists()
        assert sum(row.status == "ready" for row in rows.values()) == 4
        assert unanalysed.id not in rows
        # The four are what a review reads as shots; the fifth is still to be made ready.
        async with factory() as db:
            library = await db.scalar(select(Library).where(Library.name == "GELCO"))
            assert library is not None
            counts = await scope.count(db, scope.Scope(library.id), key="", redo=False)
            assert PART in await demo.seeded_parts(db)
        assert (counts.clips, counts.shots, counts.need_copy) == (4, 12, 1)
        assert await _count(factory, LocalAiJob) == jobs_before
        assert await _count(factory, ShotWindow) == 15

        # Once per catalogue: a file that goes afterwards is not put back, as
        # nothing the seeds add is.
        async with factory() as db:
            await db.execute(delete(Derivative).where(Derivative.kind == "motion"))
            await db.commit()
        _run_seed(root, monkeypatch, "--local-ai-only")
        assert NOTHING_ADDED in capsys.readouterr().out.splitlines()
        assert await _motion_rows(factory) == []
    finally:
        await engine.dispose()


async def test_a_catalogue_with_no_shots_is_told_and_is_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = await _empty_catalogue(tmp_path)

    # `--local-ai-only` on an empty catalogue: no clip, so no shot windows.
    _run_seed(root, monkeypatch, "--local-ai-only")
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Looking:")]
    assert lines == [
        "Looking: not added: the clips' motion files (no clip in this catalogue has shot windows)."
    ]
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Nothing was noted, so the catalogue's footage arriving later gets its files.
        async with factory() as db:
            assert PART not in await demo.seeded_parts(db)
        assert await _motion_rows(factory) == []
    finally:
        await engine.dispose()
    _run_seed(root, monkeypatch)
    assert ADDED in capsys.readouterr().out.splitlines()


async def test_a_clip_given_its_shots_by_a_later_seed_gets_its_motion_file_with_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The seed gives a GELCO clip shot windows on any run that finds it
    with none, but looked for clips missing a motion file only once per
    catalogue. So a clip that turned up after that got windows and no file:
    the one state a folder review cannot finish (it asks about the clip
    again and again). Now the two are written together, or neither is."""
    root = await _empty_catalogue(tmp_path)
    _run_seed(root, monkeypatch)
    capsys.readouterr()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        _gelco_id, clips = await _gelco(factory)
        [seeded] = clips["Hudson National"]

        def clip(name: str) -> Asset:
            return Asset(
                library_id=seeded.library_id,
                relative_path=f"Sand Valley/{name}",
                filename=name,
                extension="mp4",
                media_type="video",
                size_bytes=1024,
                mtime=seeded.mtime,
                duration_s=24.0,
                fps=30.0,
            )

        # Two clips that reached GELCO after the seed: one nothing has
        # touched, one the pipeline is part-way through analysing.
        later, in_hand = clip("DJI_0101_D.mp4"), clip("DJI_0102_D.mp4")
        async with factory() as db:
            db.add_all([later, in_hand])
            await db.flush()
            db.add(
                Derivative(
                    asset_id=in_hand.id,
                    kind="motion",
                    relative_path=f"derivatives/{str(in_hand.id)[:2]}/{in_hand.id}/motion.json",
                    media_format="json",
                    status="pending",
                )
            )
            await db.commit()

        _run_seed(root, monkeypatch, "--local-ai-only")
        said = capsys.readouterr().out.splitlines()
        assert "Cuts: added 3 shot windows with their stills and the clip's motion file." in said
        assert NOTHING_ADDED in said  # the once-only sweep did not run again

        rows = {row.asset_id: row for row in await _motion_rows(factory)}
        assert len(rows) == 7
        # The untouched clip: windows and a ready file, in one go.
        assert await _count(factory, ShotWindow, ShotWindow.asset_id == later.id) == 3
        assert rows[later.id].status == "ready"
        track = json.loads((root / "data" / rows[later.id].relative_path).read_bytes())
        assert track["version"] == motion.VERSION and track["duration_s"] == 24.0
        # The pipeline's own: neither. Windows beside a file that is not
        # ready is exactly what must not be made.
        assert await _count(factory, ShotWindow, ShotWindow.asset_id == in_hand.id) == 0
        assert rows[in_hand.id].status == "pending"
        assert not (root / "data" / rows[in_hand.id].relative_path).exists()

        # A review of the folder reads the first as a clip of three shots,
        # nothing to be made first.
        async with factory() as db:
            counts = await scope.count(
                db, scope.Scope(seeded.library_id, "Sand Valley"), key="", redo=False
            )
        assert (counts.clips, counts.shots, counts.need_copy) == (1, 3, 0)

        # And once more changes nothing.
        _run_seed(root, monkeypatch, "--local-ai-only")
        said = capsys.readouterr().out.splitlines()
        assert "Cuts: everything it needs is already in this catalogue; nothing was added." in said
        assert len(await _motion_rows(factory)) == 7
        assert await _count(factory, ShotWindow) == 18
    finally:
        await engine.dispose()


async def test_a_catalogue_whose_tables_are_older_is_told_to_migrate_not_shown_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A review catalogue from before migration 0034: its files have no
    column for what a look wrote. CONTRIBUTING.md promises the seed says
    what to run."""
    root = await _empty_catalogue(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            db.add(Library(name="GELCO", root_path=str(root / "media" / "gelco")))
            await db.commit()
        async with engine.begin() as conn:
            await conn.exec_driver_sql("ALTER TABLE assets DROP COLUMN look_caption")

        # It ends without raising: `_run_seed` would hand the error on.
        _run_seed(root, monkeypatch, "--local-ai-only")
        said = capsys.readouterr()
        [line] = [line for line in said.out.splitlines() if line.startswith("Looking:")]
        assert line.startswith("Looking: the catalogue's tables are older than this code (")
        assert "look_caption" in line
        assert line.endswith("Run `alembic upgrade head` against ui.db first.")
        assert "Traceback" not in said.out + said.err
        async with factory() as db:
            assert PART not in await demo.seeded_parts(db)
    finally:
        await engine.dispose()
