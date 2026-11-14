"""What is in a scope and what still needs looking at
(framefound/looking/scope.py).

Each test builds a small catalogue on file-backed SQLite: two libraries,
photographs and clips with the derived files the pipeline would have made
(rows only — nothing here needs a picture on disk), shot windows, and looks
written straight into the ledger as looking/store.py will write them.
Nothing here runs ffmpeg, a model or a queue.
"""

import itertools
import os
import pathlib
import re
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Derivative, Library, Look, LookRequest, ShotWindow
from framefound.looking import INGEST_TRIES, scope, store
from framefound.looking.scope import Scope
from framefound.media.folders import FolderError

BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
KEY = "google/gemma-4-e4b-it@abc123#look-1"
OTHER_KEY = "allenai/molmo-7b@def456#look-1"
INGEST = scope.Run(request_id=None)


@dataclass
class Catalogue:
    factory: Any
    engine: Any
    library: uuid.UUID
    other_library: uuid.UUID
    made: int = 0

    def session(self) -> AsyncSession:
        session: AsyncSession = self.factory()
        return session

    async def asset(
        self,
        relative_path: str,
        media_type: str,
        *,
        derived: tuple[str, ...] = (),
        library: uuid.UUID | None = None,
        availability: str = "online",
        indexed: datetime = BASE,
        duration_s: float | None = None,
        proxy_of: uuid.UUID | None = None,
        looked_by: str | None = None,
    ) -> uuid.UUID:
        """An asset with these derived files ready (rows only)."""
        self.made += 1
        async with self.session() as db:
            asset = Asset(
                library_id=library or self.library,
                relative_path=relative_path,
                filename=relative_path.rsplit("/", 1)[-1],
                extension=relative_path.rsplit(".", 1)[-1].lower(),
                media_type=media_type,
                size_bytes=1000,
                mtime=BASE,
                availability=availability,
                first_indexed_at=indexed,
                duration_s=duration_s,
                proxy_of_id=proxy_of,
                looked_at=BASE if looked_by else None,
                looked_by=looked_by,
                look_score=70 if looked_by else None,
            )
            db.add(asset)
            await db.flush()
            for kind in derived:
                db.add(
                    Derivative(
                        asset_id=asset.id,
                        kind=kind,
                        relative_path=f"derivatives/{str(asset.id)[:2]}/{asset.id}/{kind}.jpeg",
                        media_format="jpeg",
                        status="ready",
                    )
                )
            await db.commit()
            return asset.id

    async def photo(self, relative_path: str, **more: Any) -> uuid.UUID:
        more.setdefault("derived", ("thumbnail", "preview"))
        return await self.asset(relative_path, "image", **more)

    async def clip(
        self,
        relative_path: str,
        spans: Sequence[tuple[int, int]] = (),
        **more: Any,
    ) -> uuid.UUID:
        """A clip whose shots were found (motion, proxy, poster) unless
        `derived` says otherwise, with these windows."""
        more.setdefault("derived", ("poster", "thumbnail", "proxy", "motion"))
        more.setdefault("duration_s", 30.0)
        asset_id = await self.asset(relative_path, "video", **more)
        await self.windows(asset_id, spans)
        return asset_id

    async def windows(self, asset_id: uuid.UUID, spans: Sequence[tuple[int, int]]) -> None:
        """The clip's windows, rebuilt whole as a re-analysis does."""
        async with self.session() as db:
            for window in (
                await db.execute(select(ShotWindow).where(ShotWindow.asset_id == asset_id))
            ).scalars():
                await db.delete(window)
            await db.flush()
            for idx, (start, end) in enumerate(spans):
                db.add(
                    ShotWindow(
                        asset_id=asset_id,
                        idx=idx,
                        start_ms=start,
                        end_ms=end,
                        move="orbit",
                        energy="low",
                    )
                )
            await db.commit()

    async def look(
        self,
        asset_id: uuid.UUID,
        span: tuple[int | None, int | None] = (None, None),
        *,
        ok: bool = True,
        key: str = KEY,
        request_id: uuid.UUID | None = None,
        current: bool | None = None,
    ) -> None:
        async with self.session() as db:
            db.add(
                Look(
                    asset_id=asset_id,
                    start_ms=span[0],
                    end_ms=span[1],
                    ok=ok,
                    current=ok if current is None else current,
                    key=key,
                    score=70 if ok else None,
                    request_id=request_id,
                )
            )
            await db.commit()

    async def review(self) -> uuid.UUID:
        """A review somebody asked for, for looks to belong to."""
        async with self.session() as db:
            request = LookRequest(library_id=self.library)
            db.add(request)
            await db.commit()
            return request.id

    async def roll_up(self, asset_id: uuid.UUID, key: str | None = KEY) -> None:
        """What store.roll_up will write once nothing in the asset is left."""
        async with self.session() as db:
            await db.execute(
                update(Asset)
                .where(Asset.id == asset_id)
                .values(looked_at=BASE if key else None, looked_by=key, look_score=70)
            )
            await db.commit()

    async def count(
        self,
        where: Scope | None = None,
        *,
        redo: bool = False,
        run: scope.Run | None = None,
    ) -> scope.Counts:
        async with self.session() as db:
            return await scope.count(db, where or Scope(self.library), key=KEY, redo=redo, run=run)

    async def remaining(self, run: scope.Run, *, redo: bool = False) -> int:
        async with self.session() as db:
            return await scope.remaining(db, Scope(self.library), key=KEY, redo=redo, run=run)

    async def items(
        self,
        asset_id: uuid.UUID,
        *,
        redo: bool = False,
        request_id: uuid.UUID | None = None,
        stills_fallback: bool = False,
        key: str = KEY,
    ) -> list[tuple[int | None, int | None]]:
        async with self.session() as db:
            asset = await db.get(Asset, asset_id)
            assert asset is not None
            found = await scope.items_for(
                db,
                asset,
                key=key,
                redo=redo,
                request_id=request_id,
                stills_fallback=stills_fallback,
            )
            return [item.span for item in found]

    async def walk(
        self,
        where: Scope | None = None,
        *,
        redo: bool = False,
        limit: int = 20,
        run: scope.Run = INGEST,
    ) -> list[str]:
        """Every path a pass would visit, page by page."""
        return [
            asset.relative_path
            for asset in await self.pass_over(where, redo=redo, limit=limit, run=run)
        ]

    async def pass_over(
        self,
        where: Scope | None = None,
        *,
        redo: bool = False,
        limit: int = 20,
        run: scope.Run = INGEST,
    ) -> list[Asset]:
        """Every asset a pass would visit, page by page."""
        seen: list[Asset] = []
        after = ""
        async with self.session() as db:
            while True:
                page = await scope.next_assets(
                    db,
                    where or Scope(self.library),
                    key=KEY,
                    redo=redo,
                    run=run,
                    after=after,
                    limit=limit,
                )
                if not page:
                    return seen
                seen += page
                after = page[-1].relative_path


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
async def cat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Catalogue]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'scope.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "scope-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
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
    yield Catalogue(factory, engine, gelco.id, intel.id)
    await engine.dispose()


