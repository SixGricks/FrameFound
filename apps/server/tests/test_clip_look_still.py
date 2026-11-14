"""The one function that grabs a still (framefound/processing/shots.py
`still_at`).

A look is judged on a still, and a contact sheet is made of them. Which
file on disk is a moment's still is `nle.shotlist`'s rule, pinned in
test_shotlist_used.py; what is pinned here is what `still_at` adds: the
picture itself, the moment it really shows, a grab from the proxy when
nobody saved one, and a grey blank that says "no picture" when there is
nothing to show.

The stills the motion analysis saves are the same files, written the same
way, so that is pinned here too.

No ffmpeg runs: `scenes.extract_frame` is replaced by a function that
writes a drawn picture, or fails the way ffmpeg does.
"""

import subprocess
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Frame, Library
from framefound.nle import shotlist
from framefound.processing import scenes, shots
from framefound.processing.ffmpeg import FfmpegError

BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
SAVED, SAMPLED, GRABBED = (200, 140, 60), (20, 180, 20), (40, 60, 200)


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """A catalogue with one drone clip: a row, and a data folder with nothing
    in it yet. Each test puts there what its clip has."""
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'stills.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "still-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path / "gelco"))
        db.add(library)
        await db.flush()
        clip = Asset(
            library_id=library.id,
            relative_path="Brookside Country Club/2026/Drone/DJI_0639.MP4",
            filename="DJI_0639.MP4",
            extension="mp4",
            media_type="video",
            size_bytes=10_000,
            mtime=BASE,
            duration_s=40.0,
            availability="online",
            captured_at=BASE,
        )
        db.add(clip)
        await db.commit()

    yield {"factory": factory, "clip": clip, "data": tmp_path / "data"}
    await engine.dispose()
    get_settings.cache_clear()


def _picture(path: Path, colour: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (480, 270), colour).save(path, "JPEG")
    return path


async def _frame(
    env: dict[str, Any], ts_ms: int, relative: str | None = None, *, on_disk: bool = True
) -> Path:
    clip_id = env["clip"].id
    relative = relative or f"frames/{str(clip_id)[:2]}/{clip_id}/{ts_ms:09d}.jpeg"
    async with env["factory"]() as db:
        db.add(Frame(asset_id=clip_id, ts_ms=ts_ms, relative_path=relative))
        await db.commit()
    path: Path = env["data"] / relative
    return _picture(path, SAMPLED) if on_disk else path


def _proxy(env: dict[str, Any]) -> Path:
    proxy = shots.proxy_path(env["data"], env["clip"].id)
    proxy.parent.mkdir(parents=True, exist_ok=True)
    proxy.write_bytes(b"a playable copy")
    return proxy


def _ffmpeg(
    monkeypatch: pytest.MonkeyPatch, write: Callable[[Path], None] | None
) -> list[tuple[Path, Path, float, int]]:
    """Stand in for ffmpeg: `write` makes the file a grab would, or None for
    a clip that will not seek. Returns the grabs asked for."""
    asked: list[tuple[Path, Path, float, int]] = []

    def extract_frame(src: Path, dst: Path, at_seconds: float, max_width: int = 640) -> None:
        asked.append((src, dst, at_seconds, max_width))
        if write is None:
            raise FfmpegError("No frame could be extracted at that timestamp")
        write(dst)

    monkeypatch.setattr(scenes, "extract_frame", extract_frame)
    return asked


def _draws(dst: Path) -> None:
    _picture(dst, GRABBED)


def _writes_half(dst: Path) -> None:
    # How a JPEG starts, and then ffmpeg was stopped.
    dst.write_bytes(bytes.fromhex("ffd8ffe000104a464946") + b" and no more")


def _breaks(path: Path) -> Path:
    """Half a picture standing under a real picture's name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _writes_half(path)
    return path


def _no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def started(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a still that was on disk started a process")

    monkeypatch.setattr(scenes, "extract_frame", started)
    monkeypatch.setattr(subprocess, "run", started)
    monkeypatch.setattr(subprocess, "Popen", started)


def _colour(image: Any) -> tuple[int, int, int]:
    """The picture's colour, near enough to tell the drawn ones apart."""
    r, g, b = image.getpixel((240, 135))
    return (round(r / 20) * 20, round(g / 20) * 20, round(b / 20) * 20)


