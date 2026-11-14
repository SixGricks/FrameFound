"""Whether a shot was used in a cut, and which file is its still
(framefound/nle/shotlist.py).

Both rules came out of the connector so the API and the job code can apply
them: a model's taste is measured against what an editor actually cut, and
that only means something if "used" is the rule `find_shots` already shows
people. What the connector answers is pinned in test_mcp_shots.py and
test_mcp_video.py, which did not change.
"""

import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Cut, CutPiece, Frame, Library, ShotWindow
from framefound.nle import shotlist

BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
SERVER = Path(__file__).resolve().parents[1]


def _window(start_ms: int, end_ms: int) -> ShotWindow:
    return ShotWindow(asset_id=uuid.uuid4(), idx=0, start_ms=start_ms, end_ms=end_ms, move="orbit")


# --- the rule ------------------------------------------------------------------


def test_a_piece_that_overlaps_by_a_second_used_the_shot() -> None:
    orbit = _window(2000, 14000)

    assert shotlist.used_by(orbit, [(13000, 20000, "Quick 30")]) == ["Quick 30"]
    assert shotlist.used_by(orbit, [(5000, 9000, "Quick 30")]) == ["Quick 30"]
    # Nine tenths of a second of a long piece is the tail of the next shot.
    assert shotlist.used_by(orbit, [(13100, 20000, "Quick 30")]) == []
    assert shotlist.used_by(orbit, [(14000, 20000, "Quick 30")]) == []
    assert shotlist.used_by(orbit, []) == []


def test_a_short_piece_needs_only_half_of_itself() -> None:
    orbit = _window(2000, 14000)

    # 1.2 s pieces across the window's end: 0.7 s inside is most of the
    # piece, 0.5 s is not.
    assert shotlist.used_by(orbit, [(13300, 14500, "Fast one")]) == ["Fast one"]
    assert shotlist.used_by(orbit, [(13500, 14700, "Fast one")]) == []
    # A single frame is not a use, however the halves fall.
    assert shotlist.used_by(orbit, [(5000, 5040, "Flash")]) == []
    assert shotlist.used_by(orbit, [(5000, 5100, "Flash")]) == ["Flash"]


def test_a_cut_that_used_a_shot_twice_is_named_once_and_in_order() -> None:
    orbit = _window(2000, 14000)
    pieces = [
        (3000, 6000, "Members' day"),
        (20000, 26000, "Never here"),
        (7000, 9000, "Quick 30"),
        (10000, 12000, "Members' day"),
    ]

    assert shotlist.used_by(orbit, pieces) == ["Members' day", "Quick 30"]


# --- cut history ---------------------------------------------------------------


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """A catalogue with two Brookside drone clips and nothing else. No app
    and no connector: these functions take a session and a folder."""
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'shotlist.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "shotlist-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    ids: dict[str, uuid.UUID] = {}
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path / "gelco"))
        db.add(library)
        await db.flush()
        for name in ("DJI_0639.MP4", "DJI_0495.MP4"):
            asset = Asset(
                library_id=library.id,
                relative_path=f"Brookside Country Club/2026/Drone/{name}",
                filename=name,
                extension="mp4",
                media_type="video",
                size_bytes=10_000,
                mtime=BASE,
                duration_s=40.0,
                availability="online",
                captured_at=BASE,
            )
            db.add(asset)
            await db.flush()
            ids[name] = asset.id
        await db.commit()

    yield {"factory": factory, "ids": ids, "data": tmp_path / "data"}
    await engine.dispose()
    get_settings.cache_clear()


async def _cut(
    db: AsyncSession, title: str, source: str, pieces: list[tuple[uuid.UUID, int, int]]
) -> uuid.UUID:
    cut = Cut(title=title, client="GELCO", source=source, piece_count=len(pieces))
    db.add(cut)
    await db.flush()
    for position, (asset_id, in_ms, out_ms) in enumerate(pieces):
        db.add(
            CutPiece(
                cut_id=cut.id, position=position, asset_id=asset_id, in_ms=in_ms, out_ms=out_ms
            )
        )
    await db.commit()
    return cut.id