# --- photographs ---------------------------------------------------------------------


async def test_a_photograph_already_looked_at_is_not_asked_again(cat: Catalogue) -> None:
    done = await cat.photo("Brookside/2026/front.jpg")
    await cat.look(done)
    await cat.roll_up(done)
    new = await cat.photo("Brookside/2026/back.jpg")

    counts = await cat.count()
    assert (counts.photos, counts.already, counts.by_other) == (1, 1, 0)
    assert await cat.items(done) == []
    assert await cat.items(new) == [(None, None)]
    assert await cat.walk() == ["Brookside/2026/back.jpg"]


async def test_a_photograph_with_a_look_is_not_offered_even_before_its_roll_up(
    cat: Catalogue,
) -> None:
    # The ledger decides per item: a look written a moment before the roll-up
    # must not be asked again by a pass that catches the asset in between.
    photo = await cat.photo("Brookside/front.jpg")
    await cat.look(photo)

    assert await cat.items(photo) == []
    assert (await cat.count()).photos == 0


async def test_a_photograph_with_nothing_to_show_yet_waits(cat: Catalogue) -> None:
    waiting = await cat.photo("Brookside/raw.cr3", derived=())
    small = await cat.photo("Brookside/small.jpg", derived=("thumbnail",))

    counts = await cat.count()
    assert (counts.photos, counts.not_ready) == (1, 1)
    assert await cat.items(waiting) == []
    assert await cat.items(small) == [(None, None)]


async def test_redo_asks_again_only_what_another_model_looked_at(cat: Catalogue) -> None:
    mine = await cat.photo("a/mine.jpg")
    await cat.look(mine)
    await cat.roll_up(mine)
    theirs = await cat.photo("a/theirs.jpg")
    await cat.look(theirs, key=OTHER_KEY)
    await cat.roll_up(theirs, OTHER_KEY)
    mixed = await cat.clip("a/mixed.mp4", [(0, 4000), (4000, 9000)])
    await cat.look(mixed, (0, 4000))
    await cat.look(mixed, (4000, 9000), key=OTHER_KEY)
    await cat.roll_up(mixed, "mixed")

    plain = await cat.count()
    # Counted in items: two photographs and the clip's two shots.
    assert (plain.photos, plain.shots, plain.already, plain.by_other) == (0, 0, 4, 2)
    assert await cat.walk() == []

    again = await cat.count(redo=True)
    assert (again.photos, again.clips, again.shots) == (1, 1, 1)
    # What ticking redo adds stays the same number once it is ticked.
    assert (again.already, again.by_other) == (2, 2)
    assert await cat.walk(redo=True) == ["a/mixed.mp4", "a/theirs.jpg"]
    assert await cat.items(mine, redo=True) == []
    assert await cat.items(theirs, redo=True) == [(None, None)]
    # Only the shot another build looked at is asked again.
    assert await cat.items(mixed, redo=True) == [(4000, 9000)]


# --- clips and shots ----------------------------------------------------------------