def _left_in(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


async def _still(env: dict[str, Any], ts_ms: int) -> tuple[Any, int]:
    async with env["factory"]() as db:
        return await shots.still_at(db, env["clip"], ts_ms, data_dir=env["data"])


# --- a picture that is already there ---------------------------------------------


async def test_the_still_the_analysis_saved_is_shown_as_that_moment(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _picture(env["data"] / shots.still_relpath(env["clip"].id, 8000), SAVED)
    await _frame(env, 8000)
    _proxy(env)
    _no_process(monkeypatch)

    picture, shown_ms = await _still(env, 8000)

    # The saved still wins over a sampled frame of the same moment, and with
    # a proxy right there nothing is grabbed.
    assert (_colour(picture), shown_ms) == (SAVED, 8000)
    assert picture.mode == "RGB" and picture.size == (480, 270)


async def test_a_sampled_frame_close_by_is_shown_with_its_own_moment(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _frame(env, 8400)
    await _frame(env, 20000)
    _proxy(env)
    _no_process(monkeypatch)

    picture, shown_ms = await _still(env, 8000)

    # Within 0.6 s it is the same picture: shown as 8.4 s, which is what it
    # is, and not grabbed again as 8.0 s.
    assert (_colour(picture), shown_ms) == (SAMPLED, 8400)
    assert not (env["data"] / shots.still_relpath(env["clip"].id, 8000)).exists()


async def test_the_moment_is_the_frames_own_not_the_one_in_its_file_name(
    env: dict[str, Any],
) -> None:
    """A clip nothing could be decoded from has one frame row, at 0, pointing
    at its thumbnail. The file's name says nothing about when."""
    clip_id = env["clip"].id
    await _frame(env, 0, f"derivatives/{str(clip_id)[:2]}/{clip_id}/thumbnail.jpeg")

    assert (await _still(env, 300))[1] == 0
    # Far from the moment asked for, it is still better than no picture, and
    # says honestly which moment it is.
    picture, shown_ms = await _still(env, 31000)
    assert (_colour(picture), shown_ms) == (SAMPLED, 0)


# --- a picture nobody saved ------------------------------------------------------


async def test_a_missing_still_is_grabbed_from_the_proxy_once_and_kept(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = _proxy(env)
    await _frame(env, 20000)  # eight seconds off: another shot
    asked = _ffmpeg(monkeypatch, _draws)
    saved = env["data"] / shots.still_relpath(env["clip"].id, 12000)

    picture, shown_ms = await _still(env, 12000)

    # The moment asked for, from the proxy, at the size of every other still;
    # a far-off frame is not shown in its place while a grab is possible.
    assert (_colour(picture), shown_ms) == (GRABBED, 12000)
    assert [(src, at, width) for src, _dst, at, width in asked] == [(proxy, 12.0, 480)]
    # Kept under the saved still's own name, and nothing else left beside it.
    assert _left_in(saved.parent) == [saved.name]
    with Image.open(saved) as kept:
        assert _colour(kept.convert("RGB")) == GRABBED

    # From then on it is simply this moment's still: for the next look...
    again, shown_again = await _still(env, 12000)
    assert (_colour(again), shown_again, len(asked)) == (GRABBED, 12000, 1)
    # ...and for a page that only looks and never grabs.
    async with env["factory"]() as db:
        assert await shotlist.still_path(db, env["clip"].id, 12000, nearest=False) == saved


async def test_a_clip_that_will_not_seek_falls_back_to_its_nearest_frame(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _proxy(env)
    await _frame(env, 20000)
    asked = _ffmpeg(monkeypatch, None)
    saved = env["data"] / shots.still_relpath(env["clip"].id, 12000)

    picture, shown_ms = await _still(env, 12000)

    assert len(asked) == 1
    assert (_colour(picture), shown_ms) == (SAMPLED, 20000)
    # Nothing was kept as "the still at 12 s": there is none.
    assert _left_in(saved.parent) == []


async def test_half_a_file_from_a_stopped_grab_is_never_kept_as_the_still(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved still is "this moment of the clip" from then on, for a contact
    sheet and for a model alike. An ffmpeg that was stopped mid-write must
    not leave one."""
    _proxy(env)
    _ffmpeg(monkeypatch, _writes_half)
    saved = env["data"] / shots.still_relpath(env["clip"].id, 12000)

    picture, shown_ms = await _still(env, 12000)

    assert shown_ms == -1 and picture.size == shots.BLANK_SIZE
    assert _left_in(saved.parent) == []
    async with env["factory"]() as db:
        assert await shotlist.still_path(db, env["clip"].id, 12000) is None


async def test_a_grab_writes_beside_the_still_and_moves_in_whole(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sheets asking for the same moment must not read each other's file
    while it is being written: ffmpeg is never pointed at the still's own
    name."""
    _proxy(env)
    saved = env["data"] / shots.still_relpath(env["clip"].id, 12000)
    asked = _ffmpeg(monkeypatch, _draws)

    await _still(env, 12000)
    saved.unlink()  # so that the second look has to grab again
    await _still(env, 12000)

    written = [dst for _src, dst, _at, _width in asked]
    assert len(written) == 2 and saved not in written
    assert written[0] != written[1]
    for path in written:
        # In the same folder (so moving it in is a rename, not a copy), and
        # still a .jpeg, which is how ffmpeg knows what to write.
        assert path.parent == saved.parent and path.suffix == saved.suffix
        assert not path.exists()


async def test_a_still_that_cannot_be_read_is_grabbed_afresh(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = env["data"] / shots.still_relpath(env["clip"].id, 8000)
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"left by something that was stopped")

    # With no proxy there is nothing to make one from: no picture, said so,
    # rather than an error in the middle of a contact sheet.
    picture, shown_ms = await _still(env, 8000)
    assert shown_ms == -1

    _proxy(env)
    _ffmpeg(monkeypatch, _draws)
    picture, shown_ms = await _still(env, 8000)
    assert (_colour(picture), shown_ms) == (GRABBED, 8000)
    with Image.open(saved) as mended:
        assert _colour(mended.convert("RGB")) == GRABBED


async def test_a_broken_still_does_not_hide_a_frame_that_reads(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half a JPEG stands at the middle of a window (a worker was stopped
    while writing it) and the proxies are on a share that is away. The clip
    was sampled 0.4 s from there and that frame reads. The broken file came
    first in line and the clip was answered "no picture"."""
    saved = _breaks(env["data"] / shots.still_relpath(env["clip"].id, 8000))
    broken = saved.read_bytes()
    await _frame(env, 8400)
    await _frame(env, 20000)
    _no_process(monkeypatch)

    picture, shown_ms = await _still(env, 8000)
    assert (_colour(picture), shown_ms) == (SAMPLED, 8400)

    # The same with a proxy that will not give up a frame...
    _proxy(env)
    asked = _ffmpeg(monkeypatch, None)
    picture, shown_ms = await _still(env, 8000)
    assert (_colour(picture), shown_ms, len(asked)) == (SAMPLED, 8400, 1)
    # ...and a grab that failed took nothing away.
    assert saved.read_bytes() == broken

    # With one that will, the moment itself is grabbed rather than the
    # neighbour shown, so the broken file is mended for whoever looks next.
    _ffmpeg(monkeypatch, _draws)
    picture, shown_ms = await _still(env, 8000)
    assert (_colour(picture), shown_ms) == (GRABBED, 8000)
    async with env["factory"]() as db:
        assert await shotlist.still_path(db, env["clip"].id, 8000, nearest=False) == saved
    with Image.open(saved) as mended:
        assert _colour(mended.convert("RGB")) == GRABBED


async def test_a_broken_frame_is_stepped_past_to_the_next_one(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _breaks(await _frame(env, 8200, on_disk=False))
    await _frame(env, 9500)
    _no_process(monkeypatch)

    picture, shown_ms = await _still(env, 8000)

    # Not the moment asked for, and it says which moment it is.
    assert (_colour(picture), shown_ms) == (SAMPLED, 9500)


async def test_a_clip_whose_pictures_are_all_broken_is_not_opened_end_to_end(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contact sheet asks for a dozen moments of a clip. One broken file is
    stepped past; a clip full of them is called pictureless after a few, not
    after every frame it has has been opened for every tile."""
    for ts_ms in range(8000, 8000 + 400 * 9, 400):
        _breaks(await _frame(env, ts_ms, on_disk=False))
    opened: list[Path] = []
    read_still = shots._read_still

    def counted(path: Path) -> Any:
        opened.append(path)
        return read_still(path)

    monkeypatch.setattr(shots, "_read_still", counted)

    picture, shown_ms = await _still(env, 8000)

    assert shown_ms == -1 and picture.size == shots.BLANK_SIZE
    assert len(opened) == len(set(opened)) == shots.UNREADABLE_LIMIT < 9


# --- the stills the analysis saves ------------------------------------------------


async def test_the_analysis_replaces_a_still_that_cannot_be_read(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The analysis used to count any file standing at a still's name as
    made, so half a JPEG from a worker that was stopped stayed for good, at
    the very moment a look is judged on."""
    clip_id, proxy = env["clip"].id, _proxy(env)
    broken = _breaks(env["data"] / shots.still_relpath(clip_id, 8000))
    kept = _picture(env["data"] / shots.still_relpath(clip_id, 12000), SAVED)
    asked = _ffmpeg(monkeypatch, _draws)

    made = shots._extract_stills(proxy, env["data"], clip_id, [8000, 12000, 16000])

    assert made == 3
    # The good still was left alone; the broken one and the missing one were
    # made, at the size of every still.
    assert [(at, width) for _src, _dst, at, width in asked] == [(8.0, 480), (16.0, 480)]
    with Image.open(broken) as mended, Image.open(kept) as untouched:
        assert _colour(mended.convert("RGB")) == GRABBED
        assert _colour(untouched.convert("RGB")) == SAVED
    # Written the way a later grab is: beside the still, never at its name.
    names = {
        shots.still_relpath(clip_id, ts_ms).rsplit("/", 1)[1] for ts_ms in (8000, 12000, 16000)
    }
    assert set(_left_in(broken.parent)) == names
    assert all(dst.name not in names and dst.parent == broken.parent for _src, dst, *_ in asked)


async def test_the_analysis_never_leaves_half_a_still_or_counts_one(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    clip_id, proxy = env["clip"].id, _proxy(env)
    folder = (env["data"] / shots.still_relpath(clip_id, 8000)).parent

    # An ffmpeg that was stopped mid-write, at every moment of the clip.
    _ffmpeg(monkeypatch, _writes_half)
    assert shots._extract_stills(proxy, env["data"], clip_id, [8000, 12000]) == 0
    assert _left_in(folder) == []

    # A broken still that could not be made again is not a still the clip
    # has, and one bad seek does not stop the others being made.
    broken = _breaks(env["data"] / shots.still_relpath(clip_id, 8000))

    def only_twelve(dst: Path) -> None:
        if not dst.name.startswith("000012000."):
            raise FfmpegError("No frame could be extracted at that timestamp")
        _picture(dst, GRABBED)

    _ffmpeg(monkeypatch, only_twelve)
    assert shots._extract_stills(proxy, env["data"], clip_id, [8000, 12000]) == 1
    assert _left_in(folder) == [broken.name, "000012000.jpeg"]


# --- no picture -------------------------------------------------------------------


async def test_a_clip_with_no_picture_gets_a_grey_blank_and_minus_one(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_process(monkeypatch)  # no proxy: nothing to grab from, nothing tried
    await _frame(env, 8200, on_disk=False)

    picture, shown_ms = await _still(env, 8000)

    # -1 is "no picture": never shown to a model, never previewed as the clip.
    assert shown_ms == -1
    assert picture.size == shots.BLANK_SIZE == (480, 270)
    assert picture.getpixel((0, 0)) == picture.getpixel((479, 269)) == shots.BLANK_GREY
    # Looking made no folder for a still that does not exist.
    assert not (env["data"] / shots.still_relpath(env["clip"].id, 8000)).parent.exists()


async def test_a_moment_before_the_clip_starts_is_its_start(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """-1 is the answer for "no picture"; a real picture never comes back
    with a negative moment, whatever was asked."""
    _proxy(env)
    asked = _ffmpeg(monkeypatch, _draws)

    picture, shown_ms = await _still(env, -250)

    assert (_colour(picture), shown_ms) == (GRABBED, 0)
    assert asked[0][2] == 0.0
    assert (env["data"] / shots.still_relpath(env["clip"].id, 0)).is_file()


# --- the connector ----------------------------------------------------------------


async def test_the_connectors_contact_sheet_gets_its_stills_here(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grab used to live in the connector. It is one function now, so a
    sheet and a look can never disagree about what a moment's picture is."""
    pytest.importorskip("mcp")
    from framefound.mcp import shots as connector

    _proxy(env)
    asked = _ffmpeg(monkeypatch, _draws)

    async with env["factory"]() as db:
        picture, shown_ms = await connector._still(db, env["clip"], 12000)
        assert (_colour(picture), shown_ms, len(asked)) == (GRABBED, 12000, 1)
        # What the sheet grabbed is what a look is then judged on.
        again, _ms = await shots.still_at(db, env["clip"], 12000, data_dir=env["data"])
        assert (_colour(again), len(asked)) == (GRABBED, 1)
        blank, none = await connector._still(db, Asset(id=uuid.uuid4()), 5000)
    assert none == -1 and blank.size == shots.BLANK_SIZE
    for gone in ("_grab", "_extract_slots"):
        assert not hasattr(connector, gone), gone
