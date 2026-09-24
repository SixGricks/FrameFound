"""Folding sub-folder libraries into one library at their parent.

The property that matters is identity: every asset keeps its id, so the
embeddings, faces, transcripts, tags, listings and edits attached to it come
along untouched — only its library and relative path change. Everything else
here is about refusing the merges that would lose or duplicate data.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.db.base import Base
from framefound.db.models import (
    Asset,
    AssetEdit,
    AuditLog,
    Frame,
    Library,
    Listing,
    ListingItem,
    PathMapping,
    Scan,
)
from framefound.ops import merge_libraries as merge

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'merge.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _library(db: AsyncSession, name: str, root: str, **settings: object) -> Library:
    library = Library(name=name, root_path=root, **settings)  # type: ignore[arg-type]
    db.add(library)
    await db.flush()
    return library


async def _asset(db: AsyncSession, library: Library, relative_path: str) -> Asset:
    asset = Asset(
        library_id=library.id,
        relative_path=relative_path,
        filename=relative_path.rsplit("/", 1)[-1],
        extension="jpg",
        media_type="image",
        size_bytes=100,
        mtime=BASE,
    )
    db.add(asset)
    await db.flush()
    return asset


@pytest.fixture()
async def intel(db: AsyncSession) -> dict:
    """The production shape: three libraries under /media/intel, one elsewhere."""
    year = await _library(
        db,
        "Intel 2026",
        "/media/intel/2026",
        exclude_globs=["#recycle", "@eaDir", "Adobe Premiere*"],
        scan_interval_minutes=60,
        generate_proxies=False,
    )
    breeze = await _library(
        db,
        "Breeze Video",
        "/media/intel/Breeze Video",
        exclude_globs=["#recycle", "*.tmp"],
        generate_proxies=True,
    )
    promo = await _library(
        db,
        "Promo Video",
        "/media/intel/PROMO VIDEO",
        exclude_globs=["*.lrf"],
        scan_interval_minutes=1440,
    )
    gelco = await _library(db, "GELCO", "/media/gelco")

    kitchen = await _asset(db, year, "09-22 - 1505 W Kings Hwy Gap/MLS/IMG_1.jpg")
    await _asset(db, year, "09-22 - 1505 W Kings Hwy Gap/MLS/IMG_2.jpg")
    await _asset(db, year, "10-01 338 Churchtown Rd/IMG_3.jpg")
    drone = await _asset(db, breeze, "DJI_0001.jpg")
    await _asset(db, breeze, "b-roll/DJI_0002.jpg")
    promo_still = await _asset(db, promo, "hero.jpg")
    elsewhere = await _asset(db, gelco, "IMG_1.jpg")

    # Work that must survive the move, attached by asset id.
    db.add(Frame(asset_id=drone.id, ts_ms=0, relative_path="frames/x.jpeg", embedding=[0.1] * 512))
    db.add(AssetEdit(asset_id=kitchen.id, version=1, recipe={"exposure": 0.3}))
    listing = Listing(name="1505 W Kings Hwy Gap")
    db.add(listing)
    await db.flush()
    db.add(ListingItem(listing_id=listing.id, asset_id=kitchen.id, position=0))

    db.add(Scan(library_id=year.id, status="running", files_seen=3000))
    db.add(Scan(library_id=breeze.id, status="completed"))
    db.add(Scan(library_id=promo.id, status="pending"))
    db.add(
        PathMapping(
            library_id=year.id, profile_name="DJ's PC", platform="windows", mapped_prefix="Z:\\2026"
        )
    )
    db.add(
        PathMapping(
            library_id=breeze.id,
            profile_name="DJ's PC",
            platform="windows",
            mapped_prefix="Z:\\Breeze Video",
        )
    )
    db.add(
        PathMapping(
            library_id=promo.id, profile_name="Mac", platform="macos", mapped_prefix="/Volumes/x"
        )
    )
    await db.commit()
    # Plain ids, not ORM objects: reading an attribute off an object after
    # expire_all() is a lazy load, which async sessions refuse.
    return {
        "year": year.id,
        "breeze": breeze.id,
        "promo": promo.id,
        "gelco": gelco.id,
        "kitchen": kitchen.id,
        "drone": drone.id,
        "promo_still": promo_still.id,
        "elsewhere": elsewhere.id,
        "listing": listing.id,
    }


async def test_the_plan_keeps_the_biggest_library_and_merges_its_settings(
    db: AsyncSession, intel: dict
) -> None:
    plan = await merge.plan_merge(db, "/media/intel", "Intel")

    assert plan.target.id == intel["year"], "the biggest library survives, id and all"
    assert {m.name: m.subfolder for m in plan.members} == {
        "Breeze Video": "Breeze Video",
        "Intel 2026": "2026",
        "Promo Video": "PROMO VIDEO",
    }
    assert plan.exclude_globs == ["#recycle", "@eaDir", "Adobe Premiere*", "*.tmp", "*.lrf"]
    assert plan.scan_interval_minutes == 60, "the most frequent schedule wins"
    assert plan.path_mappings == {"DJ's PC": ("windows", "Z:\\")}
    assert any("generate_proxies" in note for note in plan.notes)
    assert any("'Mac'" in note and "dropped" in note for note in plan.notes)


async def test_planning_changes_nothing(db: AsyncSession, intel: dict) -> None:
    await merge.plan_merge(db, "/media/intel", "Intel")
    assert (await db.execute(select(func.count()).select_from(Library))).scalar_one() == 4
    kitchen = await db.get(Asset, intel["kitchen"])
    assert kitchen is not None and kitchen.relative_path.startswith("09-22")


async def test_applying_reparents_every_asset_and_keeps_its_work(
    db: AsyncSession, intel: dict
) -> None:
    plan = await merge.plan_merge(db, "/media/intel", "Intel")
    await merge.apply_merge(db, plan)
    db.expire_all()

    merged = await db.get(Library, intel["year"])
    assert merged is not None
    assert (merged.name, merged.root_path) == ("Intel", "/media/intel")
    assert merged.scan_interval_minutes == 60
    assert await db.get(Library, intel["breeze"]) is None
    assert await db.get(Library, intel["promo"]) is None

    paths = dict(
        (
            await db.execute(
                select(Asset.id, Asset.relative_path).where(Asset.library_id == merged.id)
            )
        ).all()
    )
    assert paths[intel["kitchen"]] == "2026/09-22 - 1505 W Kings Hwy Gap/MLS/IMG_1.jpg"
    assert paths[intel["drone"]] == "Breeze Video/DJI_0001.jpg"
    assert paths[intel["promo_still"]] == "PROMO VIDEO/hero.jpg"
    assert len(paths) == 6

    elsewhere = await db.get(Asset, intel["elsewhere"])
    assert elsewhere is not None and elsewhere.relative_path == "IMG_1.jpg", "GELCO untouched"

    # The work came along: same ids, same rows.
    frames = (await db.execute(select(Frame).where(Frame.asset_id == intel["drone"]))).all()
    assert len(frames) == 1
    edits = (
        await db.execute(select(AssetEdit).where(AssetEdit.asset_id == intel["kitchen"]))
    ).all()
    assert len(edits) == 1
    item = (
        await db.execute(select(ListingItem).where(ListingItem.listing_id == intel["listing"]))
    ).scalar_one()
    assert item.asset_id == intel["kitchen"]

    scans = (await db.execute(select(Scan.library_id, Scan.status))).all()
    assert {library_id for library_id, _ in scans} == {merged.id}, "history moved, not lost"
    statuses = sorted(status for _, status in scans)
    assert statuses == ["cancelled", "cancelled", "completed", "pending"], (
        "scans of the old roots are cancelled and exactly one scan of the parent is queued"
    )

    mappings = (await db.execute(select(PathMapping))).scalars().all()
    assert [(m.profile_name, m.mapped_prefix) for m in mappings] == [("DJ's PC", "Z:\\")]
    assert (
        await db.execute(select(AuditLog).where(AuditLog.event == "library.merged"))
    ).scalar_one().detail["into"] == "Intel"


async def test_a_library_already_at_the_parent_is_the_one_kept(db: AsyncSession) -> None:
    parent = await _library(db, "Intel", "/media/intel")
    await _asset(db, parent, "loose.jpg")
    child = await _library(db, "Big", "/media/intel/2026")
    for i in range(5):
        await _asset(db, child, f"IMG_{i}.jpg")
    await db.commit()

    plan = await merge.plan_merge(db, "/media/intel", "Intel")
    assert plan.target.id == parent.id, "already at the root beats being bigger"
    await merge.apply_merge(db, plan)
    db.expire_all()
    loose = (await db.execute(select(Asset).where(Asset.filename == "loose.jpg"))).scalar_one()
    assert loose.relative_path == "loose.jpg", "no prefix for files already at the root"


async def test_it_refuses_a_parent_inside_another_library(db: AsyncSession, intel: dict) -> None:
    with pytest.raises(merge.MergeError, match="inside the library 'Intel 2026'"):
        await merge.plan_merge(db, "/media/intel/2026/sub", "Sub")


async def test_it_refuses_a_folder_with_no_library_under_it(db: AsyncSession, intel: dict) -> None:
    with pytest.raises(merge.MergeError, match="No library"):
        await merge.plan_merge(db, "/media/nowhere", "Nowhere")


async def test_it_refuses_a_name_another_library_already_has(db: AsyncSession, intel: dict) -> None:
    with pytest.raises(merge.MergeError, match="already called 'GELCO'"):
        await merge.plan_merge(db, "/media/intel", "GELCO")


async def test_it_refuses_libraries_that_overlap(db: AsyncSession, intel: dict) -> None:
    """Nested libraries catalogue the same file twice; after a move both rows
    would claim one path, so the merge stops before touching anything."""
    nested = await _library(db, "MLS only", "/media/intel/2026/09-22 - 1505 W Kings Hwy Gap")
    await _asset(db, nested, "MLS/IMG_1.jpg")
    await db.commit()

    with pytest.raises(merge.MergeError, match="overlap"):
        await merge.plan_merge(db, "/media/intel", "Intel")


@pytest.mark.parametrize(
    ("prefix", "subfolder", "lifted"),
    [
        ("Z:\\2026", "2026", "Z:\\"),
        ("Z:\\2026\\", "2026", "Z:\\"),
        ("Z:\\Intel\\2026", "2026", "Z:\\Intel"),
        ("Z:\\Breeze Video", "Breeze Video", "Z:\\"),
        ("\\\\nas\\intel\\2026", "2026", "\\\\nas\\intel"),
        ("/Volumes/intel/2026", "2026", "/Volumes/intel"),
        ("/2026", "2026", "/"),
        ("Z:\\X2026", "2026", None),
        ("/Volumes/other", "2026", None),
        ("W:\\", "", "W:\\"),
    ],
)
def test_workstation_prefixes_are_lifted_to_the_parent(
    prefix: str, subfolder: str, lifted: str | None
) -> None:
    assert merge._parent_prefix(prefix, subfolder) == lifted


async def test_the_command_plans_unless_told_to_apply(
    db: AsyncSession,
    intel: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from framefound.config import get_settings

    monkeypatch.setenv(
        "FRAMEFOUND_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'merge.db').as_posix()}"
    )
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "merge-secret")
    get_settings.cache_clear()
    try:
        assert await merge.main(["/media/intel", "--name", "Intel"]) == 0
        assert "Nothing changed" in capsys.readouterr().out
        assert await merge.main(["/media/intel", "--name", "GELCO"]) == 1

        assert await merge.main(["/media/intel", "--name", "Intel", "--apply"]) == 0
        assert "Merged. 6 assets" in capsys.readouterr().out
        db.expire_all()
        assert (await db.execute(select(func.count()).select_from(Library))).scalar_one() == 2
    finally:
        get_settings.cache_clear()