async def test_a_clip_is_its_shots(cat: Catalogue) -> None:
    clip = await cat.clip("Pete Dye/drone.mp4", [(0, 4000), (4000, 9000), (9000, 15000)])
    await cat.look(clip, (4000, 9000))

    assert await cat.items(clip) == [(0, 4000), (9000, 15000)]
    async with cat.session() as db:
        asset = await db.get(Asset, clip)
        assert asset is not None
        found = await scope.items_for(
            db, asset, key=KEY, redo=False, request_id=None, stills_fallback=False
        )
    assert [item.window.idx for item in found if item.window] == [0, 2]
    assert [item.middle_ms for item in found] == [2000, 12000]
    assert {item.filename for item in found} == {"drone.mp4"}

    counts = await cat.count()
    # Half done: the shot looked at needs nothing, the other two are to do.
    assert (counts.clips, counts.shots, counts.already) == (1, 2, 1)


async def test_a_shot_keeps_its_look_when_the_clip_is_reanalysed_to_the_same_times(
    cat: Catalogue,
) -> None:
    clip = await cat.clip("Pete Dye/drone.mp4", [(0, 4000), (4000, 9000)])
    await cat.look(clip, (0, 4000))
    await cat.look(clip, (4000, 9000))

    await cat.windows(clip, [(0, 4000), (4000, 9000)])  # new rows, new ids, same times

    assert await cat.items(clip) == []
    assert (await cat.count()).shots == 0


async def test_a_look_that_is_no_longer_current_does_not_count(cat: Catalogue) -> None:
    # A re-analysis that lost a shot marks its look not current; when a later
    # one finds the shot again, that old look is history, not an answer.
    clip = await cat.clip("Pete Dye/drone.mp4", [(0, 4000), (4000, 9000)])
    await cat.look(clip, (0, 4000))
    await cat.look(clip, (4000, 9000), current=False)
    photo = await cat.photo("Pete Dye/front.jpg")
    await cat.look(photo, current=False)

    assert await cat.items(clip) == [(4000, 9000)]
    assert await cat.items(photo) == [(None, None)]
    counts = await cat.count()
    assert (counts.photos, counts.clips, counts.shots) == (1, 1, 1)


async def test_a_shot_whose_times_changed_needs_looking_at_again(cat: Catalogue) -> None:
    clip = await cat.clip("Pete Dye/drone.mp4", [(0, 4000), (4000, 9000)])
    await cat.look(clip, (0, 4000))
    await cat.look(clip, (4000, 9000))

    await cat.windows(clip, [(0, 4000), (4000, 6500), (6500, 9000)])

    assert await cat.items(clip) == [(4000, 6500), (6500, 9000)]
    counts = await cat.count()
    assert (counts.clips, counts.shots) == (1, 2)


async def test_a_clip_without_a_playable_copy_is_counted_as_needing_one(cat: Catalogue) -> None:
    copy = await cat.clip("Bay/a.mp4", derived=("poster", "thumbnail"), duration_s=42.5)
    await cat.clip("Bay/b.mov", derived=("poster", "thumbnail"), duration_s=7.5)
    shots = await cat.clip("Bay/c.mp4", derived=("poster", "thumbnail", "proxy"))
    waiting = await cat.clip("Bay/d.mp4", derived=())

    counts = await cat.count()
    assert (counts.need_copy, counts.need_copy_footage_s) == (2, 50.0)
    assert (counts.need_shots, counts.not_ready) == (1, 1)
    assert (counts.clips, counts.shots, counts.stills_only, counts.items) == (0, 0, 0, 0)
    for clip in (copy, shots, waiting):
        assert await cat.items(clip) == []

    async with cat.session() as db:
        need_copy, need_shots = await scope.not_ready_clips(db, Scope(cat.library))
    assert need_copy == [copy, (await _id(cat, "Bay/b.mov"))]
    assert need_shots == [shots]


async def test_a_clip_whose_original_is_away_is_not_sent_to_be_copied(cat: Catalogue) -> None:
    # A playable copy is made from the original. With its share asleep the
    # copy job would only be skipped, and a review would sit at "Getting
    # clips ready" for hours waiting for it.
    here = await cat.clip("Bay/here.mp4", derived=("poster", "thumbnail"), duration_s=40.0)
    away = await cat.clip(
        "Bay/away.mp4", derived=("poster", "thumbnail"), duration_s=600.0, availability="unmounted"
    )
    # Its shots are found from the copy, which is on this machine: that still works.
    copied = await cat.clip("Bay/copied.mp4", derived=("poster", "proxy"), availability="unmounted")

    counts = await cat.count()
    assert (counts.need_copy, counts.need_copy_footage_s, counts.copy_away) == (1, 40.0, 1)
    assert counts.need_shots == 1
    async with cat.session() as db:
        assert await scope.not_ready_clips(db, Scope(cat.library)) == ([here], [copied])
    # Judged from its poster with the rest that never became ready.
    assert await cat.items(away, stills_fallback=True) == [(None, None)]
    assert await cat.remaining(scope.Run(uuid.uuid4(), stills_fallback=True)) == 3