async def test_only_the_chosen_cuts_are_counted(env: dict[str, Any]) -> None:
    orbit, glide = env["ids"]["DJI_0639.MP4"], env["ids"]["DJI_0495.MP4"]
    async with env["factory"]() as db:
        quick = await _cut(db, "Quick 30", "mcp", [(orbit, 3000, 8000), (glide, 21000, 25000)])
        members = await _cut(db, "Members' day", "import", [(orbit, 9000, 12000)])

        everything = await shotlist.pieces_by_asset(db, [orbit, glide])
        assert sorted(everything[orbit]) == [
            (3000, 8000, "Quick 30"),
            (9000, 12000, "Members' day"),
        ]
        assert everything[glide] == [(21000, 25000, "Quick 30")]

        chosen = await shotlist.pieces_by_asset(db, [orbit, glide], cut_ids=[members])
        assert chosen == {orbit: [(9000, 12000, "Members' day")]}
        # A set, a tuple, a list: whatever the caller kept the ids in.
        assert await shotlist.pieces_by_asset(db, (orbit,), cut_ids={quick}) == {
            orbit: [(3000, 8000, "Quick 30")]
        }
        # "No cut counts" is an answer; it must not read as "every cut".
        assert await shotlist.pieces_by_asset(db, [orbit, glide], cut_ids=[]) == {}
        assert await shotlist.pieces_by_asset(db, []) == {}

        window = _window(2000, 14000)
        assert shotlist.used_by(window, everything[orbit]) == ["Quick 30", "Members' day"]
        assert shotlist.used_by(window, chosen[orbit]) == ["Members' day"]


async def test_a_clip_far_down_a_long_list_is_still_asked_about(env: dict[str, Any]) -> None:
    """The ids go to the database in parts; a library of clips in one list
    is more than SQLite takes, and the last part must not be dropped."""
    orbit = env["ids"]["DJI_0639.MP4"]
    strangers = [uuid.uuid4() for _ in range(2 * shotlist.PIECES_CHUNK + 7)]
    async with env["factory"]() as db:
        quick = await _cut(db, "Quick 30", "mcp", [(orbit, 3000, 8000)])

        found = await shotlist.pieces_by_asset(db, [*strangers, orbit])
        assert found == {orbit: [(3000, 8000, "Quick 30")]}
        assert await shotlist.pieces_by_asset(db, [*strangers, orbit], cut_ids=[quick]) == found


async def test_a_draft_source_is_not_finished_work(env: dict[str, Any]) -> None:
    orbit = env["ids"]["DJI_0639.MP4"]
    async with env["factory"]() as db:
        await _cut(db, "Quick 30", "mcp", [(orbit, 3000, 8000)])
        await _cut(db, "Members' day", "import", [(orbit, 9000, 12000)])
        await _cut(db, "A model's first try", "local", [(orbit, 20000, 26000)])

        finished = (
            await db.execute(select(Cut.id, Cut.title).where(shotlist.counts_as_used()))
        ).all()
        assert sorted(title for _, title in finished) == ["Members' day", "Quick 30"]

        # What a comparison reads: the cuts it checked, and nothing else.
        pieces = await shotlist.pieces_by_asset(
            db, [orbit], cut_ids=[cut_id for cut_id, _ in finished]
        )
        assert shotlist.used_by(_window(18000, 30000), pieces[orbit]) == []
        # What find_shots reads applies the same rule (L5): a draft nobody
        # downloaded marks no shot used, while the finished cuts still do.
        every = await shotlist.pieces_by_asset(db, [orbit])
        assert shotlist.used_by(_window(18000, 30000), every[orbit]) == []
        assert shotlist.used_by(_window(2000, 14000), every[orbit]) == ["Quick 30", "Members' day"]

        # Once its Premiere file has been downloaded, the draft went somewhere.
        draft = (await db.execute(select(Cut).where(Cut.source == "local"))).scalar_one()
        draft.sent_at = BASE
        await db.commit()
        sent = await shotlist.pieces_by_asset(db, [orbit])
        assert shotlist.used_by(_window(18000, 30000), sent[orbit]) == ["A model's first try"]
        finished = (await db.execute(select(Cut.title).where(shotlist.counts_as_used()))).all()
        assert len(finished) == 3


