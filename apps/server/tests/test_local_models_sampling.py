"""Choosing a comparison's sample, and freezing it
(framefound/local_models/sampling.py).

The catalogue is built by each test on file-backed SQLite: a library, clips
with shot windows and stills on disk, photographs with previews, tags with
every ruling, cuts. Pictures are small JPEGs drawn with Pillow under
`tmp_path`, at the paths the app uses. Nothing here runs ffmpeg, a model or
a queue.
"""

import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import (
    Asset,
    AssetTag,
    Comparison,
    ComparisonItem,
    Cut,
    CutPiece,
    Derivative,
    Frame,
    Library,
    Listing,
    ListingItem,
    ShotWindow,
    Tag,
)
from framefound.local_models import sampling
from framefound.local_models.sampling import SampleError, SampleSpec
from framefound.looking import prompt
from framefound.processing import shots as shot_files
from framefound.storage import stores

BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)


def _jpeg(path: Path, shade: tuple[int, int, int] = (90, 120, 150), size=(64, 36)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, shade).save(path, "JPEG")
    return path


@dataclass
class Catalogue:
    factory: Any
    data: Path
    library: uuid.UUID
    other_library: uuid.UUID
    made: int = 0
    tags: dict[str, uuid.UUID] = field(default_factory=dict)

    def session(self) -> AsyncSession:
        session: AsyncSession = self.factory()
        return session

    async def video(
        self,
        relative_path: str,
        spans: list[tuple[int, int]],
        *,
        stills: bool = True,
        library: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """A clip with these shot windows, each with its three stills on
        disk unless `stills` is False."""
        async with self.session() as db:
            asset = self._asset(relative_path, "video", library)
            db.add(asset)
            await db.flush()
            for idx, (start, end) in enumerate(spans):
                db.add(
                    ShotWindow(
                        asset_id=asset.id,
                        idx=idx,
                        start_ms=start,
                        end_ms=end,
                        move="orbit",
                        direction="right",
                        speed=0.1,
                        smooth=0.93,
                        energy="low",
                        light="golden",
                        look={"sky": "overcast"},
                        subjects={"barn": 0.9},
                    )
                )
                if stills:
                    for moment in set(shot_files.window_moments(start, end)):
                        _jpeg(self.data / shot_files.still_relpath(asset.id, moment))
            await db.commit()
            return asset.id

    async def photo(
        self,
        relative_path: str,
        *,
        preview: bool = True,
        on_disk: bool = True,
        library: uuid.UUID | None = None,
    ) -> uuid.UUID:
        async with self.session() as db:
            asset = self._asset(relative_path, "image", library)
            db.add(asset)
            await db.flush()
            relative = f"derivatives/{str(asset.id)[:2]}/{asset.id}/preview.jpeg"
            if preview:
                db.add(
                    Derivative(
                        asset_id=asset.id,
                        kind="preview",
                        relative_path=relative,
                        media_format="jpeg",
                        status="ready",
                    )
                )
                if on_disk:
                    _jpeg(self.data / relative, (200, 180, 160), (1600, 1200))
            await db.commit()
            return asset.id

    def _asset(self, relative_path: str, media_type: str, library: uuid.UUID | None) -> Asset:
        self.made += 1
        return Asset(
            library_id=library or self.library,
            relative_path=relative_path,
            filename=relative_path.rsplit("/", 1)[-1],
            extension=relative_path.rsplit(".", 1)[-1].lower(),
            media_type=media_type,
            size_bytes=1000,
            mtime=BASE,
            availability="online",
            captured_at=BASE + timedelta(minutes=self.made),
        )

    async def rule(self, asset_id: uuid.UUID, name: str, source: str) -> None:
        async with self.session() as db:
            if name not in self.tags:
                tag = Tag(name=name, slug=name.lower().replace(" ", "-"))
                db.add(tag)
                await db.flush()
                self.tags[name] = tag.id
            db.add(AssetTag(asset_id=asset_id, tag_id=self.tags[name], source=source))
            await db.commit()

    async def cut(
        self, title: str, source: str, pieces: list[tuple[uuid.UUID | None, int, int]]
    ) -> uuid.UUID:
        async with self.session() as db:
            cut = Cut(title=title, client="GELCO", source=source, piece_count=len(pieces))
            db.add(cut)
            await db.flush()
            for position, (asset_id, in_ms, out_ms) in enumerate(pieces):
                db.add(
                    CutPiece(
                        cut_id=cut.id,
                        position=position,
                        asset_id=asset_id,
                        in_ms=in_ms,
                        out_ms=out_ms,
                    )
                )
            await db.commit()
            return cut.id

    async def draw(self, spec: SampleSpec) -> sampling.Drawn:
        async with self.session() as db:
            return await sampling.draw(db, spec)

    def folder(self, folder: str, **more: Any) -> SampleSpec:
        return SampleSpec(kind="folder", library_id=self.library, folder=folder, **more)


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    get_settings.cache_clear()
    stores._CACHE.clear()


@pytest.fixture()
async def cat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Catalogue]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'sampling.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "sampling-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    # The free-space floor is the real one; a test machine's disk may be
    # fuller than a production data drive.
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "0")
    get_settings.cache_clear()
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        gelco = Library(name="GELCO", root_path=str(tmp_path / "gelco"))
        intel = Library(name="Intel", root_path=str(tmp_path / "intel"))
        db.add_all([gelco, intel])
        await db.commit()
    (tmp_path / "data").mkdir()
    yield Catalogue(factory, tmp_path / "data", gelco.id, intel.id)
    await engine.dispose()


