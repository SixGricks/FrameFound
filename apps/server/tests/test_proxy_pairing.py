"""Premiere's editing proxies paired with their originals (scanner/proxies.py).

The GELCO case that prompted it: `Pete Dye/2025 10 28/MVI_4513.MP4` and
`Pete Dye/2025 10 28/Proxies/MVI_4513_Proxy.mp4` both came back from every
search, and the proxy was dated the day it was rendered.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_scan import db, make_library, scan_once, write_old  # noqa: F401 - fixture

from framefound.db.models import PAIRED_PROXY_STATUS, Asset, AssetTag, Library, Tag
from framefound.ops import pair_proxies
from framefound.scanner.proxies import ProxyMatch, ProxyPattern, match_proxy, reconcile

DEFAULT = ProxyPattern.parse("Proxies/{stem}_Proxy")
SHOOT = "Pete Dye/2025 10 28"


# --- the rule ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        f"{SHOOT}/Proxies/MVI_4513_Proxy.mp4",
        f"{SHOOT}/proxies/mvi_4513_proxy.MOV",  # case of folder, stem and extension
        f"{SHOOT}/PROXIES/MVI_4513_PROXY.Mp4",
    ],
)
def test_premiere_proxies_match_whatever_the_case(path: str) -> None:
    match = match_proxy(path, DEFAULT)
    assert match is not None
    assert match.original_dir == SHOOT
    assert match.stem.casefold() == "mvi_4513"


@pytest.mark.parametrize(
    "path",
    [
        f"{SHOOT}/MVI_4513.MP4",  # an original
        f"{SHOOT}/Proxies/MVI_4513.mp4",  # no _Proxy
        f"{SHOOT}/Renders/MVI_4513_Proxy.mp4",  # not the Proxies folder
        f"{SHOOT}/Proxies/MVI_4513_Proxy.jpg",  # a still is somebody's photograph
        f"{SHOOT}/Proxies/_Proxy.mp4",  # nothing left for a stem
    ],
)
def test_other_files_are_not_proxies(path: str) -> None:
    assert match_proxy(path, DEFAULT) is None


def test_a_proxies_folder_at_the_library_root_pairs_with_the_root() -> None:
    assert match_proxy("Proxies/A001_Proxy.mov", DEFAULT) == ProxyMatch("", "A001")


def test_the_pattern_is_a_setting() -> None:
    custom = ProxyPattern.parse("Low Res/{stem}-lowres")
    assert match_proxy("Shoot/Low Res/A001-LOWRES.mp4", custom) == ProxyMatch("Shoot", "A001")
    assert ProxyPattern.parse("") is None  # off
    for bad in ("Proxies/_Proxy", "{stem}", "a/b/{stem}_Proxy", "Proxies/{stem}{stem}"):
        with pytest.raises(ValueError):
            ProxyPattern.parse(bad)


# --- scanning ----------------------------------------------------------------


async def _row(db: AsyncSession, path: str) -> Asset:  # noqa: F811
    return (await db.execute(select(Asset).where(Asset.relative_path == path))).scalar_one()


async def test_a_scan_pairs_the_proxy_and_never_queues_it(db: AsyncSession, tmp_path: Path) -> None:  # noqa: F811
    root = tmp_path / "gelco"
    write_old(root / SHOOT / "MVI_4513.MP4", b"the 4k original")
    write_old(root / SHOOT / "Proxies" / "MVI_4513_Proxy.mp4", b"the proxy")
    library = await make_library(db, root)

    enqueued: list[uuid.UUID] = []
    await scan_once(db, library, enqueued)

    original = await _row(db, f"{SHOOT}/MVI_4513.MP4")
    proxy = await _row(db, f"{SHOOT}/Proxies/MVI_4513_Proxy.mp4")
    assert proxy.proxy_of_id == original.id
    assert proxy.processing_status == PAIRED_PROXY_STATUS
    assert enqueued == [original.id]


async def test_a_proxy_without_its_original_is_the_only_copy(
    db: AsyncSession,  # noqa: F811
    tmp_path: Path,
) -> None:
    root = tmp_path / "gelco"
    write_old(root / SHOOT / "Proxies" / "MVI_9999_Proxy.mp4", b"orphaned proxy")
    library = await make_library(db, root)

    enqueued: list[uuid.UUID] = []
    await scan_once(db, library, enqueued)

    proxy = await _row(db, f"{SHOOT}/Proxies/MVI_9999_Proxy.mp4")
    assert proxy.proxy_of_id is None
    assert enqueued == [proxy.id]  # processed like any asset, once, after the pass


async def test_case_variants_pair_across_extensions(db: AsyncSession, tmp_path: Path) -> None:  # noqa: F811
    root = tmp_path / "gelco"
    write_old(root / SHOOT / "mvi_4600.mov", b"original in lower case")
    write_old(root / SHOOT / "PROXIES" / "MVI_4600_PROXY.MP4", b"proxy in upper case")
    library = await make_library(db, root)

    await scan_once(db, library, [])

    original = await _row(db, f"{SHOOT}/mvi_4600.mov")
    proxy = await _row(db, f"{SHOOT}/PROXIES/MVI_4600_PROXY.MP4")
    assert proxy.proxy_of_id == original.id


async def test_a_rescan_leaves_pairs_as_they_are(db: AsyncSession, tmp_path: Path) -> None:  # noqa: F811
    root = tmp_path / "gelco"
    write_old(root / SHOOT / "MVI_4513.MP4", b"the 4k original")
    write_old(root / SHOOT / "Proxies" / "MVI_4513_Proxy.mp4", b"the proxy")
    library = await make_library(db, root)
    await scan_once(db, library, [])
    first = (await _row(db, f"{SHOOT}/Proxies/MVI_4513_Proxy.mp4")).proxy_of_id

    again: list[uuid.UUID] = []
    scan = await scan_once(db, library, again)

    proxy = await _row(db, f"{SHOOT}/Proxies/MVI_4513_Proxy.mp4")
    assert proxy.proxy_of_id == first
    assert proxy.processing_status == PAIRED_PROXY_STATUS
    assert again == []
    assert scan.files_new == 0 and scan.files_changed == 0


async def test_deleting_the_original_releases_its_proxy(db: AsyncSession, tmp_path: Path) -> None:  # noqa: F811
    root = tmp_path / "gelco"
    write_old(root / SHOOT / "MVI_4513.MP4", b"the 4k original")
    write_old(root / SHOOT / "Proxies" / "MVI_4513_Proxy.mp4", b"the proxy")
    library = await make_library(db, root)
    await scan_once(db, library, [])

    (root / SHOOT / "MVI_4513.MP4").unlink()
    enqueued: list[uuid.UUID] = []
    await scan_once(db, library, enqueued)

    proxy = await _row(db, f"{SHOOT}/Proxies/MVI_4513_Proxy.mp4")
    assert proxy.proxy_of_id is None
    assert proxy.processing_status == "pending"
    assert enqueued == [proxy.id]


async def test_pairing_off_keeps_proxies_as_assets(db: AsyncSession, tmp_path: Path) -> None:  # noqa: F811
    root = tmp_path / "gelco"
    write_old(root / SHOOT / "MVI_4513.MP4", b"the 4k original")
    write_old(root / SHOOT / "Proxies" / "MVI_4513_Proxy.mp4", b"the proxy")
    library = await make_library(db, root)
    library.proxy_pattern = ""
    await db.commit()

    enqueued: list[uuid.UUID] = []
    await scan_once(db, library, enqueued)

    assert (await _row(db, f"{SHOOT}/Proxies/MVI_4513_Proxy.mp4")).proxy_of_id is None
    assert len(enqueued) == 2


# --- rows from before pairing ------------------------------------------------


async def _legacy(db: AsyncSession) -> tuple[Library, Asset, Asset]:  # noqa: F811
    """Two rows the old scanner left: the original and its proxy, both
    catalogued, the proxy dated the day it was rendered."""
    library = Library(name="GELCO", root_path="/media/gelco")
    db.add(library)
    await db.flush()

    def asset(path: str, captured: datetime) -> Asset:
        return Asset(
            library_id=library.id,
            relative_path=path,
            filename=path.rsplit("/", 1)[-1],
            extension=path.rsplit(".", 1)[-1].lower(),
            media_type="video",
            size_bytes=1,
            mtime=captured,
            captured_at=captured,
            availability="online",
            processing_status="ready",
        )

    original = asset(f"{SHOOT}/MVI_4513.MP4", datetime(2025, 10, 28, 16, 2, tzinfo=UTC))
    proxy = asset(f"{SHOOT}/Proxies/MVI_4513_Proxy.mp4", datetime(2026, 2, 12, 16, 25, tzinfo=UTC))
    db.add_all([original, proxy])
    await db.commit()
    return library, original, proxy


async def test_the_backfill_reports_before_it_changes_anything(db: AsyncSession) -> None:  # noqa: F811
    _library, _original, proxy = await _legacy(db)

    plans = await pair_proxies.plan_all(db)
    report = await pair_proxies.describe(db, plans)

    assert "GELCO" in report and "1 to pair" in report
    assert "dated 2026-02-12 as a proxy, 2025-10-28 as the original" in report
    await db.refresh(proxy)
    assert proxy.proxy_of_id is None  # a dry run writes nothing


async def test_tag_decisions_on_a_proxy_move_to_the_original(db: AsyncSession) -> None:  # noqa: F811
    library, original, proxy = await _legacy(db)
    mainline, bunker, cart = (
        Tag(name="mainline", slug="mainline"),
        Tag(name="bunker", slug="bunker"),
        Tag(name="cart", slug="cart"),
    )
    db.add_all([mainline, bunker, cart])
    await db.flush()
    db.add_all(
        [
            # Decided on the proxy, nothing on the original: moves.
            AssetTag(asset_id=proxy.id, tag_id=mainline.id, source="manual"),
            # Decided on the proxy, only suggested on the original: the decision wins.
            AssetTag(asset_id=proxy.id, tag_id=bunker.id, source="rejected"),
            AssetTag(asset_id=original.id, tag_id=bunker.id, source="suggested", confidence=0.4),
            # The original's own decision stands.
            AssetTag(asset_id=proxy.id, tag_id=cart.id, source="suggested"),
            AssetTag(asset_id=original.id, tag_id=cart.id, source="manual"),
        ]
    )
    await db.commit()

    plan, requeue = await reconcile(db, library)
    await db.commit()

    assert requeue == []
    on_original = {
        row.tag_id: row.source
        for row in (
            await db.execute(select(AssetTag).where(AssetTag.asset_id == original.id))
        ).scalars()
    }
    assert on_original == {mainline.id: "manual", bunker.id: "rejected", cart.id: "manual"}
    left = (await db.execute(select(AssetTag).where(AssetTag.asset_id == proxy.id))).scalars().all()
    assert left == []
    await db.refresh(proxy)
    assert proxy.proxy_of_id == original.id  # the row is kept, linked
    assert len(plan.link) == 1


async def test_search_never_returns_a_paired_proxy(db: AsyncSession) -> None:  # noqa: F811
    from framefound.api.v1.search import run_search

    library, original, proxy = await _legacy(db)
    before = await run_search(db, "MVI_4513", None, 10)
    assert {hit.asset_id for hit in before.filename_hits} == {original.id, proxy.id}

    await reconcile(db, library)
    await db.commit()

    after = await run_search(db, "MVI_4513", None, 10)
    assert [hit.asset_id for hit in after.filename_hits] == [original.id]
    assert [r.asset_id for r in after.results] == [original.id]