async def test_already_and_what_redo_adds_are_counted_in_items(cat: Catalogue) -> None:
    # The dialog says "Already looked at — N items" beside the shots to do,
    # and offers redo for what another model looked at: a clip of twenty
    # shots is twenty items, not one.
    spans = [(n * 1000, (n + 1) * 1000) for n in range(20)]
    for name in ("a.mp4", "b.mp4"):
        clip = await cat.clip(f"Bay/{name}", spans)
        for span in spans:
            await cat.look(clip, span, key=OTHER_KEY)
        await cat.roll_up(clip, OTHER_KEY)
    # Partly looked at: no roll-up yet, two of its three shots done elsewhere.
    part = await cat.clip("Bay/part.mp4", [(0, 1000), (1000, 2000), (2000, 3000)])
    await cat.look(part, (0, 1000), key=OTHER_KEY)
    await cat.look(part, (1000, 2000), key=OTHER_KEY)
    raw = await cat.clip("Bay/raw.braw", derived=("poster",))
    await cat.look(raw)
    await cat.roll_up(raw)

    plain = await cat.count()
    assert (plain.shots, plain.already, plain.by_other) == (1, 43, 42)

    again = await cat.count(redo=True)
    assert again.shots == 43  # what ticking redo added: the 42
    assert (again.already, again.by_other) == (1, 42)
    # By kind: two whole clips and the part one by their shots, the raw clip
    # judged whole (a clip with no shots).
    assert (plain.already_photos, plain.already_clips, plain.already_shots) == (0, 4, 42)
    assert (plain.other_photos, plain.other_clips, plain.other_shots) == (0, 3, 42)


async def test_what_was_already_looked_at_is_said_by_kind_never_above_the_files(
    cat: Catalogue,
) -> None:
    # Pete Dye: 8 files, 6 photographs and 2 clips of 3 shots each. Once all
    # is looked at, "already" in items is 12, which the dialog said as
    # "12 items" beside a folder list that says "8 items". By kind it is the
    # folder's own 6 photographs and 2 clips, with the clips' 6 shots.
    for n in range(6):
        photo = await cat.photo(f"Pete Dye/{n}.jpg")
        await cat.look(photo)
        await cat.roll_up(photo)
    spans = [(0, 1000), (1000, 2000), (2000, 3000)]
    for name in ("a.mp4", "b.mp4"):
        clip = await cat.clip(f"Pete Dye/{name}", spans)
        for span in spans:
            await cat.look(clip, span)
        await cat.roll_up(clip)

    done = await cat.count()
    assert (done.items, done.already) == (0, 12)
    assert (done.already_photos, done.already_clips, done.already_shots) == (6, 2, 6)
    assert done.already_photos + done.already_clips == 8
    assert (done.other_photos, done.other_clips, done.other_shots) == (0, 0, 0)

    # A clip half looked at is in both rows: one shot done, two to do.
    half = await cat.clip("Pete Dye/c.mp4", spans)
    await cat.look(half, (0, 1000))
    counts = await cat.count()
    assert (counts.clips, counts.shots) == (1, 2)
    assert (counts.already_clips, counts.already_shots, counts.already) == (3, 7, 13)


async def test_clips_that_never_became_ready_are_judged_from_one_still_when_asked(
    cat: Catalogue,
) -> None:
    copy = await cat.clip("Bay/a.mp4", derived=("poster", "thumbnail"))
    shots = await cat.clip("Bay/b.mp4", derived=("poster", "proxy"))
    waiting = await cat.clip("Bay/c.mp4", derived=())

    for clip in (copy, shots):
        assert await cat.items(clip, stills_fallback=True) == [(None, None)]
    # Nothing to show it from: no still to be judged from either.
    assert await cat.items(waiting, stills_fallback=True) == []


async def test_a_camera_raw_clip_is_judged_from_one_still(cat: Catalogue) -> None:
    raw = await cat.clip("Brookside/A001.braw", derived=("poster", "thumbnail"))
    no_poster = await cat.clip("Brookside/A002.braw", derived=())
    # The thumbnail made from the poster is a still of the whole clip too.
    thumbnail = await cat.clip("Brookside/A003.braw", derived=("thumbnail",))

    counts = await cat.count()
    assert (counts.stills_only, counts.need_copy, counts.not_ready, counts.items) == (2, 0, 1, 2)
    assert await cat.items(raw) == [(None, None)]
    assert await cat.items(thumbnail) == [(None, None)]
    assert await cat.items(no_poster) == []
    async with cat.session() as db:
        # It can never have a copy here, so making one is never asked for.
        assert await scope.not_ready_clips(db, Scope(cat.library)) == ([], [])

    await cat.look(raw)
    await cat.roll_up(raw)
    assert await cat.items(raw) == []
    assert (await cat.count()).stills_only == 1


async def test_a_clip_whose_analysis_found_no_shots_is_judged_from_its_poster(
    cat: Catalogue,
) -> None:
    clip = await cat.clip("Bay/blink.mp4", [])

    assert await cat.items(clip) == [(None, None)]
    counts = await cat.count()
    assert (counts.stills_only, counts.clips, counts.shots) == (1, 0, 0)