def _spans(count: int, length: int = 4000) -> list[tuple[int, int]]:
    return [(i * length, (i + 1) * length) for i in range(count)]


def _paths(drawn: sampling.Drawn) -> list[str]:
    return [item.relative_path for item in drawn.items]


# --- what a folder is --------------------------------------------------------------


async def test_a_folder_takes_everything_under_it_and_nothing_beside_it(cat: Catalogue) -> None:
    await cat.video("Brookside/2026/Drone/DJI_0001.MP4", _spans(3))
    await cat.video("Brookside/2026/Drone/Low/DJI_0002.MP4", _spans(2))
    await cat.photo("Brookside/2026/Stills/IMG_0001.JPG")
    await cat.video("Brookside/2026-old/Drone/DJI_0009.MP4", _spans(4))
    await cat.photo("Brookside/2026/Stills/IMG_0002.JPG", library=cat.other_library)
    await cat.photo("Brookside/50% off/IMG_0003.JPG")
    await cat.photo("Brookside/50 percent off/IMG_0004.JPG")

    drawn = await cat.draw(cat.folder("/Brookside/2026/"))

    assert sorted(set(_paths(drawn))) == [
        "Brookside/2026/Drone/DJI_0001.MP4",
        "Brookside/2026/Drone/Low/DJI_0002.MP4",
        "Brookside/2026/Stills/IMG_0001.JPG",
    ]
    assert all(item.library_name == "GELCO" for item in drawn.items)
    assert drawn.pool == {"photos": 1, "shots": 5, "clips": 2}
    assert drawn.label == "GELCO › Brookside/2026"
    assert drawn.spec.folder == "Brookside/2026"

    percent = await cat.draw(cat.folder("Brookside/50% off"))
    assert _paths(percent) == ["Brookside/50% off/IMG_0003.JPG"]

    # A folder renamed in Finder with a space at the end is that folder,
    # not the one without the space.
    await cat.photo("Brookside/Rev 2 /IMG_0005.JPG")
    spaced = await cat.draw(cat.folder("Brookside/Rev 2 "))
    assert _paths(spaced) == ["Brookside/Rev 2 /IMG_0005.JPG"]
    assert spaced.label == "GELCO › Brookside/Rev 2 "

    with pytest.raises(SampleError, match="cannot use . or .. to point elsewhere"):
        await cat.draw(cat.folder("Brookside/../Intel"))


async def test_a_bad_request_is_refused_in_a_sentence(cat: Catalogue) -> None:
    await cat.photo("Brookside/IMG_0001.JPG")
    refusals = {
        "between 10 and 200": cat.folder("", size=5),
        "between 10 and 200 items": cat.folder("", size=201),
        "one still or three": cat.folder("", stills=2),  # type: ignore[arg-type]
        "library is no longer": SampleSpec(kind="folder", library_id=uuid.uuid4()),
        "Choose the library": SampleSpec(kind="folder"),
        "listing is no longer": SampleSpec(kind="listing", listing_id=uuid.uuid4()),
        "at least one cut": SampleSpec(kind="cuts"),
        "no longer in cut history": SampleSpec(kind="cuts", cut_ids=(uuid.uuid4(),)),
        "comparison is no longer": SampleSpec(kind="comparison", comparison_id=uuid.uuid4()),
        "empty part": cat.folder("Brookside//2026"),
    }
    for words, spec in refusals.items():
        with pytest.raises(SampleError, match=words):
            await cat.draw(spec)

    with pytest.raises(SampleError) as empty:
        await cat.draw(cat.folder("Nowhere", what="shots"))
    assert str(empty.value) == "There is nothing to compare on here: it holds no shots."


# --- which shots, in which order -------------------------------------------------


