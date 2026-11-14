"""The bulk store (storage/stores.py, storage/moves.py): every bulky kind in
a folder of its own, movable on its own, and paused — not failed, not lost —
while that folder is away.

The proxy store was the first of these and keeps its own tests
(test_proxy_store.py); these are about the rule being the same for the rest."""

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    Asset,
    Derivative,
    Job,
    Library,
    Listing,
    ListingItem,
    Slideshow,
)
from framefound.native import backup, database, envfile
from framefound.native.layout import Layout
from framefound.processing import derivatives as deriv
from framefound.storage import moves, stores
from framefound.storage.stores import (
    BULK_STORE_MARKER,
    INCOMING_MARKER,
    PROXY_STORE_MARKER,
    STORES_FILE,
)
from framefound.storage.worker_roots import DATA_STORE_MARKER, NotHere

PROXY = "derivatives/ab/abcd/proxy.mp4"
THUMB = "derivatives/ab/abcd/thumbnail.webp"
RENDER = "renders/show.mp4"
EXPORT = "exports/listings/house.zip"
BASEMAP = "basemaps/pennsylvania.pmtiles"
ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}

# A move waits ten seconds for the old folder to go quiet. Not in a test.
QUICK: dict[str, Any] = {"settle_s": 0.0, "sleep": lambda _seconds: None}


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    get_settings.cache_clear()
    stores._CACHE.clear()


def at(data: Path, **more: Any) -> Settings:
    return Settings(data_dir=data, **more)


def use(monkeypatch: pytest.MonkeyPatch, data: Path, bulk: Path | None = None) -> None:
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(data))
    monkeypatch.setenv("FRAMEFOUND_BULK_DIR", str(bulk) if bulk else "")
    get_settings.cache_clear()