async def test_the_state_of_a_clip_is_the_same_rule_in_python_and_in_sql(
    cat: Catalogue,
) -> None:
    # Every combination of what a clip may have, counted by SQL and judged in
    # Python; the two must agree or the dialog's counts lie about the run.
    # The poster and the thumbnail made from it are separate: either one is
    # a still of the whole clip.
    expected: dict[str, int] = {}
    judged: dict[str, scope.ClipState] = {}
    for n, (motion, proxy, poster, thumbnail, raw, windows) in enumerate(
        itertools.product((False, True), repeat=6)
    ):
        derived = tuple(
            kind
            for kind, has in (
                ("motion", motion),
                ("proxy", proxy),
                ("poster", poster),
                ("thumbnail", thumbnail),
            )
            if has
        )
        path = f"all/{n:02d}.{'braw' if raw else 'mp4'}"
        clip = await cat.clip(path, [(0, 4000)] if windows else [], derived=derived)
        state = scope.clip_state(
            has_motion=motion,
            has_proxy=proxy,
            has_poster=poster or thumbnail,
            extension="BRAW" if raw else "mp4",
            has_windows=windows,
        )
        judged[path] = state
        # The dialog counts a clip it would look at now by its shots when it
        # has windows (camera raw with windows too), else by its state.
        counted_as = "shots" if windows and state in scope.judged_now(False) else state
        expected[counted_as] = expected.get(counted_as, 0) + 1
        # And what the last pass is handed agrees with the state: a still of
        # the whole clip is enough to be judged from, but a clip with windows
        # is always its shots — a look of its whole would never count.
        items = await cat.items(clip, stills_fallback=True)
        if state == "not_ready":
            assert items == [], path
        elif windows:
            assert items == [(0, 4000)], path
        else:
            assert items == [(None, None)], path

    async with cat.session() as db:
        rows = await db.execute(select(Asset.relative_path, scope._state_sql()))
        in_sql = {str(path): state for path, state in rows.all()}
    assert in_sql == judged

    counts = await cat.count()
    assert counts.clips == expected["shots"]
    assert counts.stills_only == expected["stills_only"]
    assert counts.need_shots == expected["need_shots"]
    assert counts.need_copy == expected["need_copy"]
    assert counts.not_ready == expected["not_ready"]


async def test_whatever_a_clip_has_what_is_handed_out_is_what_a_look_counts_for(
    cat: Catalogue,
) -> None:
    # The rule that hands items out (`items_for`) and the rule that decides
    # whether a look counts (`store._is_item`) must agree for every clip, or
    # an answer is kept, does not count, and the same item is handed out
    # again for ever. It happened to clips with shot windows and no ready
    # analysis file: handed out whole in the last pass, a whole look that a
    # clip with windows never counts, and a review at "20 of 8".
    review = await cat.review()
    for n, (motion, proxy, poster, raw, windows) in enumerate(
        itertools.product((None, "ready", "failed", "pending"), *[(False, True)] * 4)
    ):
        state = scope.clip_state(
            has_motion=motion == "ready",
            has_proxy=proxy,
            has_poster=poster,
            extension="braw" if raw else "mp4",
            has_windows=windows,
        )
        asked: list[tuple[int | None, int | None]] = []
        derived = tuple(kind for kind, has in (("proxy", proxy), ("poster", poster)) if has)
        clip = await cat.clip(
            f"all/{n:03d}.{'braw' if raw else 'mp4'}",
            [(0, 4000), (4000, 9000)] if windows else [],
            derived=derived,
        )
        if motion is not None:
            async with cat.session() as db:
                db.add(
                    Derivative(
                        asset_id=clip,
                        kind="motion",
                        relative_path=f"derivatives/{clip}/motion.json",
                        media_format="json",
                        status=motion,
                    )
                )
                await db.commit()
        for fallback in (False, True):
            run = scope.Run(review, stills_fallback=fallback)
            async with cat.session() as db:
                asset = await db.get(Asset, clip)
                assert asset is not None
                handed = await scope.items_for(
                    db, asset, key=KEY, redo=False, request_id=review, stills_fallback=fallback
                )
                # The SQL that decides whether a pass walks to the clip says
                # the same, before anything is answered (after, the roll-up
                # alone would hide it).
                offered = await db.scalar(
                    select(Asset.id).where(Asset.id == clip, scope.has_items(KEY, False, run))
                )
                assert (offered is not None) == bool(handed), (n, fallback)
                asked += [item.span for item in handed]
                # Every item handed out is one a look of it would count for.
                assert [await store._is_item(db, item) for item in handed] == [True] * len(
                    handed
                ), (n, fallback)
                for item in handed:
                    db.add(
                        Look(
                            asset_id=clip,
                            start_ms=item.start_ms,
                            end_ms=item.end_ms,
                            ok=True,
                            current=await store._is_item(db, item),
                            key=KEY,
                            score=70,
                            request_id=review,
                        )
                    )
                await store.roll_up(db, clip)
                await db.commit()
            # Once each is answered, nothing in it is handed out again, and
            # the SQL that decides whether to walk to it agrees.
            assert await cat.items(clip, request_id=review, stills_fallback=fallback) == [], n
            async with cat.session() as db:
                walked = await db.scalar(
                    select(Asset.id).where(
                        Asset.id == clip,
                        scope.needs_work(KEY, False),
                        scope.has_items(KEY, False, run),
                    )
                )
            assert walked is None, (n, fallback)
        # A clip with windows is its shots, each asked once, by the last pass
        # at the latest, whatever its analysis file says — unless it has
        # nothing to show yet. Never one item of the whole.
        if windows and state != "not_ready":
            assert asked == [(0, 4000), (4000, 9000)], n
        elif windows:
            assert asked == [], n