async def test_shots_used_in_a_cut_come_with_the_ones_left_out(cat: Catalogue) -> None:
    used = await cat.video("Brookside/Drone/DJI_0639.MP4", _spans(4))
    beside = await cat.video("Brookside/Drone/DJI_0640.MP4", [(0, 5000), (5000, 5600)])
    await cat.video("Brookside/Drone/DJI_0641.MP4", [])  # never analysed
    await cat.video("Brookside/Drone/Deeper/DJI_0700.MP4", _spans(2))  # below, not beside
    gone = await cat.video("Brookside/Drone/DJI_0642.MP4", _spans(1))
    quick = await cat.cut(
        "Brookside Quick 30 - Finished Work - Rev 2",
        "import",
        [(used, 4500, 7000), (used, 12500, 15000), (gone, 0, 2000), (None, 0, 1000)],
    )
    async with cat.session() as db:
        await db.delete(await db.get(Asset, gone))
        await db.commit()

    drawn = await cat.draw(SampleSpec(kind="cuts", cut_ids=(quick,)))

    assert drawn.label == (
        "Shots used in Brookside Quick 30 - Finished Work - Rev 2, and the clips beside them"
    )
    assert drawn.spec.what == "shots"
    assert {item.relative_path for item in drawn.items} == {
        "Brookside/Drone/DJI_0639.MP4",
        "Brookside/Drone/DJI_0640.MP4",
    }
    by_span = {(item.asset_id, item.start_ms): item for item in drawn.items}
    assert by_span[(used, 4000)].used is True
    assert by_span[(used, 4000)].used_in == ("Brookside Quick 30 - Finished Work - Rev 2",)
    assert by_span[(used, 12000)].used is True
    assert by_span[(used, 0)].used is False
    assert by_span[(beside, 0)].used is False
    assert drawn.left_out == {"pieces_gone": 2, "no_shots": 1, "short": 1}
    assert [cut[0] for cut in drawn.counted_cuts] == [quick]
    assert sampling.left_out_sentence("pieces_gone", 2) == (
        "2 pieces of these cuts are from clips that are no longer in the catalogue."
    )

    alone = await cat.draw(SampleSpec(kind="cuts", cut_ids=(quick,), neighbours=False))
    assert {item.asset_id for item in alone.items} == {used}


async def test_used_shots_fill_at_most_half_when_there_are_others(cat: Catalogue) -> None:
    clips = [await cat.video(f"Course/Drone/DJI_{n:04d}.MP4", _spans(6)) for n in range(4)]
    # Every shot of the first three clips was used.
    finished = await cat.cut("Members' day", "mcp", [(c, 0, 24000) for c in clips[:3]])

    drawn = await cat.draw(cat.folder("Course", what="shots", size=10, seed=3))

    assert [cut[0] for cut in drawn.counted_cuts] == [finished]
    assert len(drawn.items) == 10
    assert sum(1 for item in drawn.items if item.used) == 5
    # Used shots make up the rest only once the unused ones run out.
    bigger = await cat.draw(cat.folder("Course", what="shots", size=20, seed=3))
    assert sum(1 for item in bigger.items if not item.used) == 6
    assert sum(1 for item in bigger.items if item.used) == 14


def test_the_order_tries_at_most_half_used_shots_before_the_others() -> None:
    """`order` alone, with no pictures to find: the used pass stops at half
    the shots wanted, and the rest of the used shots come last."""
    clips = [uuid.uuid4() for _ in range(3)]

    def shot(clip: int, n: int, used: bool) -> sampling.Candidate:
        return sampling.Candidate(
            kind="shot",
            asset_id=clips[clip],
            library_name="GELCO",
            relative_path=f"Course/DJI_{clip}.MP4",
            filename=f"DJI_{clip}.MP4",
            media_type="video",
            start_ms=n * 4000,
            end_ms=(n + 1) * 4000,
            facts={},
            facts_line="",
            truth_yes=(),
            truth_no=(),
            used=used,
            used_in=("Members' day",) if used else (),
        )

    pool = [shot(c, n, used=c < 2) for c in range(3) for n in range(8)]

    tried = sampling.order(pool, SampleSpec(kind="folder", what="shots", size=10, seed=1))

    assert len(tried) == len(pool)
    assert [item.used for item in tried[:5]] == [True] * 5
    assert [item.used for item in tried[5:13]] == [False] * 8
    assert all(item.used for item in tried[13:])
    # The first used shots alternate between the two clips that have them.
    assert {item.asset_id for item in tried[:2]} == {clips[0], clips[1]}


async def test_one_long_clip_cannot_fill_the_sample(cat: Catalogue) -> None:
    long = await cat.video("Course/Drone/DJI_LONG.MP4", _spans(40))
    short = [await cat.video(f"Course/Drone/DJI_{n}.MP4", _spans(3)) for n in range(3)]

    drawn = await cat.draw(cat.folder("Course", what="shots", size=10, seed=11))

    per_clip = {clip: sum(1 for i in drawn.items if i.asset_id == clip) for clip in [long, *short]}
    # One shot from each clip in turn: forty shots of one clip get no more
    # turns than three shots of another.
    assert sorted(per_clip.values()) == [2, 2, 3, 3]
    assert per_clip[long] <= 3
    # Shooting order, not draw order.
    assert [(i.relative_path, i.start_ms) for i in drawn.items] == sorted(
        (i.relative_path, i.start_ms) for i in drawn.items
    )