def put(root: Path, relative: str, content: bytes = b"content") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def names(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


# --- where a kind lives ----------------------------------------------------------


def test_with_nothing_configured_every_file_is_where_it_always_was(tmp_path: Path) -> None:
    data, models = tmp_path / "data", tmp_path / "models"
    here = at(data, models_dir=models)
    for relative in (PROXY, THUMB, RENDER, EXPORT, BASEMAP, "frames/ab/abcd/000001000.jpeg"):
        assert stores.path_for(relative, here) == data / relative
    assert stores.kind_root("models", here) == models
    assert stores.kind_root("backups", here) is None  # no off-machine copy unless asked for
    for kind in stores.KINDS:
        assert not stores.is_separate(kind, here)
        assert stores.kind_problem(kind, here) is None
        assert stores.write_problem(kind, here) is None
    assert not data.exists()  # looking things up creates nothing


def test_a_bulk_folder_gives_each_kind_a_folder_of_its_own(tmp_path: Path) -> None:
    data, nas = tmp_path / "data", tmp_path / "Photos" / "Framefound"
    here = at(data, bulk_dir=nas)
    # A proxy keeps its whole path, so a proxy store made before there was a
    # bulk folder (`--proxies <bulk>/proxies`) is already in the right place.
    assert stores.path_for(PROXY, here) == nas / "proxies" / PROXY
    # The others drop the first folder of theirs: it is the kind's own name.
    assert stores.path_for(RENDER, here) == nas / "renders" / "show.mp4"
    assert stores.path_for(EXPORT, here) == nas / "exports" / "listings" / "house.zip"
    assert stores.path_for(BASEMAP, here) == nas / "basemaps" / "pennsylvania.pmtiles"
    assert stores.kind_root("models", here) == nas / "models"
    assert stores.kind_root("backups", here) == nas / "backups"
    # What every page load reads stays on this machine, and so does scratch.
    assert stores.path_for(THUMB, here) == data / THUMB
    assert (
        stores.path_for("renders/work/show/final.mp4", here) == data / "renders/work/show/final.mp4"
    )
    assert stores.kind_of("renders/work/show/final.mp4") is None


def test_a_kinds_own_folder_beats_its_setting_which_beats_the_bulk_folder(tmp_path: Path) -> None:
    data, nas, ssd, other = (tmp_path / name for name in ("data", "nas", "ssd", "other"))

    assert stores.kind_root("proxies", at(data, bulk_dir=nas)) == nas / "proxies"
    assert stores.placed_by("proxies", at(data, bulk_dir=nas)) == "bulk"
    both = at(data, bulk_dir=nas, proxy_dir=ssd, backup_copy_dir=ssd / "backups")
    assert stores.kind_root("proxies", both) == ssd
    assert stores.kind_root("backups", both) == ssd / "backups"
    assert stores.kind_root("renders", both) == nas / "renders"  # a setting is for its own kind

    stores.set_override(data, "proxies", other)
    stores.set_override(data, "backups", other / "backups")
    assert stores.kind_root("proxies", both) == other
    assert stores.kind_root("backups", both) == other / "backups"
    assert stores.placed_by("proxies", both) == "own"

    # Moved back onto this machine: recorded, so the bulk folder does not
    # quietly claim the kind again.
    stores.set_override(data, "renders", data / "renders")
    assert stores.kind_root("renders", both) == data / "renders"
    assert not stores.is_separate("renders", both)
    assert stores.kind_problem("renders", both) is None

    stores.clear_override(data, "proxies")
    assert stores.kind_root("proxies", both) == ssd


def test_blank_storage_settings_mean_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FRAMEFOUND_BULK_DIR=` in the env file must not become the current
    working directory."""
    monkeypatch.setenv("FRAMEFOUND_BULK_DIR", "  ")
    monkeypatch.setenv("FRAMEFOUND_BACKUP_COPY_DIR", "")
    loaded = Settings()
    assert loaded.bulk_dir is None and loaded.backup_copy_dir is None


def test_an_unknown_kind_is_an_error_not_a_folder(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a kind of storage"):
        stores.kind_root("thumbnails", at(tmp_path))


# --- the record that makes a switch take effect without a restart ------------


def test_a_switch_reaches_a_process_that_is_already_running(tmp_path: Path) -> None:
    """Settings are read once per process, and every worker child has its
    own copy. A move must not need them all restarted."""
    data, nas = tmp_path / "data", tmp_path / "nas"
    data.mkdir()
    here = at(data)
    assert stores.path_for(RENDER, here) == data / RENDER  # and the answer is now cached

    def another_process_writes(folder: Path) -> None:
        fresh = data / "written-elsewhere.json"
        fresh.write_text(json.dumps({"version": 1, "kinds": {"renders": str(folder)}}))
        os.replace(fresh, data / STORES_FILE)

    another_process_writes(nas / "aaa")
    assert stores.path_for(RENDER, here) == nas / "aaa" / "show.mp4"
    # Again at once, the same length: a coarse filesystem clock and an
    # unchanged size must not hide the second switch.
    another_process_writes(nas / "bbb")
    assert stores.path_for(RENDER, here) == nas / "bbb" / "show.mp4"


def test_the_record_is_written_whole_and_is_gone_when_it_says_nothing(tmp_path: Path) -> None:
    data = tmp_path / "data"
    stores.set_override(data, "proxies", tmp_path / "nas")
    stores.set_override(data, "renders", data / "renders")
    assert sorted(p.name for p in data.iterdir()) == [STORES_FILE]  # no half-written leftovers
    written = json.loads((data / STORES_FILE).read_text())
    # A folder inside the data store is kept relative to it, so it means the
    # same folder to a worker that mounts the store somewhere else.
    assert written["kinds"] == {"proxies": str(tmp_path / "nas"), "renders": "renders"}

    stores.clear_override(data, "proxies")
    stores.clear_override(data, "renders")
    assert not (data / STORES_FILE).exists()
    stores.clear_override(data, "renders")  # nothing to forget writes nothing
    assert not (data / STORES_FILE).exists()


def test_a_damaged_record_pauses_the_bulky_kinds_rather_than_guessing(tmp_path: Path) -> None:
    """Read as "nothing configured", a half-edited file would send proxies
    back to this machine's disk and report every one on the NAS missing."""
    data = tmp_path / "data"
    put(data, STORES_FILE, b'{"kinds": {"proxies": ')
    here = at(data)
    assert "cannot be read" in (stores.kind_problem("proxies", here) or "")
    assert "cannot be read" in (stores.write_problem("renders", here) or "")
    assert stores.path_for(THUMB, here) == data / THUMB  # thumbnails are not a kind
    with pytest.raises(stores.StoresFileError):
        stores.set_override(data, "renders", tmp_path / "nas")  # never overwritten blind
    assert (data / STORES_FILE).read_bytes() == b'{"kinds": {"proxies": '


def test_a_lock_left_by_a_process_that_died_does_not_hold_the_record_for_ever(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Changes are read-then-write, one writer at a time: a move's switch
    landing between another writer's two halves would be silently undone."""
    data, nas = tmp_path / "data", tmp_path / "nas"
    data.mkdir()
    lock = data / f"{STORES_FILE}.lock"
    lock.write_bytes(b"")  # somebody is writing
    monkeypatch.setattr(stores, "LOCK_WAIT_S", 0.05)
    with pytest.raises(stores.StoresFileError, match="in use"):
        stores.set_override(data, "renders", nas)

    long_ago = time.time() - stores.LOCK_STALE_S - 5
    os.utime(lock, (long_ago, long_ago))  # …or was, and died doing it
    stores.set_override(data, "renders", nas)
    assert stores.load_overrides(data) == {"renders": nas}
    assert not lock.exists()


# --- connected, or away ----------------------------------------------------------


@pytest.mark.parametrize("kind", ["renders", "exports", "basemaps"])
def test_a_store_without_its_marker_is_not_connected(tmp_path: Path, kind: str) -> None:
    data, nas = tmp_path / "data", tmp_path / "nas"
    (nas / kind).mkdir(parents=True)  # the empty folder an unmounted share leaves behind
    here = at(data, bulk_dir=nas)
    assert "not connected" in (stores.kind_problem(kind, here) or "")
    assert "not connected" in (stores.away(kind, here) or "")
    assert str(nas) not in (stores.away(kind, here) or "")  # no paths for people who are not admins

    stores.mark_bulk_root(nas)

    assert stores.kind_problem(kind, here) is None and stores.away(kind, here) is None
    # The folder itself is marked too, which is what keeps the scanner out of
    # all of it; and proxies keep the marker they have always had.
    assert (nas / BULK_STORE_MARKER).is_file()
    assert (nas / "proxies" / PROXY_STORE_MARKER).is_file()
    assert stores.kind_problem("proxies", here) is None


def test_nothing_is_created_in_a_store_that_is_away(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data, nas = tmp_path / "data", tmp_path / "nas"
    nas.mkdir()
    use(monkeypatch, data, nas)

    for kind in ("renders", "exports", "basemaps"):
        with pytest.raises(deriv.StoreUnavailable, match="not connected"):
            deriv.store_ready(kind)
    with pytest.raises(deriv.ProxyStoreUnavailable):
        deriv.store_ready("proxies")

    assert list(nas.iterdir()) == []  # not even the folders


def test_the_record_made_when_the_bulk_folder_was_set_outranks_the_setting(tmp_path: Path) -> None:
    """`FRAMEFOUND_BULK_DIR` is written at install for a person to read; what
    the services follow is the record, so that the folder can be changed from
    the Storage page with nothing restarted. The settings file says so."""
    data, in_the_file, recorded = (tmp_path / name for name in ("data", "old-nas", "new-nas"))
    here = at(data, bulk_dir=in_the_file)
    assert stores.bulk_root(here) == in_the_file

    stores.set_bulk_root(data, recorded)

    assert stores.bulk_root(here) == recorded
    assert stores.kind_root("renders", here) == recorded / "renders"
    template = envfile.initial(Layout(home=tmp_path / "FrameFound", source=tmp_path), "/pg/bin")
    assert "Do not change it by editing this line" in template


def test_writers_wait_out_a_move_instead_of_dropping_their_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    use(monkeypatch, data)
    stores.pause(data, "renders")
    assert "being moved" in (stores.write_problem("renders") or "")
    assert stores.kind_problem("renders") is None  # reading carries on throughout
    assert stores.write_problem("exports") is None  # and so does every other kind

    naps: list[float] = []

    def the_move_finishes(seconds: float) -> None:
        naps.append(seconds)
        stores.resume(data, "renders")

    deriv.store_ready("renders", sleep=the_move_finishes)  # waited, then went ahead
    assert len(naps) == 1

    # A move that never finishes is not waited for for ever…
    stores.pause(data, "renders")
    monkeypatch.setattr(deriv, "MOVE_WAIT_S", 10.0)
    with pytest.raises(deriv.StoreUnavailable, match="being moved"):
        deriv.store_ready("renders", sleep=lambda _seconds: None)
    # …and a pause nobody renews belongs to a move that died: it lapses.
    state = stores.read_state(data)
    assert state.is_paused("renders")
    assert not state.is_paused("renders", now=time.time() + stores.PAUSE_EXPIRES_S + 1)


def test_a_finished_file_only_ever_appears_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Onto another disk a file is copied under a `.partial` name and then
    renamed, so a share that drops mid-copy leaves nothing that looks like a
    finished video."""
    scratch, store = tmp_path / "scratch", tmp_path / "nas" / "renders"
    real_replace = os.replace

    def another_disk(source: Any, target: Any) -> None:
        if Path(source).parent == scratch:
            raise OSError(18, "Invalid cross-device link")
        real_replace(source, target)

    monkeypatch.setattr(stores.os, "replace", another_disk)
    stores.put_in_place(put(scratch, "final.mp4", b"whole"), store / "show.mp4")
    assert (store / "show.mp4").read_bytes() == b"whole"
    assert names(store) == ["show.mp4"] and names(scratch) == []

    def share_drops(source: Any, target: Any) -> None:
        Path(target).write_bytes(b"wh")
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(stores.shutil, "copyfile", share_drops)
    finished = put(scratch, "final.mp4", b"whole")
    with pytest.raises(OSError, match="Input/output"):
        stores.put_in_place(finished, store / "next.mp4")
    assert names(store) == ["show.mp4"]  # neither `next.mp4` nor half of it
    assert finished.read_bytes() == b"whole"  # still there to try again with


# --- the tasks and the pages -----------------------------------------------------


@pytest.fixture()
async def site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """An install whose bulk folder is on a share: `nas` is the folder the
    share mounts on, there but empty until a test connects it."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'catalogue.db').as_posix()}"
    data, nas = tmp_path / "data", tmp_path / "nas"
    data.mkdir()
    nas.mkdir()
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "bulk-store-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    use(monkeypatch, data, nas)

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        setup = await client.post(
            "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
        )
        assert setup.status_code == 201
        yield {"client": client, "factory": factory, "data": data, "nas": nas}
    await engine.dispose()


async def _slideshow(site: dict[str, Any], status: str = "ready") -> Slideshow:
    async with site["factory"]() as db:
        show = Slideshow(title="Open house", status=status, asset_ids=[])
        db.add(show)
        await db.flush()
        show.relative_path = f"renders/{show.id}.mp4" if status == "ready" else None
        await db.commit()
        return show


async def test_a_slideshow_on_a_store_that_is_away_says_so(site: dict[str, Any]) -> None:
    """Not "no longer on disk": that sends the operator off to render again
    a video that is sitting safely on a share."""
    client, nas = site["client"], site["nas"]
    show = await _slideshow(site)
    url = f"/api/v1/slideshows/{show.id}/video"

    away = await client.get(url)
    assert away.status_code == 503 and "not connected" in away.json()["error"]["message"]
    # Nor is it deleted while its video cannot be: the row would go and the
    # file would stay, known to nothing.
    assert (await client.delete(f"/api/v1/slideshows/{show.id}")).status_code == 503
    assert list(nas.iterdir()) == []

    stores.mark_bulk_root(nas)
    assert (await client.get(url)).status_code == 404  # connected, and really not there
    put(nas, f"renders/{show.id}.mp4", b"video")
    served = await client.get(url)
    assert served.status_code == 200 and served.content == b"video"

    assert (await client.delete(f"/api/v1/slideshows/{show.id}")).status_code == 204
    assert not (nas / "renders" / f"{show.id}.mp4").exists()


async def test_an_export_on_a_store_that_is_away_says_so(site: dict[str, Any]) -> None:
    from framefound.media.export_state import listing_fingerprint

    client, nas = site["client"], site["nas"]
    async with site["factory"]() as db:
        listing = Listing(name="12 Maple St", export_status="ready")
        db.add(listing)
        await db.flush()
        listing.export_relpath = f"exports/listings/{listing.id}.zip"
        listing.export_fingerprint = await listing_fingerprint(db, listing.id)
        await db.commit()
    url = f"/api/v1/listings/{listing.id}/export/download"

    away = await client.get(url)
    assert away.status_code == 503 and "not connected" in away.json()["error"]["message"]
    assert (await client.delete(f"/api/v1/listings/{listing.id}")).status_code == 503

    stores.mark_bulk_root(nas)
    put(nas, f"exports/listings/{listing.id}.zip", b"PK")
    assert (await client.get(url)).content == b"PK"


async def test_the_basemap_page_still_loads_with_its_store_away(site: dict[str, Any]) -> None:
    client, nas = site["client"], site["nas"]

    listed = await client.get("/api/v1/basemaps")
    assert listed.status_code == 200
    assert listed.json()["installed_count"] == 0 and "not connected" in listed.json()["note"]
    for response in (
        await client.get("/api/v1/basemaps/pennsylvania/tiles/0/0/0.mvt"),
        await client.get("/api/v1/basemaps/pennsylvania/style.json"),
        await client.post("/api/v1/basemaps/download", json={"name": "pennsylvania"}),
        await client.delete("/api/v1/basemaps/pennsylvania"),
    ):
        assert (
            response.status_code == 503 and "not connected" in response.json()["error"]["message"]
        )
    # The page used to `mkdir` its folder on every visit — on an unmounted
    # share, that is the first file of a "store" on this machine's own disk.
    assert list(nas.iterdir()) == []

    stores.mark_bulk_root(nas)
    put(nas, "basemaps/pennsylvania.pmtiles", b"x" * 2048)
    listed = await client.get("/api/v1/basemaps")
    assert [b["name"] for b in listed.json()["basemaps"] if b["installed"]] == ["pennsylvania"]


async def test_a_basemap_on_a_share_is_never_memory_mapped(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share that drops under an open memory map does not fail one tile:
    it kills the API (SIGBUS). A store of its own is read with plain seeks."""
    import gzip

    import pmtiles.reader

    tile = gzip.compress(b"a vector tile")  # served as stored: gzipped
    archive = b"head" + tile + b"tail"

    class OneTile:
        """Stands in for the archive's directory: every tile is those bytes."""

        def __init__(self, get_bytes: Callable[[int, int], bytes]) -> None:
            self.get_bytes = get_bytes

        def get(self, z: int, x: int, y: int) -> bytes:
            return self.get_bytes(4, len(tile))

    mapped: list[bool] = []
    real_mmap = pmtiles.reader.MmapSource

    def mmap_source(handle: Any) -> Any:
        mapped.append(True)
        return real_mmap(handle)

    monkeypatch.setattr(pmtiles.reader, "Reader", OneTile)
    monkeypatch.setattr(pmtiles.reader, "MmapSource", mmap_source)
    url = "/api/v1/basemaps/pennsylvania/tiles/0/0/0.mvt"

    stores.mark_bulk_root(site["nas"])
    put(site["nas"], "basemaps/pennsylvania.pmtiles", archive)
    assert (await site["client"].get(url)).content == b"a vector tile"
    assert mapped == []

    # On this machine's own disk the quick way is still the way.
    stores.set_override(site["data"], "basemaps", site["data"] / "basemaps")
    put(site["data"], "basemaps/pennsylvania.pmtiles", archive)
    assert (await site["client"].get(url)).content == b"a vector tile"
    assert mapped == [True]


async def test_a_render_with_its_store_away_stops_with_a_reason_and_no_traceback(
    site: dict[str, Any],
) -> None:
    from framefound.processing.tasks import render_slideshow

    show = await _slideshow(site, status="pending")

    await asyncio.to_thread(render_slideshow, str(show.id))  # does not raise

    async with site["factory"]() as db:
        after = await db.get(Slideshow, show.id)
        # "Failed" is the status the page shows a reason for and offers
        # Render again on; the reason says what to do.
        assert after.status == "failed" and after.relative_path is None
        assert "not connected" in after.error and "render the slideshow again" in after.error
    assert list(site["nas"].iterdir()) == []


async def test_an_export_with_its_store_away_stops_with_a_reason(site: dict[str, Any]) -> None:
    from framefound.processing.tasks import export_listing_zip

    async with site["factory"]() as db:
        listing = Listing(name="12 Maple St", export_status="queued")
        db.add(listing)
        await db.commit()

    await asyncio.to_thread(export_listing_zip, str(listing.id))

    async with site["factory"]() as db:
        after = await db.get(Listing, listing.id)
        assert after.export_status == "failed" and after.export_relpath is None
        assert "not connected" in after.export_error and "export again" in after.export_error
    assert list(site["nas"].iterdir()) == []
    assert names(site["data"] / "tmp") == []  # and no half-made zip left in scratch


async def test_a_basemap_that_finished_while_its_store_was_away_is_kept_for_it(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An extract runs for hours and the share only has to be off for the
    minute it ends in. The finished file used to be deleted, with a line in
    a log; a basemap has no row to say why it never appeared."""
    from framefound.processing.tasks import fetch_basemap

    client, data, nas = site["client"], site["data"], site["nas"]
    stores.mark_bulk_root(nas)
    extractions: list[str] = []
    queued: list[tuple[str, ...]] = []

    def pmtiles_extract(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        extractions.append(argv[3])
        Path(argv[3]).write_bytes(b"x" * 4096)
        (nas / "basemaps" / BULK_STORE_MARKER).unlink()  # …and the share went while it ran
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/pmtiles")
    monkeypatch.setattr(subprocess, "run", pmtiles_extract)
    monkeypatch.setattr(fetch_basemap, "delay", lambda *args: queued.append(args))

    await asyncio.to_thread(fetch_basemap, "pennsylvania", "-80.9,39.4,-74.4,42.5")  # no raise

    assert names(data / "tmp") == ["basemap-pennsylvania.pmtiles"]  # whole, and kept
    assert names(nas / "basemaps") == []
    away = (await client.get("/api/v1/basemaps")).json()
    assert "pennsylvania finished downloading" in away["note"] and "not connected" in away["note"]

    stores.mark_bulk_root(nas)  # the share is back
    back = (await client.get("/api/v1/basemaps")).json()
    assert back["installed_count"] == 0 and "Choose Download again" in back["note"]
    asked = await client.post("/api/v1/basemaps/download", json={"name": "pennsylvania"})
    assert asked.status_code == 202 and queued == [("pennsylvania", "-80.9,39.4,-74.4,42.5")]
    await asyncio.to_thread(fetch_basemap, *queued[0])  # the worker's turn

    assert len(extractions) == 1, "four hours of range requests were made twice"
    assert names(nas / "basemaps") == [BULK_STORE_MARKER, "pennsylvania.pmtiles"]
    assert names(data / "tmp") == []
    listed = (await client.get("/api/v1/basemaps")).json()
    assert listed["installed_count"] == 1 and "finished downloading" not in listed["note"]


async def test_deleting_a_basemap_also_forgets_one_waiting_to_be_put_in_place(
    site: dict[str, Any],
) -> None:
    stores.mark_bulk_root(site["nas"])
    put(site["data"], "tmp/basemap-ohio.pmtiles", b"x" * 2048)

    assert (await site["client"].delete("/api/v1/basemaps/ohio")).status_code == 204

    assert names(site["data"] / "tmp") == []
    listed = (await site["client"].get("/api/v1/basemaps")).json()
    assert "finished downloading" not in listed["note"]


def test_a_basemap_is_extracted_on_this_machine_and_arrives_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from framefound.processing.tasks import fetch_basemap

    data, nas = tmp_path / "data", tmp_path / "nas"
    nas.mkdir()
    use(monkeypatch, data, nas)
    extracted_to: list[Path] = []

    def pmtiles_extract(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        extracted_to.append(Path(argv[3]))
        Path(argv[3]).write_bytes(b"x" * 4096)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/pmtiles")
    monkeypatch.setattr(subprocess, "run", pmtiles_extract)

    fetch_basemap("pennsylvania", "-80.9,39.4,-74.4,42.5")  # its store is away: waits, quietly
    assert extracted_to == [] and list(nas.iterdir()) == []

    stores.mark_bulk_root(nas)
    fetch_basemap("pennsylvania", "-80.9,39.4,-74.4,42.5")
    # Four hours of range requests are written to this machine's own disk;
    # the share sees one finished file.
    assert extracted_to[0].parent == data / "tmp"
    assert names(nas / "basemaps") == [BULK_STORE_MARKER, "pennsylvania.pmtiles"]
    assert names(data / "tmp") == []


# --- a proxy that was being made while proxies moved --------------------------


async def test_a_proxy_finished_after_the_move_follows_the_others(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcode that had not written a byte looks finished to a move.
    Recorded as ready where it was made, it would be a proxy that never plays."""
    data, ssd = site["data"], site["nas"].parent / "ssd"
    stores.set_override(data, "proxies", data)  # proxies start on this machine
    async with site["factory"]() as db:
        library = Library(name="L", root_path=str(site["nas"].parent))
        db.add(library)
        await db.flush()
        asset = Asset(
            library_id=library.id,
            relative_path="clip.mp4",
            filename="clip.mp4",
            extension="mp4",
            media_type="video",
            size_bytes=1,
            mtime=datetime.now(UTC),
        )
        db.add(asset)
        await db.commit()

        def transcode(_source: Path, target: Path, _height: int) -> str:
            target.write_bytes(b"proxy")
            stores.mark_proxy_store(ssd)  # …and meanwhile the move finished
            stores.set_override(data, "proxies", ssd)
            return "libx264"

        monkeypatch.setattr(deriv.ffmpeg, "transcode_proxy", transcode)
        await deriv.generate_video_proxy(db, data, asset, library, Path("clip.mp4"))

        relative = deriv.derivative_relpath(asset.id, "proxy", "mp4")
        assert (ssd / relative).read_bytes() == b"proxy"
        assert not (data / relative).exists()
        record = (await db.execute(Derivative.__table__.select())).one()
        assert record.status == "ready" and record.size_bytes == 5


# --- moving a kind ---------------------------------------------------------------


def _install(tmp_path: Path) -> tuple[Settings, Path, Path]:
    """Files of every kind on this machine, and a share to move them to."""
    data, nas = tmp_path / "data", tmp_path / "Photos"
    for relative in (PROXY, THUMB, RENDER, "renders/other.mp4", EXPORT, BASEMAP):
        put(data, relative, relative.encode())
    put(data, "renders/work/show/0000-body0.mp4", b"scratch")
    put(data, DATA_STORE_MARKER, b"")
    nas.mkdir()
    return at(data), data, nas


def test_moving_a_kind_copies_checks_marks_and_switches(tmp_path: Path) -> None:
    here, data, nas = _install(tmp_path)
    target = nas / "FrameFound renders"
    phases: list[str] = []

    result = moves.move(
        "renders", target, settings=here, report=lambda p: phases.append(p.phase), **QUICK
    )

    assert names(target) == [BULK_STORE_MARKER, "other.mp4", "show.mp4"]  # not the scratch
    assert (target / "show.mp4").read_bytes() == RENDER.encode()
    assert (result.files, result.copied) == (2, 2)
    # Every process follows at its next lookup: no restart, no setting.
    assert stores.path_for(RENDER, here) == target / "show.mp4"
    assert stores.kind_problem("renders", here) is None
    assert stores.write_problem("renders", here) is None  # the pause was lifted
    # The originals of the move are left for the operator to remove.
    assert (data / RENDER).is_file()
    assert stores.read_state(data).previous == {"renders": data / "renders"}
    # A page can show how far it has got.
    assert [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]] == [
        "copying",
        "finishing",
        "switching",
        "done",
    ]
    # Nothing else moved.
    assert stores.path_for(EXPORT, here) == data / EXPORT
    assert stores.path_for(PROXY, here) == data / PROXY


def test_moving_proxies_takes_only_proxies_and_keeps_their_paths(tmp_path: Path) -> None:
    here, data, nas = _install(tmp_path)
    target = nas / "Framefound" / "proxies"
    target.parent.mkdir()

    moves.move("proxies", target, settings=here, **QUICK)

    assert names(target) == [PROXY_STORE_MARKER, PROXY]  # the thumbnail beside it stays
    assert stores.path_for(PROXY, here) == target / PROXY
    assert stores.separate_proxy_store(here) == target  # the older names agree
    assert stores.proxy_store_problem(here) is None


def test_a_move_that_did_not_copy_everything_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    here, data, nas = _install(tmp_path)
    target = nas / "exports"
    monkeypatch.setattr(moves, "_copy_file", lambda *_args: None)  # a copy that copied nothing

    with pytest.raises(moves.MoveError, match="0 of 1"):
        moves.move("exports", target, settings=here, **QUICK)

    assert stores.path_for(EXPORT, here) == data / EXPORT
    assert stores.load_overrides(data) == {}
    assert not (target / BULK_STORE_MARKER).exists()  # not a store: it was never checked
    assert stores.write_problem("exports", here) is None  # and exports are not left paused


def test_an_interrupted_move_carries_on_where_it_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    here, data, nas = _install(tmp_path)
    target = nas / "renders"
    real_copy = moves._copy_file
    copied: list[str] = []

    def share_drops_after_one(source: Path, *rest: Any) -> None:
        if copied:
            raise OSError(5, "Input/output error")
        copied.append(source.name)
        real_copy(source, *rest)

    monkeypatch.setattr(moves, "_copy_file", share_drops_after_one)
    with pytest.raises(moves.MoveError, match="Nothing was changed"):
        moves.move("renders", target, settings=here, **QUICK)
    assert stores.path_for(RENDER, here) == data / RENDER
    # Half a copy is not a store, but it is already FrameFound's: the
    # scanner keeps out of it (test_proxy_store.py).
    assert (target / INCOMING_MARKER).is_file() and not (target / BULK_STORE_MARKER).exists()

    monkeypatch.setattr(moves, "_copy_file", real_copy)
    result = moves.move("renders", target, settings=here, **QUICK)

    assert (result.files, result.copied) == (2, 1)  # only what was missing
    assert names(target) == [BULK_STORE_MARKER, "other.mp4", "show.mp4"]


def test_files_made_during_the_long_copy_are_brought_too(tmp_path: Path) -> None:
    here, data, nas = _install(tmp_path)
    seen = 0

    def a_render_finishes_meanwhile(progress: moves.Progress) -> None:
        nonlocal seen
        seen += 1
        if progress.phase == "copying" and seen == 2:  # after the folder was listed
            put(data, "renders/late.mp4", b"made during the copy")

    moves.move(
        "renders", nas / "renders", settings=here, report=a_render_finishes_meanwhile, **QUICK
    )

    assert (nas / "renders" / "late.mp4").read_bytes() == b"made during the copy"


def test_a_file_still_being_written_stops_the_move_with_nothing_changed(tmp_path: Path) -> None:
    """A proxy transcode can run for hours. The move waits, then gives up
    cleanly rather than switch to a folder holding half of one."""
    here, data, nas = _install(tmp_path)

    def still_transcoding(_seconds: float) -> None:
        with (data / PROXY).open("ab") as proxy:
            proxy.write(b"more")

    with pytest.raises(moves.MoveError, match="still being written"):
        moves.move(
            "proxies", nas / "proxies", settings=here, wait_s=0, settle_s=0, sleep=still_transcoding
        )

    assert stores.path_for(PROXY, here) == data / PROXY
    assert stores.write_problem("proxies", here) is None  # new proxies carry on


def test_a_store_that_is_away_cannot_be_copied_from_but_can_be_replaced(tmp_path: Path) -> None:
    data, old, new = tmp_path / "data", tmp_path / "old-drive", tmp_path / "new-drive"
    old.mkdir()  # unplugged: folder there, marker not
    stores.set_override(data, "renders", old)
    here = at(data)

    with pytest.raises(moves.MoveError, match="cannot be copied from"):
        moves.move("renders", new, settings=here, **QUICK)
    assert stores.kind_root("renders", here) == old

    moves.move("renders", new, settings=here, mode="empty", **QUICK)
    assert stores.kind_root("renders", here) == new and stores.kind_problem("renders", here) is None


def test_asking_twice_never_marks_a_folder_that_lost_its_marker(tmp_path: Path) -> None:
    """A store without its marker is a share that is not mounted. "Move it
    to where it already is" must not bless the empty folder left behind."""
    here, _data, nas = _install(tmp_path)
    target = nas / "renders"
    moves.move("renders", target, settings=here, **QUICK)
    assert moves.move("renders", target, settings=here, **QUICK).copied == 0  # harmless

    shutil.rmtree(target)
    target.mkdir()  # unmounted: what answers now is the folder it mounts on

    with pytest.raises(moves.MoveError, match="not connected"):
        moves.move("renders", target, settings=here, **QUICK)
    assert not (target / BULK_STORE_MARKER).exists()
    assert "not connected" in (stores.kind_problem("renders", here) or "")


def test_moving_back_onto_this_machine_needs_no_marker(tmp_path: Path) -> None:
    here, data, nas = _install(tmp_path)
    moves.move("basemaps", nas / "basemaps", settings=here, **QUICK)
    put(nas, "basemaps/us.pmtiles", b"downloaded since")

    moves.move("basemaps", data / "basemaps", settings=here, **QUICK)

    assert stores.path_for("basemaps/us.pmtiles", here) == data / "basemaps" / "us.pmtiles"
    assert (data / "basemaps" / "us.pmtiles").read_bytes() == b"downloaded since"
    assert not stores.is_separate("basemaps", here)
    assert not any((data / "basemaps" / marker).exists() for marker in stores.STORE_MARKERS)


def test_a_replacement_drive_under_the_old_ones_name_can_be_started_empty(tmp_path: Path) -> None:
    """docs/storage.md: a store that is gone for good is replaced by starting
    the new one empty. A new drive given the dead one's name mounts at the
    same path, and that used to answer "already live in … Reconnect the
    drive" for ever."""
    here, _data, nas = _install(tmp_path)
    store = nas / "Fast SSD" / "renders"
    store.parent.mkdir()
    moves.move("renders", store, settings=here, **QUICK)
    shutil.rmtree(store.parent)
    store.parent.mkdir()  # the drive died; its replacement has the same name
    assert "not connected" in (stores.kind_problem("renders", here) or "")

    # A copy is still not what to ask for, and the refusal says what is.
    with pytest.raises(moves.MoveError, match="start the folder empty"):
        moves.move("renders", store, settings=here, **QUICK)
    assert not store.exists() or names(store) == []

    looked = moves.preflight("renders", store, settings=here, mode="empty", confirmed=True)
    assert not looked.already  # the page's check agrees with the move
    moves.move("renders", store, settings=here, mode="empty", **QUICK)

    assert stores.kind_root("renders", here) == store
    assert stores.kind_problem("renders", here) is None
    assert names(store) == [BULK_STORE_MARKER]


# --- the same folder under another name ----------------------------------------------


def another_name_for(folder: Path, alias: Path) -> None:
    """Give `folder` a second name. On a Mac that is nothing more than
    another capitalisation (`Framefound`, `FrameFound`): the volumes ignore
    case and Python's paths do not. A symlink is the same thing anywhere."""
    try:
        alias.symlink_to(folder, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine will not make a symlink")


def test_a_kind_is_not_moved_to_the_folder_it_is_in_under_another_name(tmp_path: Path) -> None:
    """ "Copied" onto itself every file is already there, the move succeeds,
    and the old spelling is then offered for removal as the old copies."""
    here, data, nas = _install(tmp_path)
    live = nas / "Framefound" / "renders"
    live.parent.mkdir()
    moves.move("renders", live, settings=here, **QUICK)
    moves.remove_old_copies("renders", settings=here)  # the share now holds the only copies
    another_name_for(nas / "Framefound", nas / "Frame-Found")
    respelt = nas / "Frame-Found" / "renders"

    for ask in (moves.move, moves.preflight):
        with pytest.raises(moves.MoveError, match="already live in"):
            ask("renders", respelt, settings=here)

    assert stores.kind_root("renders", here) == live
    assert stores.read_state(data).previous == {}  # nothing to offer for removal
    assert names(live) == [BULK_STORE_MARKER, "other.mp4", "show.mp4"]


def test_old_copies_are_not_removed_from_the_folder_in_use_under_another_name(
    tmp_path: Path,
) -> None:
    """Whatever recorded it, a folder is never its own old copy: every file
    in it "has a copy in the folder in use"."""
    here, data, nas = _install(tmp_path)
    live = nas / "renders"
    moves.move("renders", live, settings=here, **QUICK)
    alias = nas / "Renders (old)"
    another_name_for(live, alias)
    stores.set_previous(data, "renders", alias)  # as a move made before the check above left it

    with pytest.raises(moves.MoveError, match="same folder under two names"):
        moves.remove_old_copies("renders", settings=here)
    assert stores.read_state(data).previous == {}  # and it is not offered again
    with pytest.raises(moves.MoveError, match="same folder under two names"):
        moves.remove_old_copies("renders", settings=here, old_root=alias, everything=True)

    assert names(live) == [BULK_STORE_MARKER, "other.mp4", "show.mp4"]


def test_one_share_mounted_twice_is_still_one_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """macOS mounts a share again as `/Volumes/Photos-1`. The two mounts are
    two devices to `stat`, so only a file seen through both names tells."""
    photos, again, elsewhere = (tmp_path / name for name in ("Photos", "Photos-1", "Other"))
    put(photos, "kept.txt")
    elsewhere.mkdir()
    another_name_for(photos, again)
    monkeypatch.setattr(stores.os.path, "samefile", lambda _one, _other: False)

    assert stores.same_folder(photos, again)
    assert not stores.same_folder(photos, elsewhere)
    assert not stores.same_folder(photos, tmp_path / "not there")
    # The look leaves nothing behind in either.
    assert names(photos) == ["kept.txt"] and names(elsewhere) == []


# --- room for it -------------------------------------------------------------------


def test_a_move_that_will_not_fit_is_refused_before_a_byte_is_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every other writer stops short of filling a disk, because the data
    folder shares its disk with the catalogue. A move wrote until the disk
    refused, and then left what it had copied there."""
    here, data, nas = _install(tmp_path)
    floor = int(here.min_free_gb * 1024**3)
    free = {"bytes": floor + 1}  # room for nothing above what FrameFound leaves
    monkeypatch.setattr(moves, "_free_bytes", lambda _folder: free["bytes"])
    target = nas / "renders"

    with pytest.raises(moves.MoveError, match="not enough room") as refused:
        moves.move("renders", target, settings=here, **QUICK)
    assert "Nothing was changed" in str(refused.value) and str(target) in str(refused.value)
    assert names(target) == [INCOMING_MARKER]  # checked, and nothing copied
    assert stores.kind_root("renders", here) == data / "renders"
    assert stores.write_problem("renders", here) is None
    # The page says so at the click, not in a progress row a minute later…
    with pytest.raises(moves.MoveError, match="not enough room"):
        moves.preflight("renders", target, settings=here, confirmed=True)
    # …for proxies too, from what the catalogue says they take, since a
    # folder per clip on a share cannot be counted inside a request.
    assert moves.room_problem("proxies", nas / "proxies", here) is None  # cannot tell
    assert "not enough room" in (
        moves.room_problem("proxies", nas / "proxies", here, known_bytes=10**9) or ""
    )

    free["bytes"] = floor + 10**9
    moves.move("renders", target, settings=here, **QUICK)
    moves.remove_old_copies("renders", settings=here)

    # Back onto this machine's own disk — the one the catalogue is on — is
    # the move that matters most, and the one no other check looked at.
    free["bytes"] = floor + 1
    with pytest.raises(moves.MoveError, match="not enough room"):
        moves.move("renders", data / "renders", settings=here, **QUICK)
    assert names(data / "renders") == ["work/show/0000-body0.mp4"]
    assert stores.kind_root("renders", here) == target


def test_a_disk_that_fills_during_the_copy_stops_it_short_of_full(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    here, data, nas = _install(tmp_path)
    floor = int(here.min_free_gb * 1024**3)
    looks = iter([floor * 2])  # room when the pass began…

    monkeypatch.setattr(moves, "_free_bytes", lambda _folder: next(looks, floor - 1))  # …then not

    with pytest.raises(moves.MoveError, match="is down to"):
        moves.move("renders", nas / "renders", settings=here, **QUICK)

    assert names(nas / "renders") == [INCOMING_MARKER]  # not even half a file left behind
    assert stores.kind_root("renders", here) == data / "renders"
    assert stores.write_problem("renders", here) is None


# --- what the last pass must not be fooled by ----------------------------------------


def _on_a_share(tmp_path: Path) -> tuple[Settings, Path, Path, Path]:
    """Renders that already live on a share, and a second share to move
    them to."""
    data, old, new = (tmp_path / name for name in ("data", "old-share", "new-share"))
    data.mkdir()
    stores.mark_store(old, BULK_STORE_MARKER, kind="renders")
    for name in ("show.mp4", "other.mp4"):
        put(old, name, name.encode())
    stores.set_override(data, "renders", old)
    return at(data), data, old, new


def test_an_old_store_that_drops_at_the_end_is_not_read_as_nothing_left(tmp_path: Path) -> None:
    """An unmounted share lists as an empty folder. Taken at its word, the
    last look finds nothing still to copy and the move switches to a folder
    holding whatever had been copied by then."""
    here, _data, old, new = _on_a_share(tmp_path)

    def the_share_drops(progress: moves.Progress) -> None:
        if progress.phase == "finishing":
            for left in old.iterdir():  # what answers now is the folder it mounted on
                left.unlink()

    with pytest.raises(moves.MoveError, match="went away"):
        moves.move("renders", new, settings=here, report=the_share_drops, **QUICK)

    assert stores.kind_root("renders", here) == old
    assert not (new / BULK_STORE_MARKER).exists()  # never became a store
    assert not stores.read_state(here.data_dir).is_paused("renders")


def test_a_new_store_that_drops_mid_copy_has_nothing_more_written_in_its_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Where the folder a share mounts on outlives the mount, the copy would
    carry on into it: onto this machine's own disk, until that was full."""
    here, data, nas = _install(tmp_path)
    target = nas / "renders"
    real_copy = moves._copy_file

    def copy_and_then_the_share_drops(source: Path, *rest: Any) -> None:
        real_copy(source, *rest)
        shutil.rmtree(target)
        target.mkdir()

    monkeypatch.setattr(moves, "_copy_file", copy_and_then_the_share_drops)
    with pytest.raises(moves.MoveError, match="went away"):
        moves.move("renders", target, settings=here, **QUICK)

    assert list(target.iterdir()) == []
    assert stores.kind_root("renders", here) == data / "renders"


def test_a_file_still_being_put_in_place_holds_the_switch(tmp_path: Path) -> None:
    """A render being copied into the old store when the pause began is
    there under a `.partial` name. Not counted as something still being
    written, the move switches, the writer renames into the old folder, and
    the slideshow is "ready" with its video where nothing looks."""
    here, data, nas = _install(tmp_path)
    arriving = data / "renders" / "late.mp4.partial"

    def still_arriving(_seconds: float) -> None:
        with arriving.open("ab") as partial:
            partial.write(b"more")

    with pytest.raises(moves.MoveError, match="still being written"):
        moves.move(
            "renders", nas / "renders", settings=here, wait_s=0, settle_s=0, sleep=still_arriving
        )

    assert stores.kind_root("renders", here) == data / "renders"
    assert names(nas / "renders") == [INCOMING_MARKER, "other.mp4", "show.mp4"]


# --- deleted while it moved ----------------------------------------------------------


def test_a_file_deleted_while_its_kind_is_being_moved_does_not_come_back(tmp_path: Path) -> None:
    """The copy takes hours and only its last pass refuses a delete. A
    basemap deleted on the Basemaps page meanwhile had already been copied,
    and was installed again by the switch."""
    here, data, nas = _install(tmp_path)
    put(data, "basemaps/ohio.pmtiles", b"ohio")
    target = nas / "basemaps"

    def deleted_on_the_basemaps_page(progress: moves.Progress) -> None:
        if progress.phase == "finishing":  # both have been copied by now
            (data / "basemaps" / "ohio.pmtiles").unlink(missing_ok=True)

    result = moves.move(
        "basemaps", target, settings=here, report=deleted_on_the_basemaps_page, **QUICK
    )

    assert names(target) == [BULK_STORE_MARKER, "pennsylvania.pmtiles"]
    assert result.files == 1


def test_a_copy_an_interrupted_move_left_of_a_file_since_deleted_is_taken_back_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    here, data, nas = _install(tmp_path)
    put(data, "basemaps/ohio.pmtiles", b"ohio")  # copied first: before "pennsylvania"
    target = nas / "basemaps"
    real_copy = moves._copy_file
    copied: list[str] = []

    def share_drops_after_one(source: Path, *rest: Any) -> None:
        if copied:
            raise OSError(5, "Input/output error")
        copied.append(source.name)
        real_copy(source, *rest)

    monkeypatch.setattr(moves, "_copy_file", share_drops_after_one)
    with pytest.raises(moves.MoveError):
        moves.move("basemaps", target, settings=here, **QUICK)
    assert copied == ["ohio.pmtiles"] and (target / "ohio.pmtiles").is_file()

    (data / "basemaps" / "ohio.pmtiles").unlink()  # deleted before the move was started again
    monkeypatch.setattr(moves, "_copy_file", real_copy)
    moves.move("basemaps", target, settings=here, **QUICK)

    assert names(target) == [BULK_STORE_MARKER, "pennsylvania.pmtiles"]


def test_files_already_in_a_folder_the_kind_used_before_are_never_taken_out(
    tmp_path: Path,
) -> None:
    """Only this move's own copies are taken back. What a folder already
    held may be the only copy of something."""
    here, _data, nas = _install(tmp_path)
    used_before = nas / "basemaps"
    stores.mark_store(used_before, BULK_STORE_MARKER, kind="basemaps")
    put(used_before, "europe.pmtiles", b"from when basemaps lived here")

    moves.move("basemaps", used_before, settings=here, **QUICK)

    assert names(used_before) == [BULK_STORE_MARKER, "europe.pmtiles", "pennsylvania.pmtiles"]


async def test_a_basemap_deleted_after_a_move_is_not_brought_back_by_a_move_back(
    site: dict[str, Any],
) -> None:
    """Old copies are kept until the operator removes them, and a move back
    onto them makes them the live files again — with everything deleted in
    between. So a delete takes the old copy as well."""
    client, data, nas = site["client"], site["data"], site["nas"]
    stores.mark_bulk_root(nas)
    stores.set_override(data, "basemaps", data / "basemaps")  # they start on this machine
    for name in ("ohio", "pennsylvania"):
        put(data, f"basemaps/{name}.pmtiles", name.encode() * 400)
    await asyncio.to_thread(lambda: moves.move("basemaps", nas / "basemaps", **QUICK))

    assert (await client.delete("/api/v1/basemaps/ohio")).status_code == 204

    assert not (nas / "basemaps" / "ohio.pmtiles").exists()
    assert not (data / "basemaps" / "ohio.pmtiles").exists()  # its old copy went with it
    assert (data / "basemaps" / "pennsylvania.pmtiles").is_file()  # the other is untouched
    await asyncio.to_thread(lambda: moves.move("basemaps", data / "basemaps", **QUICK))
    listed = (await client.get("/api/v1/basemaps")).json()
    assert [b["name"] for b in listed["basemaps"] if b["installed"]] == ["pennsylvania"]


# --- one move at a time ----------------------------------------------------------------


def test_one_move_runs_at_a_time_whoever_starts_it(tmp_path: Path) -> None:
    """The page's job and `framefound storage move` are two processes. Each
    lifted the pause the other was relying on for its last pass, because
    only the page's turn-taking was written down, in the database."""
    here, data, nas = _install(tmp_path)
    asked: list[str] = []

    def a_second_move_is_asked_for(progress: moves.Progress) -> None:
        if progress.phase != "copying" or asked:
            return
        asked.append("asked")
        held = moves.active_move(here)
        assert held is not None and (held.kind, held.target) == ("renders", str(nas / "renders"))
        with pytest.raises(moves.MoveError, match="Slideshow videos are being moved"):
            moves.move("exports", nas / "exports", settings=here, **QUICK)
        with pytest.raises(moves.MoveError, match="being moved"):
            moves.adopt_bulk_root(nas / "Framefound", settings=here)
        with pytest.raises(moves.MoveError, match="Nothing was removed"):
            moves.remove_old_copies("renders", settings=here, old_root=nas / "elsewhere")

    moves.move(
        "renders", nas / "renders", settings=here, report=a_second_move_is_asked_for, **QUICK
    )

    assert asked and moves.active_move(here) is None
    assert stores.kind_root("exports", here) == data / "exports" and stores.bulk_root(here) is None
    moves.move("exports", nas / "exports", settings=here, **QUICK)  # and then it is its turn


def test_a_move_that_was_killed_does_not_hold_the_turn_for_ever(tmp_path: Path) -> None:
    here, data, nas = _install(tmp_path)
    turn = data / moves.MOVE_LOCK
    turn.write_text(
        json.dumps({"kind": "proxies", "target": "/Volumes/x", "since": 1.0, "token": "dead"})
    )
    with pytest.raises(moves.MoveError, match="Proxies are being moved to /Volumes/x"):
        moves.move("renders", nas / "renders", settings=here, **QUICK)

    long_ago = time.time() - moves.LOCK_STALE_S - 5
    os.utime(turn, (long_ago, long_ago))  # nobody has renewed it: its process is gone
    assert moves.active_move(here) is None
    moves.move("renders", nas / "renders", settings=here, **QUICK)

    assert stores.kind_root("renders", here) == nas / "renders"
    assert not turn.exists()


@pytest.mark.parametrize("other", ["exports", "renders"])
def test_a_move_whose_turn_was_taken_stops_instead_of_switching_as_well(
    tmp_path: Path, other: str
) -> None:
    """A Mac that slept through the copy, a share that hung for minutes: the
    move was taken for dead and another began. Waking, it must not finish —
    nor lift the pause, if the other move is of the same kind and needs it."""
    here, data, nas = _install(tmp_path)
    turn = data / moves.MOVE_LOCK
    theirs = json.dumps({"kind": other, "target": "/x", "since": 2.0, "token": "another"})

    def another_move_took_its_place(progress: moves.Progress) -> None:
        if progress.phase == "finishing":
            turn.write_text(theirs)

    with pytest.raises(moves.MoveError, match="another was allowed to start"):
        moves.move(
            "renders", nas / "renders", settings=here, report=another_move_took_its_place, **QUICK
        )

    assert stores.kind_root("renders", here) == data / "renders"
    assert stores.read_state(data).is_paused("renders") == (other == "renders")
    assert turn.read_text() == theirs  # and the other's turn is left alone


# --- which folders will do ---------------------------------------------------------


def test_a_kind_is_never_moved_somewhere_it_must_not_go(tmp_path: Path) -> None:
    here, data, nas = _install(tmp_path)

    def refused(target: Path, kind: str = "renders", **more: Any) -> str:
        with pytest.raises(moves.MoveError) as refusal:
            moves.validate_target(kind, target, here, **more)
        return str(refusal.value)

    assert "full path" in refused(Path("renders"))
    # `/Volumes/Fast SSD/Renders` with the SSD unplugged: creating the
    # parents would put the "store" on this machine's own disk.
    assert "Connect the drive" in refused(tmp_path / "Fast SSD" / "Renders")
    assert "whole disk" in refused(Path(tmp_path.anchor))
    assert "own data folder" in refused(data.parent)
    assert "inside FrameFound's own data folder" in refused(data / "elsewhere")
    # The model library moves like any other kind now (L1); its own rules
    # are tests/test_local_models_library.py's.
    moves.validate_target("models", nas / "models", here)

    # Somebody's files. Marked as a store, every scan would skip them.
    put(nas, "Wedding/clip.mp4")
    assert "did not put there" in refused(nas / "Wedding")
    # The same folder as the catalogue sees it: footage, however it looks.
    assert "catalogue lists as footage" in refused(nas / "Empty", catalogued=3)
    # Another FrameFound store.
    stores.mark_proxy_store(nas / "Proxies")
    assert "something else" in refused(nas / "Proxies")
    put(nas, f"Old data/{DATA_STORE_MARKER}")
    assert "data store" in refused(nas / "Old data")
    # The bulk folder itself: a kind goes in its own folder inside it.
    stores.mark_bulk_root(nas / "Framefound")
    with_bulk = at(data, bulk_dir=nas / "Framefound")
    with pytest.raises(moves.MoveError, match="own folder inside it"):
        moves.validate_target("renders", nas / "Framefound", with_bulk)

    # What will do: a new folder, one Finder has looked at, one this kind
    # already used, and the kind's own place on this machine.
    put(nas, "Looked at/.DS_Store")
    put(nas, "Framefound/renders/earlier.mp4")
    for fine in (nas / "New", nas / "Looked at", nas / "Framefound" / "renders", data / "renders"):
        moves.validate_target("renders", fine, here)
    # Allowed, but worth an "are you sure?": a folder on the data folder's own
    # disk is what a share that is not mounted looks like.
    assert moves.on_data_disk(nas / "New", here)


def test_a_kind_is_not_moved_into_another_kinds_folder(tmp_path: Path) -> None:
    """Five kinds share one marker name, so "this is already a store for
    something else" could not tell the export store from the render store."""
    here, _data, nas = _install(tmp_path)
    root = nas / "Framefound"
    moves.adopt_bulk_root(root, settings=here)

    def refused(kind: str, target: Path) -> str:
        with pytest.raises(moves.MoveError) as refusal:
            moves.validate_target(kind, target, here)
        return str(refusal.value)

    assert "folder for listing exports" in refused("renders", root / "exports")
    assert "folder for models" in refused("backups", root / "models")
    assert "folder for proxies" in refused("renders", root / "proxies")
    # A kind's own folder somewhere else is that kind's as well…
    ssd = nas / "Fast SSD"
    moves.move("exports", ssd, settings=here, **QUICK)
    assert "folder for listing exports" in refused("renders", ssd)
    # …and so is the folder an unfinished move of another kind is copying to,
    # and one another kind has left its old copies in.
    stores.mark_store(nas / "half", INCOMING_MARKER, kind="basemaps")
    assert "unfinished move of basemaps" in refused("renders", nas / "half")
    stores.mark_store(nas / "left", BULK_STORE_MARKER, kind="basemaps")
    assert "basemaps are kept" in refused("renders", nas / "left")

    moves.validate_target("renders", root / "renders", here)  # its own place is its own
    moves.validate_target("basemaps", nas / "half", here)  # and a move may be carried on
    # The stores file is unchanged by any of the looks.
    assert stores.kind_root("renders", here) != root / "exports"


@pytest.fixture()
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'c.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def test_the_catalogue_says_which_folders_hold_footage(
    tmp_path: Path, db: AsyncSession
) -> None:
    """The bulk folder may sit beside footage on the Photos drive. It may
    not sit on top of it."""
    photos = tmp_path / "Photos"
    library = Library(name="Photos", root_path=str(photos))
    db.add(library)
    await db.flush()
    for relative, availability in (
        ("2026/Smith/a.jpg", "online"),
        ("2026/Smith/b.jpg", "online"),
        ("Frame_found/c.jpg", "online"),
        ("FrameXfound/d.jpg", "online"),
        ("Cleared out/e.jpg", "missing"),
    ):
        db.add(
            Asset(
                library_id=library.id,
                relative_path=relative,
                filename=relative.rsplit("/", 1)[-1],
                extension="jpg",
                media_type="image",
                size_bytes=1,
                mtime=datetime.now(UTC),
                availability=availability,
            )
        )
    await db.commit()
    here = at(tmp_path / "data")

    assert await moves.catalogued_under(db, photos / "2026", here) == 2
    assert await moves.catalogued_under(db, photos, here) == 4
    assert await moves.catalogued_under(db, tmp_path, here) == 4  # a folder above the library
    assert await moves.catalogued_under(db, photos / "Frame_found", here) == 1  # `_` is a letter
    assert await moves.catalogued_under(db, photos / "Framefound", here) == 0  # beside it: fine
    assert await moves.catalogued_under(db, photos / "Cleared out", here) == 0  # nothing left there
    assert await moves.catalogued_under(db, tmp_path / "Elsewhere", here) == 0


# --- afterwards: the old copies ----------------------------------------------------


def test_removing_old_copies_takes_only_that_kind_and_only_what_has_a_copy(
    tmp_path: Path,
) -> None:
    here, data, nas = _install(tmp_path)
    put(data, "derivatives/cd/cdef/proxy.mp4", b"second")
    with pytest.raises(moves.MoveError, match="no old copies"):
        moves.remove_old_copies("proxies", settings=here)  # nothing has been moved
    moves.move("proxies", nas / "proxies", settings=here, **QUICK)
    (nas / "proxies" / "derivatives/cd/cdef/proxy.mp4").unlink()  # lost since, somehow

    removed = moves.remove_old_copies("proxies", settings=here)

    assert (removed.files, removed.kept) == (1, 1)
    # The proxy with a copy in the new folder is gone from the old one…
    assert not (data / PROXY).exists() and (nas / "proxies" / PROXY).is_file()
    # …the one without is the only copy there is, and stays…
    assert (data / "derivatives/cd/cdef/proxy.mp4").read_bytes() == b"second"
    assert stores.read_state(data).previous == {"proxies": data}  # so it can be asked again
    # …and nothing that is not a proxy was touched: the thumbnail in the
    # same folder, the renders, the exports, the basemaps, the scratch.
    assert names(data) == sorted(
        [
            DATA_STORE_MARKER,
            STORES_FILE,
            THUMB,
            "derivatives/cd/cdef/proxy.mp4",
            RENDER,
            "renders/other.mp4",
            "renders/work/show/0000-body0.mp4",
            EXPORT,
            BASEMAP,
        ]
    )

    # Told the rest are not wanted either, it finishes and forgets the folder.
    assert moves.remove_old_copies("proxies", settings=here, everything=True).files == 1
    assert stores.read_state(data).previous == {}


def test_old_copies_are_never_removed_while_the_new_folder_is_away(tmp_path: Path) -> None:
    """Then they are the only copies anybody can reach."""
    here, data, nas = _install(tmp_path)
    moves.move("renders", nas / "renders", settings=here, **QUICK)
    (nas / "renders" / BULK_STORE_MARKER).unlink()  # the share drops

    with pytest.raises(moves.MoveError, match="Nothing was removed"):
        moves.remove_old_copies("renders", settings=here)
    with pytest.raises(moves.MoveError, match="live now"):
        moves.remove_old_copies("exports", settings=here, old_root=data / "exports")

    assert (data / RENDER).is_file() and (data / EXPORT).is_file()


# --- one folder for everything bulky -----------------------------------------------


def test_naming_the_bulk_folder_moves_nothing_and_loses_nothing(tmp_path: Path) -> None:
    here, data, nas = _install(tmp_path)
    (data / EXPORT).unlink()  # this install has never exported a listing
    root = nas / "Framefound"

    staying = moves.adopt_bulk_root(root, settings=here)

    # Kinds with files stay findable where they are, to be moved one by one…
    assert staying == ["basemaps", "proxies", "renders"]
    assert stores.path_for(RENDER, here) == data / RENDER
    assert stores.path_for(PROXY, here) == data / PROXY
    # …and a kind with nothing yet starts in the new folder straight away.
    assert stores.path_for(EXPORT, here) == root / "exports" / "listings" / "house.zip"
    assert stores.write_problem("exports", here) is None
    assert stores.kind_root("backups", here) == root / "backups"
    assert stores.bulk_root(here) == root
    assert names(root) == sorted(
        [BULK_STORE_MARKER] + [f"{kind}/{stores.marker_for(kind)}" for kind in stores.KINDS]
    )

    moves.move("renders", root / "renders", settings=here, **QUICK)
    assert stores.path_for(RENDER, here) == root / "renders" / "show.mp4"


def test_a_proxy_folder_made_before_the_bulk_folder_becomes_part_of_it(tmp_path: Path) -> None:
    """docs/local-models.md promises it: install with
    `--proxies <Photos>/Framefound/proxies` now, and nothing moves later."""
    data, root = tmp_path / "data", tmp_path / "Photos" / "Framefound"
    stores.mark_proxy_store(root / "proxies")
    put(root, f"proxies/{PROXY}")
    here = at(data, proxy_dir=root / "proxies")

    assert moves.adopt_bulk_root(root, settings=here) == []

    assert stores.path_for(PROXY, here) == root / "proxies" / PROXY
    assert stores.kind_problem("proxies", here) is None

    put(tmp_path, "Photos/Family/2019.jpg")
    with pytest.raises(moves.MoveError, match="did not put there"):
        moves.adopt_bulk_root(tmp_path / "Photos", settings=here)


def test_naming_another_bulk_folder_while_the_first_is_away_loses_nothing(tmp_path: Path) -> None:
    """Nobody can look inside a store that is not connected, and "it holds
    nothing" is the guess that makes its files unfindable when it is back."""
    data, first, second = (tmp_path / name for name in ("data", "old-nas", "new-nas"))
    first.mkdir()  # unplugged: the folder it mounts on, and nothing in it
    here = at(data, bulk_dir=first)

    staying = moves.adopt_bulk_root(second, settings=here)

    # The model library too: a share that is away may hold every model.
    assert staying == ["basemaps", "exports", "models", "proxies", "renders"]
    assert stores.path_for(RENDER, here) == first / "renders" / "show.mp4"
    assert "not connected" in (stores.kind_problem("renders", here) or "")
    assert stores.bulk_root(here) == second


def test_a_bulk_folder_that_lost_its_marker_is_not_marked_again_on_the_quiet(
    tmp_path: Path,
) -> None:
    """Running `install --store` again after a failure, with the share away
    and the folder it mounts on still there, used to mark that folder: every
    kind then read as connected, on this machine's own disk."""
    here, data, nas = _install(tmp_path)
    root = nas / "Framefound"
    moves.adopt_bulk_root(root, settings=here)
    assert moves.bulk_question(nas / "Elsewhere", here) is not None  # the same disk as the data
    shutil.rmtree(root)  # the share went, and the folder with it

    asked = moves.bulk_question(root, here)
    assert asked is not None and "marker file is missing" in asked.why
    with pytest.raises(moves.MoveError, match="not connected"):
        moves.adopt_bulk_root(root, settings=here)
    assert not root.exists()  # declined: nothing is made where the share mounts
    assert "not connected" in (stores.kind_problem("backups", here) or "")

    # The operator says the drive was replaced: started again, empty.
    moves.adopt_bulk_root(root, settings=here, sure=True)
    assert stores.kind_problem("backups", here) is None
    assert (root / BULK_STORE_MARKER).is_file()


# --- backups ---------------------------------------------------------------------


def test_backup_copies_go_to_the_bulk_folder_unless_told_otherwise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = Layout(home=tmp_path / "FrameFound", source=tmp_path / "src")
    nas, elsewhere = tmp_path / "nas", tmp_path / "elsewhere"
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    env = envfile.read(layout.env_file)
    monkeypatch.setattr(database, "dump", lambda _env, target: target.write_bytes(b"PGDMP"))
    monkeypatch.setattr(database, "schema_version", lambda _env: "0030")

    assert backup.copy_dir(env, layout) is None  # as before: no copy unless asked for
    bulk = {**env, "FRAMEFOUND_BULK_DIR": str(nas)}
    assert backup.copy_dir(bulk, layout) == nas / "backups"
    told = {**bulk, "FRAMEFOUND_BACKUP_COPY_DIR": str(elsewhere)}
    assert backup.copy_dir(told, layout) == elsewhere

    # The share is not mounted: the folder it mounts on must not collect the
    # "off-machine" copies on this Mac's own disk.
    (nas / "backups").mkdir(parents=True)
    archive = backup.run(layout, bulk)
    assert archive is not None and archive.is_file()
    assert "skipped" in capsys.readouterr().out
    assert list((nas / "backups").iterdir()) == []

    stores.mark_bulk_root(nas)
    archive.unlink()
    archive = backup.run(layout, bulk)
    assert archive is not None and (nas / "backups" / archive.name).is_file()


def test_backup_copies_in_a_plain_folder_can_be_moved_and_their_old_copies_removed(
    tmp_path: Path,
) -> None:
    """FRAMEFOUND_BACKUP_COPY_DIR is older than the markers and never had
    one. Its folder was moved from happily, then called "not connected" by
    every Remove old copies, for want of the marker nothing ever put there."""
    data, plain, nas = (tmp_path / name for name in ("data", "FrameFound Backups", "nas"))
    data.mkdir()
    nas.mkdir()
    put(plain, "framefound-20261101T000000Z.tar.gz", b"backup")
    put(plain, "holiday.jpg", b"not FrameFound's")
    here = at(data, backup_copy_dir=plain)

    moves.move("backups", nas / "backups", settings=here, **QUICK)

    assert stores.kind_root("backups", here) == nas / "backups"
    assert stores.read_state(data).previous == {"backups": plain}
    # Its drive unplugged, the old folder is still "not connected"…
    plain.rename(tmp_path / "unplugged")
    with pytest.raises(moves.MoveError, match="not connected"):
        moves.remove_old_copies("backups", settings=here)
    (tmp_path / "unplugged").rename(plain)

    removed = moves.remove_old_copies("backups", settings=here)

    # …and there, its backups go and nothing else in it does.
    assert (removed.files, removed.kept) == (1, 0)
    assert names(plain) == ["holiday.jpg"]
    assert names(nas / "backups") == [BULK_STORE_MARKER, "framefound-20261101T000000Z.tar.gz"]
    assert stores.read_state(data).previous == {}  # the offer is withdrawn


def test_backup_copies_can_be_given_a_folder_when_none_were_kept(tmp_path: Path) -> None:
    data, nas = tmp_path / "data", tmp_path / "nas"
    data.mkdir()
    nas.mkdir()
    here = at(data)
    assert stores.kind_root("backups", here) is None

    result = moves.move("backups", nas / "backups", settings=here, **QUICK)

    assert (result.source, result.files, result.copied) == (None, 0, 0)
    assert stores.kind_root("backups", here) == nas / "backups"
    assert stores.kind_problem("backups", here) is None
    with pytest.raises(moves.MoveError, match="no old copies"):
        moves.remove_old_copies("backups", settings=here)


# --- a worker on another machine -----------------------------------------------------


def test_a_remote_worker_never_guesses_where_the_server_keeps_proxies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The record names folders as the server sees them. A worker that
    followed it would write to a path that is somebody else's on its own
    machine; one that ignored it would write where the server no longer looks."""
    data, nas = tmp_path / "shared-data", tmp_path / "nas"
    stores.set_override(data, "proxies", nas / "proxies")
    worker = at(data, root_map="/media/intel=/Volumes/intel")

    assert stores.path_for(PROXY, worker) == data / PROXY  # never the server's path
    assert "no setting" in (stores.not_here("proxies", worker) or "")
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(data))
    monkeypatch.setenv("FRAMEFOUND_ROOT_MAP", "/media/intel=/Volumes/intel")
    get_settings.cache_clear()
    with pytest.raises(NotHere):  # handed back to the server, not failed
        deriv.ensure_store("proxies")

    # With its own setting for the same store, it carries on…
    mounted = tmp_path / "Volumes" / "nas" / "proxies"
    assert stores.not_here("proxies", at(data, root_map="/a=/b", proxy_dir=mounted)) is None
    # …as it does when the server keeps proxies in the data store they share.
    stores.set_override(data, "proxies", data)
    assert stores.not_here("proxies", worker) is None
    # The server itself is never "not here".
    assert stores.not_here("proxies", at(data)) is None


# --- the tasks, all the way to a store that is there --------------------------------


async def _photograph(site: dict[str, Any], name: str = "front.jpg") -> Asset:
    """A real JPEG in a library, with the preview a slideshow is made from."""
    from PIL import Image

    root = site["data"].parent / "media"
    root.mkdir(exist_ok=True)
    Image.new("RGB", (120, 90), (200, 60, 40)).save(root / name, "JPEG")
    async with site["factory"]() as db:
        library = Library(name=f"L-{name}", root_path=str(root))
        db.add(library)
        await db.flush()
        asset = Asset(
            library_id=library.id,
            relative_path=name,
            filename=name,
            extension="jpg",
            media_type="image",
            size_bytes=1000,
            mtime=datetime.now(UTC),
        )
        db.add(asset)
        await db.flush()
        preview = deriv.derivative_relpath(asset.id, "preview", "webp")
        db.add(
            Derivative(
                asset_id=asset.id,
                kind="preview",
                relative_path=preview,
                media_format="webp",
                status="ready",
            )
        )
        await db.commit()
    put(site["data"], preview, b"preview")
    return asset


async def test_an_export_is_built_on_this_machine_and_lands_in_its_store(
    site: dict[str, Any],
) -> None:
    """The task itself, not its last two lines: a later edit that wrote the
    zip under the data folder again would record it ready where the download
    never looks."""
    from framefound.processing.tasks import export_listing_zip

    data, nas = site["data"], site["nas"]
    stores.mark_bulk_root(nas)
    photo = await _photograph(site)
    async with site["factory"]() as db:
        listing = Listing(name="12 Maple St", export_status="queued")
        db.add(listing)
        await db.flush()
        db.add(ListingItem(listing_id=listing.id, asset_id=photo.id, position=0))
        await db.commit()

    await asyncio.to_thread(export_listing_zip, str(listing.id))

    async with site["factory"]() as db:
        after = await db.get(Listing, listing.id)
        assert after.export_status == "ready", after.export_error
        assert after.export_relpath == f"exports/listings/{listing.id}.zip"
    placed = nas / "exports" / "listings" / f"{listing.id}.zip"
    assert placed.is_file() and placed.read_bytes()[:2] == b"PK"
    assert not (data / "exports").exists()  # not where it was before there was a store
    assert names(data / "tmp") == []  # and nothing left in scratch
    served = await site["client"].get(f"/api/v1/listings/{listing.id}/export/download")
    assert served.status_code == 200 and served.content == placed.read_bytes()


@pytest.mark.parametrize("with_a_store", [True, False])
async def test_a_render_is_assembled_on_this_machine_and_lands_where_renders_live(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch, with_a_store: bool
) -> None:
    from framefound.media import pipeline
    from framefound.processing.tasks import render_slideshow

    data, nas = site["data"], site["nas"]
    if with_a_store:
        stores.mark_bulk_root(nas)
    else:
        use(monkeypatch, data)  # nothing configured: renders are where they always were
    photo = await _photograph(site)
    async with site["factory"]() as db:
        show = Slideshow(title="Open house", status="pending", asset_ids=[str(photo.id)])
        db.add(show)
        await db.commit()
    made_in: list[Path] = []

    def encode(piece: Any) -> None:
        piece.path.write_bytes(b"piece")

    def join(_spec: Any, _pieces: Any, workdir: Path) -> Any:
        made_in.append(workdir)
        (workdir / "final.mp4").write_bytes(b"the video")
        return pipeline.RenderResult(path=workdir / "final.mp4", seconds=4.0, size_bytes=9)

    monkeypatch.setattr(pipeline, "choose_encoder", lambda _spec: None)  # no ffmpeg in a test
    monkeypatch.setattr(pipeline, "run_piece", encode)
    monkeypatch.setattr(pipeline, "assemble", join)

    await asyncio.to_thread(render_slideshow, str(show.id))

    async with site["factory"]() as db:
        after = await db.get(Slideshow, show.id)
        assert (after.status, after.error) == ("ready", None)
        assert after.relative_path == f"renders/{show.id}.mp4" and after.size_bytes == 9
    # The pieces and the join are this machine's own disk's work…
    assert made_in == [data / "renders" / "work" / str(show.id)]
    assert not made_in[0].exists()  # …cleared away once the video is in place
    placed = (nas if with_a_store else data) / "renders" / f"{show.id}.mp4"
    assert placed.read_bytes() == b"the video"
    if with_a_store:
        assert names(data / "renders") == []
    served = await site["client"].get(f"/api/v1/slideshows/{show.id}/video")
    assert served.status_code == 200 and served.content == b"the video"


async def test_a_proxy_skipped_while_its_store_is_away_is_found_by_the_rebuild(
    site: dict[str, Any],
) -> None:
    """The store is away: the clip's proxy job ends quietly, with nothing
    written and no record made. Nothing queues it a second time, so
    `framefound proxies rebuild` has to be able to see that it was skipped."""
    from framefound.ops import proxy_store
    from framefound.processing.tasks import generate_proxy

    data, nas = site["data"], site["nas"]
    root = data.parent / "media"
    put(root, "clip.mp4", b"not really a video")
    async with site["factory"]() as db:
        library = Library(name="Footage", root_path=str(root))
        db.add(library)
        await db.flush()
        clip = Asset(
            library_id=library.id,
            relative_path="clip.mp4",
            filename="clip.mp4",
            extension="mp4",
            media_type="video",
            size_bytes=18,
            mtime=datetime.now(UTC),
            processing_status="ready",
        )
        db.add(clip)
        await db.commit()

    await asyncio.to_thread(generate_proxy, str(clip.id))  # does not raise, does not retry

    assert list(nas.iterdir()) == [] and not (data / "derivatives").exists()
    async with site["factory"]() as db:
        assert (await db.execute(Derivative.__table__.select())).all() == []
        job = (await db.execute(Job.__table__.select())).one()
        assert job.task_name == "generate_proxy" and "not connected" in job.error
        # Back again, the clip is one the rebuild queues.
        stores.mark_bulk_root(nas)
        assert await proxy_store.never_made(db) == [clip.id]
        assert await proxy_store.missing(db, data) == (0, [])