# --- where and when -----------------------------------------------------------------


async def test_a_folder_holds_what_is_beneath_it_and_nothing_beside_it(cat: Catalogue) -> None:
    await cat.photo("Brookside/2026/a.jpg")
    await cat.photo("Brookside/2026/Rev 2/b.jpg")
    await cat.photo("Brookside/2026-old/c.jpg")
    await cat.photo("brookside/2026/d.jpg")
    await cat.photo("Brookside/2026/e.jpg", library=cat.other_library)
    original = await cat.clip("Brookside/2026/f.mp4", [(0, 4000)])
    await cat.clip("Brookside/2026/Proxies/f_Proxy.mp4", [(0, 4000)], proxy_of=original)

    folder = Scope(cat.library, "/Brookside/2026/")
    assert folder.folder == "Brookside/2026"
    counts = await cat.count(folder)
    assert (counts.photos, counts.clips, counts.shots) == (2, 1, 1)
    assert await cat.walk(folder) == [
        "Brookside/2026/Rev 2/b.jpg",
        "Brookside/2026/a.jpg",
        "Brookside/2026/f.mp4",
    ]
    whole = await cat.count()
    assert whole.photos == 4  # the other library's photograph is never in it


async def test_a_folder_path_that_points_elsewhere_is_refused(cat: Catalogue) -> None:
    for bad in ("Brookside/../Intel", "a//b", "./a"):
        with pytest.raises(FolderError):
            Scope(cat.library, bad)


async def test_a_missing_file_is_left_out_and_an_unmounted_one_is_not(cat: Catalogue) -> None:
    gone = await cat.photo("a/gone.jpg", availability="missing")
    away = await cat.photo("a/away.jpg", availability="unmounted")
    clip_away = await cat.clip("a/away.mp4", [(0, 4000)], availability="unmounted")

    counts = await cat.count()
    assert (counts.photos, counts.shots, counts.missing) == (1, 1, 1)
    assert await cat.items(gone) == []
    assert await cat.items(away) == [(None, None)]
    assert await cat.items(clip_away) == [(0, 4000)]
    assert await cat.walk() == ["a/away.jpg", "a/away.mp4"]


async def test_sound_files_are_counted_and_never_looked_at(cat: Catalogue) -> None:
    song = await cat.asset("a/voice.wav", "audio", derived=("waveform",))

    counts = await cat.count()
    assert (counts.audio, counts.items, counts.already) == (1, 0, 0)
    assert await cat.items(song) == []
    assert await cat.walk() == []


async def test_ingest_only_sees_what_came_in_after_the_switch(cat: Catalogue) -> None:
    switched_on = BASE + timedelta(days=30)
    await cat.photo("Card/old.jpg", indexed=switched_on - timedelta(minutes=1))
    await cat.photo("Card/new.jpg", indexed=switched_on + timedelta(minutes=5))
    await cat.clip("Card/new.mp4", [(0, 4000)], indexed=switched_on + timedelta(minutes=6))

    since = Scope(cat.library, since=switched_on)
    counts = await cat.count(since)
    assert (counts.photos, counts.shots) == (1, 1)
    assert await cat.walk(since) == ["Card/new.jpg", "Card/new.mp4"]
    assert (await cat.count()).photos == 2


# --- failures ----------------------------------------------------------------------


async def test_an_item_that_failed_in_this_review_is_not_offered_to_it_again(
    cat: Catalogue,
) -> None:
    this_review, next_review = await cat.review(), await cat.review()
    photo = await cat.photo("a/blurry.jpg")
    clip = await cat.clip("a/drone.mp4", [(0, 4000), (4000, 9000)])
    await cat.look(photo, ok=False, request_id=this_review)
    await cat.look(clip, (0, 4000), ok=False, request_id=this_review)

    assert await cat.items(photo, request_id=this_review) == []
    assert await cat.items(clip, request_id=this_review) == [(4000, 9000)]
    # A new review is the way out: it tries them again.
    assert await cat.items(photo, request_id=next_review) == [(None, None)]
    assert await cat.items(clip, request_id=next_review) == [(0, 4000), (4000, 9000)]

    # The dialog asks for a new review, which tries them again...
    counts = await cat.count()
    assert (counts.photos, counts.shots, counts.failed_before) == (1, 2, 2)
    # ...but the review they failed in is not handed them again, and its
    # progress bar does not wait for them.
    assert await cat.remaining(scope.Run(this_review)) == 1
    assert await cat.walk(run=scope.Run(this_review)) == ["a/drone.mp4"]
    assert await cat.remaining(scope.Run(next_review)) == 3
    assert await cat.walk(run=scope.Run(next_review)) == ["a/blurry.jpg", "a/drone.mp4"]