async def test_unused_shots_of_clips_somebody_tagged_come_before_the_rest(
    cat: Catalogue,
) -> None:
    """Items with tags a person decided are the ones a model's tags can be
    checked against, so they go first. One shot per clip in turn would take
    only 2 of the decided clip's 8 among five clips; all 8 are taken."""
    decided = await cat.video("Course/A/DJI_0001.MP4", _spans(8))
    for n in range(4):
        await cat.video(f"Course/B/DJI_{n + 2:04d}.MP4", _spans(6))
    await cat.rule(decided, "Barn", "confirmed")

    drawn = await cat.draw(cat.folder("Course", what="shots", size=10, seed=5))

    assert sum(1 for item in drawn.items if item.asset_id == decided) == 8


async def test_photographs_somebody_tagged_come_before_the_rest(cat: Catalogue) -> None:
    tagged = [await cat.photo(f"Course/Stills/IMG_{n:04d}.JPG") for n in range(4)]
    for n in range(4, 24):
        await cat.photo(f"Course/Stills/IMG_{n:04d}.JPG")
    await cat.rule(tagged[0], "Barn", "confirmed")
    await cat.rule(tagged[1], "Pond", "manual")
    await cat.rule(tagged[2], "Silo", "rejected")  # a no is decided too
    await cat.rule(tagged[3], "Dock", "confirmed")

    for seed in range(3):
        drawn = await cat.draw(cat.folder("Course", what="photos", size=10, seed=seed))
        assert set(tagged) <= {item.asset_id for item in drawn.items}, seed


async def test_the_same_seed_draws_the_same_sample(cat: Catalogue) -> None:
    for n in range(6):
        await cat.video(f"Course/Drone/DJI_{n:04d}.MP4", _spans(8))
        await cat.photo(f"Course/Stills/IMG_{n:04d}.JPG")

    first = await cat.draw(cat.folder("Course", size=12, seed=42))
    again = await cat.draw(cat.folder("Course", size=12, seed=42))
    other = await cat.draw(cat.folder("Course", size=12, seed=43))

    def picked(drawn: sampling.Drawn) -> list[tuple[uuid.UUID, int | None]]:
        return [(item.asset_id, item.start_ms) for item in drawn.items]

    assert picked(first) == picked(again)
    assert picked(first) != picked(other)
    # Half shots, half photographs.
    assert sum(1 for item in first.items if item.kind == "photo") == 6
    assert first.left_out == {"over": 6 + 48 - 12}
    assert sampling.left_out_sentence("over", 42, size=12) == (
        "42 more were not needed: the sample is 12."
    )


async def test_either_side_makes_up_the_others_shortfall(cat: Catalogue) -> None:
    await cat.video("Course/Drone/DJI_0001.MP4", _spans(30))
    await cat.photo("Course/Stills/IMG_0001.JPG")
    await cat.photo("Course/Stills/IMG_0002.JPG")

    drawn = await cat.draw(cat.folder("Course", size=20))

    assert sum(1 for item in drawn.items if item.kind == "photo") == 2
    assert sum(1 for item in drawn.items if item.kind == "shot") == 18


# --- pictures -----------------------------------------------------------------------


async def test_a_shot_with_no_picture_is_left_out_and_counted(cat: Catalogue) -> None:
    await cat.video("Course/Drone/DJI_0001.MP4", _spans(3))
    await cat.video("Course/Drone/DJI_0002.MP4", _spans(2), stills=False)
    await cat.photo("Course/Stills/IMG_0001.JPG", preview=False)
    await cat.photo("Course/Stills/IMG_0002.JPG", on_disk=False)

    drawn = await cat.draw(cat.folder("Course"))

    assert {item.filename for item in drawn.items} == {"DJI_0001.MP4"}
    assert drawn.left_out == {"no_picture": 4}
    assert sampling.left_out_sentence("no_picture", 4) == (
        "4 items have no picture on this machine yet."
    )
    # Three stills a shot, in the order they were filmed, at their moments.
    first = drawn.items[0]
    assert [ms for ms, _path in first.pictures] == [250, 2000, 3750]
    assert all(path.is_file() for _ms, path in first.pictures)

    one = await cat.draw(cat.folder("Course", stills=1))
    assert [ms for ms, _path in one.items[0].pictures] == [2000]