# --- stills --------------------------------------------------------------------


def _picture(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (48, 27), (40, 90, 200)).save(path, "JPEG")
    return path


async def _frame(
    env: dict[str, Any], asset_id: uuid.UUID, ts_ms: int, *, on_disk: bool = True
) -> Path:
    relative = f"frames/{str(asset_id)[:2]}/{asset_id}/{ts_ms:09d}.jpeg"
    async with env["factory"]() as db:
        db.add(Frame(asset_id=asset_id, ts_ms=ts_ms, relative_path=relative))
        await db.commit()
    path: Path = env["data"] / relative
    return _picture(path) if on_disk else path


async def test_the_still_the_analysis_saved_is_the_shots_still(env: dict[str, Any]) -> None:
    from framefound.processing import shots

    orbit, glide = env["ids"]["DJI_0639.MP4"], env["ids"]["DJI_0495.MP4"]
    saved = _picture(env["data"] / shots.still_relpath(orbit, 8000))
    await _frame(env, orbit, 8000)
    await _frame(env, glide, 8000)

    async with env["factory"]() as db:
        # The saved still wins over a sampled frame of the same moment.
        assert await shotlist.still_path(db, orbit, 8000) == saved
        assert await shotlist.still_path(db, orbit, 8000, nearest=False) == saved
        # Asked about another data folder, it looks there and nowhere else.
        elsewhere = get_settings().model_copy(update={"data_dir": env["data"].parent / "other"})
        assert await shotlist.still_path(db, orbit, 8000, elsewhere) is None


async def test_a_sampled_frame_close_by_stands_in_for_the_still(env: dict[str, Any]) -> None:
    orbit, glide = env["ids"]["DJI_0639.MP4"], env["ids"]["DJI_0495.MP4"]
    near = await _frame(env, orbit, 8400)
    await _frame(env, orbit, 20000)
    # Another clip's frame at the very moment is not this clip's picture.
    await _frame(env, glide, 8000)

    async with env["factory"]() as db:
        assert await shotlist.still_path(db, orbit, 8000, nearest=False) == near
        assert await shotlist.still_path(db, orbit, 8000) == near
        assert await shotlist.still_path(db, orbit, 9000, nearest=False) == near
        assert await shotlist.still_path(db, orbit, 9001, nearest=False) is None


async def test_a_far_off_frame_is_not_this_shots_still(env: dict[str, Any]) -> None:
    orbit = env["ids"]["DJI_0639.MP4"]
    far = await _frame(env, orbit, 20000)

    async with env["factory"]() as db:
        # Eight seconds away is another shot: never shown as this one...
        assert await shotlist.still_path(db, orbit, 12000, nearest=False) is None
        # ...though a contact sheet may prefer it to a grey tile.
        assert await shotlist.still_path(db, orbit, 12000) == far
        assert await shotlist.still_path(db, orbit, 12000, nearest=True) == far


async def test_a_frame_whose_file_is_gone_is_no_still(env: dict[str, Any]) -> None:
    orbit = env["ids"]["DJI_0639.MP4"]
    await _frame(env, orbit, 8200, on_disk=False)

    async with env["factory"]() as db:
        assert await shotlist.still_path(db, orbit, 8000) is None
        assert await shotlist.still_path(db, orbit, 8000, nearest=False) is None
        # A clip nobody sampled has no picture either.
        assert await shotlist.still_path(db, env["ids"]["DJI_0495.MP4"], 8000) is None