async def test_looking_at_ingest_gives_an_item_two_tries_per_build(cat: Catalogue) -> None:
    photo = await cat.photo("Card/a.jpg")
    for tries in range(INGEST_TRIES):
        assert await cat.items(photo) == [(None, None)], tries
        await cat.look(photo, ok=False)

    assert await cat.items(photo) == []
    # Another build, or another question, starts its own count.
    assert await cat.items(photo, key=OTHER_KEY) == [(None, None)]


async def test_a_failure_does_not_hide_a_look_that_counts(cat: Catalogue) -> None:
    photo = await cat.photo("a/front.jpg")
    await cat.look(photo, ok=False)
    await cat.look(photo)

    assert await cat.items(photo) == []
    assert (await cat.count()).failed_before == 0


async def test_what_ingest_has_given_up_on_leaves_it_nothing_to_do(cat: Catalogue) -> None:
    # A clip whose one shot has no picture fails every try. Its roll-up never
    # says "done", so without this its library would read as never finished
    # and every scan would start a model-loading pass that finds nothing.
    clip = await cat.clip("Card/blink.mp4", [(0, 4000)])
    photo = await cat.photo("Card/broken.jpg")
    for _ in range(INGEST_TRIES):
        await cat.look(clip, (0, 4000), ok=False)
        await cat.look(photo, ok=False)

    assert await cat.items(clip) == [] and await cat.items(photo) == []
    assert await cat.remaining(INGEST) == 0
    assert await cat.walk(run=INGEST) == []
    ingest = await cat.count(run=INGEST)
    assert (ingest.items, ingest.already, ingest.failed_before) == (0, 0, 0)

    # A review somebody asks for tries them again, and the dialog says so.
    assert (await cat.count()).items == 2 and (await cat.count()).failed_before == 2
    review = scope.Run(await cat.review())
    assert await cat.remaining(review) == 2
    assert await cat.walk(run=review) == ["Card/blink.mp4", "Card/broken.jpg"]

    # One try short is still to do.
    other = await cat.clip("Card/other.mp4", [(0, 4000)])
    await cat.look(other, (0, 4000), ok=False)
    assert await cat.remaining(INGEST) == 1
    assert await cat.walk(run=INGEST) == ["Card/other.mp4"]


async def test_what_a_run_counts_is_what_its_pass_hands_out(cat: Catalogue) -> None:
    # The progress bar's total is `remaining`, and the pass is `next_assets`
    # then `items_for`: if the two disagree the bar never fills, or a job is
    # started for an asset that gives it nothing.
    review = await cat.review()
    earlier = await cat.review()
    await cat.photo("s/p1.jpg")
    p2 = await cat.photo("s/p2.jpg")
    await cat.look(p2, ok=False, request_id=review)
    p3 = await cat.photo("s/p3.jpg")
    for _ in range(INGEST_TRIES):
        await cat.look(p3, ok=False)
    p4 = await cat.photo("s/p4.jpg")
    await cat.look(p4, ok=False, request_id=earlier)
    p5 = await cat.photo("s/p5.jpg")
    await cat.look(p5, key=OTHER_KEY)
    await cat.roll_up(p5, OTHER_KEY)
    await cat.photo("s/p6.jpg", derived=())
    c1 = await cat.clip("s/c1.mp4", [(0, 4000), (4000, 9000), (9000, 12000), (12000, 15000)])
    await cat.look(c1, (0, 4000))
    for _ in range(INGEST_TRIES):
        await cat.look(c1, (4000, 9000), ok=False)
    await cat.look(c1, (9000, 12000), ok=False, request_id=review)
    c2 = await cat.clip("s/c2.braw", derived=("poster",))
    await cat.look(c2, ok=False, request_id=review)
    c3 = await cat.clip("s/c3.mp4", derived=("poster", "thumbnail"))
    for _ in range(INGEST_TRIES):
        await cat.look(c3, ok=False)
    await cat.clip("s/c4.mp4", derived=("poster", "proxy"))
    await cat.clip("s/c5.mp4", derived=("thumbnail",), availability="unmounted")
    c6 = await cat.clip("s/c6.mp4", [(0, 4000)])
    await cat.look(c6, (0, 4000), key=OTHER_KEY)
    await cat.roll_up(c6, OTHER_KEY)
    await cat.clip("s/c7.mp4", derived=())
    # Shots on record but no ready analysis file: missing, and failed. They
    # wait to be made ready like c3 and c4, and on the last pass they are
    # their shots, never one item of the whole.
    await cat.clip("s/c8.mp4", [(0, 4000), (4000, 9000)], derived=("poster", "proxy"))
    c9 = await cat.clip("s/c9.mp4", [(0, 4000)], derived=("poster",))
    async with cat.session() as db:
        db.add(
            Derivative(
                asset_id=c9,
                kind="motion",
                relative_path=f"derivatives/{c9}/motion.json",
                media_format="json",
                status="failed",
            )
        )
        await db.commit()
    async with cat.session() as db:
        every = list((await db.execute(select(Asset.id))).scalars())

    runs = [
        scope.Run(None),
        scope.Run(None, stills_fallback=True),
        scope.Run(review),
        scope.Run(review, stills_fallback=True),
        scope.Run(uuid.uuid4()),
    ]
    for run, redo in itertools.product(runs, (False, True)):
        handed_out = 0
        visited = set()
        for asset in await cat.pass_over(run=run, redo=redo, limit=3):
            items = await cat.items(
                asset.id,
                redo=redo,
                request_id=run.request_id,
                stills_fallback=run.stills_fallback,
            )
            assert items, (run, redo, asset.relative_path)
            handed_out += len(items)
            visited.add(asset.id)
        assert await cat.remaining(run, redo=redo) == handed_out, (run, redo)
        # And nothing with an item is passed over.
        for asset_id in set(every) - visited:
            assert (
                await cat.items(
                    asset_id,
                    redo=redo,
                    request_id=run.request_id,
                    stills_fallback=run.stills_fallback,
                )
                == []
            ), (run, redo, asset_id)
    # By hand: ingest has given up on p3, c3 and c1's second shot; p2, c2 and
    # c1's third shot failed in this review.
    assert await cat.remaining(scope.Run(None)) == 6  # p1 p2 p4, c1's 3rd and 4th, c2
    assert await cat.remaining(scope.Run(review)) == 5  # p1 p3 p4, c1's 2nd and 4th
    # The last pass adds the clips that never became ready: c3, c4, and c5,
    # whose share is away, one still each; c8's two shots and c9's one.
    assert await cat.remaining(scope.Run(review, stills_fallback=True)) == 8 + 3