async def test_a_missing_still_falls_back_to_the_nearest_frame(cat: Catalogue) -> None:
    near = await cat.video("Course/Drone/DJI_0001.MP4", [(0, 4000)], stills=False)
    far = await cat.video("Course/Drone/DJI_0002.MP4", [(0, 4000)], stills=False)
    brief = await cat.video("Course/Drone/DJI_0003.MP4", [(0, 1200)], stills=False)
    async with cat.session() as db:
        for asset_id, ts_ms in ((near, 2400), (far, 9000), (brief, 600)):
            relative = f"frames/{asset_id}/{ts_ms:09d}.jpg"
            _jpeg(cat.data / relative)
            db.add(Frame(asset_id=asset_id, ts_ms=ts_ms, relative_path=relative))
        await db.commit()

    drawn = await cat.draw(cat.folder("Course"))

    assert [item.asset_id for item in drawn.items] == [near, brief]
    # The middle moment (2000) is 400 ms from the frame: that frame, said as
    # the moment it shows. The start and end are too far.
    assert [ms for ms, _path in drawn.items[0].pictures] == [2400]
    # All three moments of a short shot come to the one frame: shown once,
    # not as three pictures of the same file.
    assert [ms for ms, _path in drawn.items[1].pictures] == [600]
    assert drawn.left_out == {"no_picture": 1}