async def test_a_file_the_caller_could_not_read_is_stepped_past(env: dict[str, Any]) -> None:
    """Looking only asks whether a file is there, so half a JPEG left by a
    stopped ffmpeg counts as the still, and being first in line it hid every
    sampled frame behind it. Whoever opened it and found no picture hands it
    back, and gets the next file in the same order."""
    from framefound.processing import shots

    orbit, data = env["ids"]["DJI_0639.MP4"], env["data"]
    saved = _picture(data / shots.still_relpath(orbit, 8000))
    near = await _frame(env, orbit, 8400)
    far = await _frame(env, orbit, 20000)
    async with env["factory"]() as db:
        # A second row pointing at the same picture is the same picture.
        relative = near.relative_to(data).as_posix()
        db.add(Frame(asset_id=orbit, ts_ms=8500, relative_path=relative))
        await db.commit()

        assert await shotlist.still_found(db, orbit, 8000, data) == (saved, 8000)
        # Past the saved still is the frame close by, shown as its own moment.
        assert await shotlist.still_found(db, orbit, 8000, data, skip={saved}) == (near, 8400)
        assert await shotlist.still_found(db, orbit, 8000, data, nearest=False, skip={saved}) == (
            near,
            8400,
        )
        # Past that one too, eleven seconds off is still not "this shot"...
        both = {saved, near}
        assert await shotlist.still_found(db, orbit, 8000, data, nearest=False, skip=both) is None
        # ...though it will do where anything beats a blank.
        assert await shotlist.still_found(db, orbit, 8000, data, skip=both) == (far, 20000)
        assert await shotlist.still_found(db, orbit, 8000, data, skip={*both, far}) is None
        # A file that was never in the way changes nothing.
        assert await shotlist.still_found(db, orbit, 8000, data, skip={far}) == (saved, 8000)
        # And the path alone, for a page, is as it always was.
        assert await shotlist.still_path(db, orbit, 8000) == saved


async def test_finding_a_still_never_starts_a_process(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connector's contact sheet grabs a missing still from the proxy.
    A page that is only looking must not: it would start ffmpeg from a read
    and wait on a proxy store that may be away."""
    from framefound.processing import scenes, shots

    orbit = env["ids"]["DJI_0639.MP4"]
    proxy = shots.proxy_path(env["data"], orbit)
    proxy.parent.mkdir(parents=True, exist_ok=True)
    proxy.write_bytes(b"a playable copy is right there")

    def started(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("looking for a still started a process")

    monkeypatch.setattr(scenes, "extract_frame", started)
    monkeypatch.setattr(subprocess, "run", started)
    monkeypatch.setattr(subprocess, "Popen", started)

    async with env["factory"]() as db:
        assert await shotlist.still_path(db, orbit, 8000) is None
        assert await shotlist.still_path(db, orbit, 8000, nearest=False) is None

    saved = env["data"] / shots.still_relpath(orbit, 8000)
    assert not saved.exists()
    assert not saved.parent.exists()  # not even the folder a grab would make


# --- where it may be used ------------------------------------------------------


def test_the_rules_load_without_the_connector() -> None:
    """The reason they moved: `framefound.mcp` needs the optional mcp
    package, so nothing outside the connector could ask "was this used".
    Asked of a fresh interpreter, because this one may already hold the
    connector from another test file."""
    code = (
        "import sys\n"
        "import framefound.nle.shotlist\n"
        "import framefound.processing.shots\n"
        "held = sorted(m for m in sys.modules if m.split('.')[0] == 'mcp'"
        " or m.startswith('framefound.mcp'))\n"
        "print(','.join(held))\n"
    )
    # A fixed command line of our own, run with this interpreter.
    done = subprocess.run(  # noqa: S603 - no outside input in the arguments
        [sys.executable, "-c", code], cwd=SERVER, capture_output=True, text=True, check=False
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""