# --- the walk and the count ---------------------------------------------------------


async def test_a_pass_goes_in_path_order_page_by_page_and_never_repeats(cat: Catalogue) -> None:
    paths = [f"Shoot/{name}" for name in ("c.jpg", "a.jpg", "b_1.jpg", "b%2.jpg", "B.jpg")]
    for path in paths:
        await cat.photo(path)

    assert await cat.walk(limit=2) == sorted(paths)


async def test_a_pass_does_not_skip_what_drops_out_of_the_filter_as_it_goes(
    cat: Catalogue,
) -> None:
    for n in range(5):
        await cat.photo(f"Shoot/{n}.jpg")
    seen: list[str] = []
    after = ""
    async with cat.session() as db:
        while page := await scope.next_assets(
            db, Scope(cat.library), key=KEY, redo=False, run=INGEST, after=after, limit=2
        ):
            seen += [asset.relative_path for asset in page]
            after = page[-1].relative_path
            for asset in page:  # looked at, so out of the filter: an offset would now skip
                await cat.look(asset.id)
                await cat.roll_up(asset.id)
    assert seen == [f"Shoot/{n}.jpg" for n in range(5)]


async def test_counting_reads_no_path_and_never_touches_the_disk(
    cat: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    await cat.photo("Brookside/a.jpg")
    await cat.photo("Brookside/b.jpg", derived=())
    clip = await cat.clip("Brookside/c.mp4", [(0, 4000), (4000, 8000)])
    await cat.look(clip, (0, 4000), ok=False)
    await cat.clip("Brookside/d.mp4", derived=("poster",))
    await cat.clip("Brookside/e.braw", derived=("poster",))
    await cat.asset("Brookside/f.wav", "audio")

    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    def no_disk(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the scope was answered from the disk")

    for name in ("is_file", "exists", "stat", "iterdir", "glob", "rglob"):
        monkeypatch.setattr(pathlib.Path, name, no_disk)
    for name in ("scandir", "listdir", "walk"):
        monkeypatch.setattr(os, name, no_disk)
    event.listen(cat.engine.sync_engine, "before_cursor_execute", capture)
    try:
        counts = await cat.count(Scope(cat.library, "Brookside"))
    finally:
        event.remove(cat.engine.sync_engine, "before_cursor_execute", capture)

    assert counts.items == 4 and counts.failed_before == 1 and counts.audio == 1
    assert statements and len(statements) <= 4
    for statement in statements:
        # A path may be compared with the folder (folders.under: a LIKE and an
        # exact prefix), never read back.
        for found in re.finditer(r"[\w.]*relative_path", statement):
            before = statement[: found.start()]
            after = statement[found.end() :]
            assert after.startswith(" LIKE") or before.endswith("substr("), statement


async def test_remaining_is_what_could_be_looked_at_now(cat: Catalogue) -> None:
    await cat.photo("a/1.jpg")
    await cat.clip("a/2.mp4", [(0, 4000), (4000, 8000)])
    await cat.clip("a/3.braw", derived=("poster",))
    await cat.clip("a/4.mp4", derived=("poster",))

    assert await cat.remaining(INGEST) == 4
    # The last pass of a review also judges the clip with no copy from its poster.
    assert await cat.remaining(scope.Run(uuid.uuid4(), stills_fallback=True)) == 5


async def _id(cat: Catalogue, relative_path: str) -> uuid.UUID:
    async with cat.session() as db:
        found = await db.scalar(select(Asset.id).where(Asset.relative_path == relative_path))
        assert found is not None
        return found