async def test_a_library_whose_stills_are_gone_is_not_searched_shot_by_shot(
    cat: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The draw is a preview a person waits on. When almost nothing has a
    picture (stills from before they were kept, a data folder not carried
    over), it stops after a bounded number of candidates rather than
    checking every shot in the library, and says what it did not look at."""
    monkeypatch.setattr(sampling, "MIN_TRIES", 12)
    monkeypatch.setattr(sampling, "TRIES_PER_ITEM", 1)
    for n in range(8):
        await cat.video(f"Course/Drone/DJI_{n:04d}.MP4", _spans(5), stills=False)
    real = sampling.shotlist.still_found
    looked: list[int] = []

    async def counting(*args: Any, **kwargs: Any) -> Any:
        looked.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(sampling.shotlist, "still_found", counting)

    with pytest.raises(SampleError) as nothing:
        await cat.draw(cat.folder("Course", what="shots", size=10))

    # Twelve shots looked at, three stills each; not all forty.
    assert len(looked) == 12 * 3
    assert str(nothing.value) == (
        "There is nothing to compare on here: 12 items have no picture on this machine yet. "
        "28 more were not looked at: too many of those tried first had no picture on this "
        "machine yet."
    )

    # With pictures among them, the sample keeps what it found and says the
    # rest were not looked at, not that they were not needed.
    # (One shot per clip in turn: the new clip's three are among the first 27.)
    await cat.video("Course/Drone/DJI_0100.MP4", _spans(3))
    monkeypatch.setattr(sampling, "MIN_TRIES", 30)
    drawn = await cat.draw(cat.folder("Course", what="shots", size=10))
    assert [item.filename for item in drawn.items] == ["DJI_0100.MP4"] * 3
    assert drawn.left_out == {"no_picture": 27, "unchecked": 43 - 30}


async def test_drawing_never_starts_a_process(
    cat: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview is a page that is only looking: a missing still is never
    grabbed from the proxy, however playable the proxy is."""
    from framefound.processing import scenes

    clip = await cat.video("Course/Drone/DJI_0001.MP4", _spans(2), stills=False)
    proxy = shot_files.proxy_path(cat.data, clip)
    proxy.parent.mkdir(parents=True, exist_ok=True)
    proxy.write_bytes(b"a playable copy is right there")
    await cat.video("Course/Drone/DJI_0002.MP4", _spans(2))

    def started(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("drawing a sample started a process")

    monkeypatch.setattr(scenes, "extract_frame", started)
    monkeypatch.setattr(subprocess, "run", started)
    monkeypatch.setattr(subprocess, "Popen", started)

    drawn = await cat.draw(cat.folder("Course"))

    assert {item.filename for item in drawn.items} == {"DJI_0002.MP4"}
    assert drawn.left_out == {"no_picture": 2}
    assert not (cat.data / "shots" / str(clip)[:2] / str(clip)).exists()


# --- listings and earlier comparisons ------------------------------------------


async def test_a_listing_keeps_its_order_and_leaves_out_video_frames(cat: Catalogue) -> None:
    photos = [await cat.photo(f"130 Davis Rd/IMG_{n:04d}.JPG") for n in range(24)]
    clip = await cat.video("130 Davis Rd/DJI_0001.MP4", _spans(2))
    waiting = await cat.photo("130 Davis Rd/IMG_9999.JPG", preview=False)
    async with cat.session() as db:
        listing = Listing(name="130 Davis Rd")
        db.add(listing)
        await db.flush()
        # The gallery order is not the order of the file names.
        gallery = list(reversed(photos))
        for position, asset_id in enumerate(gallery):
            db.add(ListingItem(listing_id=listing.id, asset_id=asset_id, position=position))
        db.add(ListingItem(listing_id=listing.id, asset_id=clip, position=30, frame_ms=1500))
        db.add(ListingItem(listing_id=listing.id, asset_id=waiting, position=31))
        await db.commit()
        listing_id = listing.id

    drawn = await cat.draw(SampleSpec(kind="listing", listing_id=listing_id, what="both"))

    assert drawn.spec.what == "photos"
    assert drawn.label == "The listing 130 Davis Rd"
    assert [item.asset_id for item in drawn.items] == gallery
    assert drawn.left_out == {"video_frames": 1, "no_picture": 1}

    thinned = await cat.draw(SampleSpec(kind="listing", listing_id=listing_id, size=10))
    picked = [gallery.index(item.asset_id) for item in thinned.items]
    # Evenly along the gallery, front and back both kept, in gallery order.
    assert picked == [0, 3, 5, 8, 10, 13, 15, 18, 20, 23]
    assert thinned.left_out == {"video_frames": 1, "no_picture": 1, "over": 14}


async def test_a_sample_of_an_earlier_comparison_keeps_its_items(cat: Catalogue) -> None:
    clip = await cat.video("Course/Drone/DJI_0001.MP4", _spans(3))
    still = await cat.photo("Course/Stills/IMG_0001.JPG")
    leaving = await cat.photo("Course/Stills/IMG_0002.JPG")
    async with cat.session() as db:
        earlier = Comparison(
            name="Gemma on Brookside",
            source_kind="folder",
            what="both",
            size=12,
            stills=1,
            prompt_version="look-1",
        )
        db.add(earlier)
        await db.flush()
        spans = [(clip, "shot", 8000, 12000), (still, "photo", None, None)]
        spans += [(leaving, "photo", None, None), (clip, "shot", 0, 4000)]
        # A span the clip's re-analysis no longer has keeps its facts as frozen.
        spans += [(clip, "shot", 1000, 3000)]
        for position, (asset_id, kind, start, end) in enumerate(spans):
            db.add(
                ComparisonItem(
                    comparison_id=earlier.id,
                    position=position,
                    kind=kind,
                    asset_id=asset_id,
                    start_ms=start,
                    end_ms=end,
                    facts={"move": "pan"} if start == 1000 else {},
                    facts_line="pan to the left." if start == 1000 else "",
                )
            )
        await db.commit()
        earlier_id = earlier.id
        await db.delete(await db.get(Asset, leaving))
        await db.commit()

    drawn = await cat.draw(SampleSpec(kind="comparison", comparison_id=earlier_id))

    assert drawn.label == "The same items as Gemma on Brookside"
    assert [(item.kind, item.start_ms) for item in drawn.items] == [
        ("shot", 8000),
        ("photo", None),
        ("shot", 0),
        ("shot", 1000),
    ]
    assert drawn.left_out == {"gone": 1}
    assert (drawn.spec.what, drawn.spec.size, drawn.spec.stills) == ("both", 12, 1)
    assert [len(item.pictures) for item in drawn.items] == [1, 1, 1, 1]
    assert drawn.items[0].facts_line.startswith("orbit to the right")
    assert drawn.items[3].facts_line == "pan to the left."

    shots = await cat.draw(
        SampleSpec(kind="comparison", comparison_id=earlier_id, what="shots", stills=3)
    )
    assert [item.kind for item in shots.items] == ["shot", "shot", "shot"]
    assert len(shots.items[0].pictures) == 3


# --- the truth ----------------------------------------------------------------------


async def test_truth_is_what_a_person_decided_not_what_was_suggested(cat: Catalogue) -> None:
    clip = await cat.video("Course/Drone/DJI_0001.MP4", _spans(2))
    photo = await cat.photo("Course/Stills/IMG_0001.JPG")
    await cat.rule(clip, "Drone", "manual")
    await cat.rule(clip, "Barn", "confirmed")
    await cat.rule(clip, "Pond", "rejected")
    await cat.rule(clip, "Silo", "suggested")
    await cat.rule(photo, "Pond", "confirmed")

    drawn = await cat.draw(cat.folder("Course"))

    shot = next(item for item in drawn.items if item.kind == "shot")
    assert shot.truth_yes == (("barn", "Barn"), ("drone", "Drone"))
    assert shot.truth_no == (("pond", "Pond"),)
    picture = next(item for item in drawn.items if item.kind == "photo")
    assert picture.truth_yes == (("pond", "Pond"),)
    assert picture.truth_no == ()
    assert picture.used is None
    # No cut was counted, so "used" is not known rather than false.
    assert shot.used is None
    # The vocabulary every model is told: confirmed words only, most used first.
    assert drawn.vocabulary == (("Barn", 1), ("Drone", 1), ("Pond", 1))
    # What a model is told about the shot says nothing of the truth or the file.
    assert shot.facts_line == prompt.facts_line(
        ShotWindow(
            start_ms=0,
            end_ms=4000,
            move="orbit",
            direction="right",
            speed=0.1,
            smooth=0.93,
            energy="low",
            light="golden",
            look={"sky": "overcast"},
        )
    )
    assert shot.facts == {
        "length_s": 4.0,
        "move": "orbit",
        "direction": "right",
        "smooth": 0.93,
        "energy": "low",
        "light": "golden",
        "sky": "overcast",
    }


async def test_a_draft_cut_cannot_be_ground_truth(cat: Catalogue) -> None:
    clip = await cat.video("Course/Drone/DJI_0001.MP4", _spans(4))
    draft = await cat.cut("Local draft", "draft", [(clip, 0, 8000)])
    finished = await cat.cut("Finished", "import", [(clip, 12000, 16000)])

    with pytest.raises(SampleError, match="Local draft is a draft, not finished work"):
        await cat.draw(SampleSpec(kind="cuts", cut_ids=(draft,)))
    with pytest.raises(SampleError, match="is a draft"):
        await cat.draw(cat.folder("Course", truth_cut_ids=(finished, draft)))

    # Left to itself, a folder counts every finished cut and no draft.
    drawn = await cat.draw(cat.folder("Course"))
    assert [cut[0] for cut in drawn.counted_cuts] == [finished]
    assert [item.start_ms for item in drawn.items if item.used] == [12000]
    # And none at all, when asked for none.
    none = await cat.draw(cat.folder("Course", truth_cut_ids=()))
    assert none.counted_cuts == ()
    assert {item.used for item in none.items} == {None}


# --- freezing -----------------------------------------------------------------------


async def test_the_pictures_are_copies_that_outlive_the_originals(cat: Catalogue) -> None:
    clip = await cat.video("Course/Drone/DJI_0001.MP4", _spans(1))
    await cat.photo("Course/Stills/IMG_0001.JPG")
    drawn = await cat.draw(cat.folder("Course"))
    comparison = uuid.uuid4()

    frozen = sampling.freeze(comparison, drawn.items)

    assert frozen == [tuple(ms for ms, _p in item.pictures) for item in drawn.items]
    shot = next(i for i, item in enumerate(drawn.items) if item.kind == "shot")
    photo = next(i for i, item in enumerate(drawn.items) if item.kind == "photo")
    # The stills and the preview go; the comparison's copies stay.
    for _ms, path in drawn.items[shot].pictures + drawn.items[photo].pictures:
        path.unlink()
    assert not (cat.data / "shots" / str(clip)[:2] / str(clip) / "000000250.jpeg").exists()

    pictures = sampling.read_pictures(comparison, shot, 3)
    assert len(pictures) == 3
    big = sampling.read_pictures(comparison, photo, 1)
    with Image.open(sampling.picture_path(comparison, photo, 0)) as image:
        # Sent as the Claude reviewer's previews are: 768 px at most.
        assert max(image.size) == 768
    assert big[0][:2] == b"\xff\xd8"
    assert sampling.picture_path(comparison, shot, 2).name == f"{shot:04d}-2.jpg"
    # An item asked for more pictures than it has is shown none, never part.
    assert sampling.read_pictures(comparison, shot, 4) == []
    assert sampling.read_pictures(uuid.uuid4(), shot, 3) == []
    assert not list(sampling.pictures_dir(comparison).glob("*.partial"))


async def test_a_picture_that_cannot_be_read_is_skipped_when_frozen(cat: Catalogue) -> None:
    clip = await cat.video("Course/Drone/DJI_0001.MP4", _spans(1))
    (cat.data / shot_files.still_relpath(clip, 2000)).write_bytes(b"\xff\xd8 half a jpeg")
    drawn = await cat.draw(cat.folder("Course"))
    comparison = uuid.uuid4()

    frozen = sampling.freeze(comparison, drawn.items)

    assert frozen == [(250, 3750)]
    assert len(sampling.read_pictures(comparison, 0, 2)) == 2
    assert not sampling.picture_path(comparison, 0, 2).exists()


async def test_freezing_on_a_full_disk_writes_nothing_and_says_so_in_its_own_words(
    cat: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.processing import derivatives as deriv

    await cat.video("Course/Drone/DJI_0001.MP4", _spans(1))
    drawn = await cat.draw(cat.folder("Course"))
    settings = get_settings().model_copy(update={"min_free_gb": 5.0})

    def full(_data_dir: Path) -> None:
        raise deriv.OutOfSpace("Only 0.4 GB free for previews; generation pauses below 5 GB")

    monkeypatch.setattr(deriv, "ensure_space", full)
    comparison = uuid.uuid4()

    with pytest.raises(SampleError) as refused:
        sampling.freeze(comparison, drawn.items, settings)

    assert str(refused.value) == (
        "There is not enough free space on this machine to keep the comparison's pictures: "
        "nothing is written once less than 5 GB is free. Make some room and try again."
    )
    assert not sampling.pictures_dir(comparison).exists()


async def test_a_freeze_stopped_part_way_takes_away_what_it_wrote(
    cat: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk filling on the second item: the error is passed on, and no
    half a comparison folder is left for the next step to find."""
    await cat.video("Course/Drone/DJI_0001.MP4", _spans(1))
    await cat.video("Course/Drone/DJI_0002.MP4", _spans(1))
    drawn = await cat.draw(cat.folder("Course"))
    comparison = uuid.uuid4()
    real = os.replace
    written: list[str] = []

    def filling(source: Any, target: Any) -> None:
        written.append(str(target))
        if len(written) == 4:  # the second item's first picture
            raise OSError(28, "No space left on device")
        real(source, target)

    monkeypatch.setattr(os, "replace", filling)

    with pytest.raises(OSError, match="No space left"):
        sampling.freeze(comparison, drawn.items)

    assert len(written) == 4
    assert not sampling.pictures_dir(comparison).exists()


async def test_dropping_pictures_never_touches_another_comparison(cat: Catalogue) -> None:
    await cat.video("Course/Drone/DJI_0001.MP4", _spans(2))
    drawn = await cat.draw(cat.folder("Course"))
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    sampling.freeze(mine, drawn.items)
    sampling.freeze(theirs, drawn.items)
    left = sampling.pictures_dir(mine) / "notes.txt"
    left.write_text("not a picture")
    (sampling.pictures_dir(mine) / "0000-0.jpg.partial").write_bytes(b"half")

    sampling.drop_pictures(mine)

    assert sorted(p.name for p in sampling.pictures_dir(mine).iterdir()) == ["notes.txt"]
    assert len(list(sampling.pictures_dir(theirs).iterdir())) == 6
    left.unlink()
    sampling.drop_pictures(mine)
    assert not sampling.pictures_dir(mine).exists()
    sampling.drop_pictures(mine)  # a second time is nothing
    assert sampling.pictures_dir(theirs).exists()
    with pytest.raises(ValueError):
        sampling.pictures_dir("../../etc")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        sampling.picture_path(mine, -1, 0)


async def test_a_preview_writes_nothing(cat: Catalogue) -> None:
    clip = await cat.video("Course/Drone/DJI_0001.MP4", _spans(4))
    await cat.video("Course/Drone/DJI_0002.MP4", _spans(2), stills=False)
    await cat.photo("Course/Stills/IMG_0001.JPG")
    await cat.rule(clip, "Barn", "confirmed")
    finished = await cat.cut("Finished", "import", [(clip, 0, 4000)])

    def files() -> dict[str, float]:
        return {str(p): p.stat().st_mtime for p in cat.data.rglob("*")}

    async def rows() -> dict[str, int]:
        async with cat.session() as db:
            return {
                table.name: int(
                    (await db.execute(select(func.count()).select_from(table))).scalar_one()
                )
                for table in Base.metadata.sorted_tables
            }

    before_files, before_rows = files(), await rows()

    await cat.draw(cat.folder("Course"))
    await cat.draw(SampleSpec(kind="cuts", cut_ids=(finished,)))
    with pytest.raises(SampleError):
        await cat.draw(cat.folder("Nowhere"))

    assert files() == before_files
    assert await rows() == before_rows


def test_every_reason_reads_as_a_sentence() -> None:
    assert sampling.left_out_sentence("no_shots", 1) == (
        "1 clip has no shot analysis yet, so it has no shots to show."
    )
    assert sampling.left_out_sentence("no_shots", 3) == (
        "3 clips have no shot analysis yet, so they have no shots to show."
    )
    assert sampling.left_out_sentence("short", 1) == "1 shot is under a second long."
    assert sampling.left_out_sentence("video_frames", 2) == (
        "2 listing items are frames of a video."
    )
    assert sampling.left_out_sentence("not_photos", 1) == "1 listing item is not a photograph."
    assert sampling.left_out_sentence("gone", 1) == (
        "1 item of the earlier comparison is no longer in the catalogue."
    )
    assert sampling.left_out_sentence("over", 1, size=60) == (
        "1 more was not needed: the sample is 60."
    )
    assert sampling.left_out_sentence("no_picture", 1_200) == (
        "1,200 items have no picture on this machine yet."
    )
