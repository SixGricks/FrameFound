"""The Storage page's side of the bulk store (api/v1/storage.py,
storage/move_job.py): where each kind lives, naming the bulk folder, and a
move that is started from a page, runs in a worker and can be left and come
back to.

The copy itself is storage/moves.py and has its own tests
(test_bulk_store.py). Here the queue is a list: a test hands the job its
turn when it wants the worker to have got there."""

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.api.v1 import storage as storage_api
from framefound.api.v1 import system as system_api
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    Asset,
    AuditLog,
    Derivative,
    Library,
    LocalAiRole,
    LocalModel,
    Slideshow,
)
from framefound.local_models.files import Manifest, folder_problem, write_manifest
from framefound.local_models.hub import RepoFile
from framefound.storage import move_job, moves, stores
from framefound.storage.stores import BULK_STORE_MARKER

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
RENDER = "renders/show.mp4"
EXPORT = "exports/listings/house.zip"
# Every folder in a test is on one disk, which the API asks about before it
# will use it for a store.
SURE = {"confirm_same_disk": True}


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    def clear() -> None:
        get_settings.cache_clear()
        stores._CACHE.clear()
        storage_api._KINDS_CACHE.clear()
        storage_api._TREE_CACHE.clear()
        storage_api._ASKING.clear()
        system_api._VOLUMES_CACHE.clear()

    clear()
    yield
    clear()


def put(root: Path, relative: str, content: bytes = b"content") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture()
async def site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """An install with everything on its own disk, a drive to move things to
    (`nas`), and a queue that only records what was asked of it."""
    from framefound.processing.tasks import move_storage_kind

    url = f"sqlite+aiosqlite:///{(tmp_path / 'catalogue.db').as_posix()}"
    data, nas = tmp_path / "data", tmp_path / "nas"
    data.mkdir()
    nas.mkdir()
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "storage-page-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(data))
    monkeypatch.setenv("FRAMEFOUND_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("FRAMEFOUND_BULK_DIR", "")
    get_settings.cache_clear()
    # Each request measures afresh; one test puts the memory back.
    monkeypatch.setattr(storage_api, "KINDS_CACHE_S", 0.0)
    queued: list[str] = []
    monkeypatch.setattr(move_storage_kind, "delay", queued.append)

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
        yield {
            "client": client,
            "app": app,
            "factory": factory,
            "data": data,
            "nas": nas,
            "queued": queued,
        }
    await engine.dispose()


async def kinds(site: dict[str, Any]) -> dict[str, dict[str, Any]]:
    response = await site["client"].get("/api/v1/storage/kinds")
    assert response.status_code == 200, response.text
    return {row["kind"]: row for row in response.json()["kinds"]}


async def worker_runs(site: dict[str, Any], move_id: str, **more: Any) -> None:
    """The worker reaches the job. A real move waits ten seconds for the old
    folder to go quiet; not in a test."""
    options: dict[str, Any] = {"tick_s": 0.01, "settle_s": 0.0, "sleep": lambda _seconds: None}
    await move_job.run(site["factory"], move_id, **{**options, **more})


async def audited(site: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    async with site["factory"]() as db:
        rows = (await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        return [(row.event, row.detail) for row in rows if row.event.startswith("storage.")]


def message(response: Any) -> str:
    return str(response.json()["error"]["message"])


# --- where things live -------------------------------------------------------------


async def test_each_kind_is_listed_with_where_it_lives_and_how_much_there_is(
    site: dict[str, Any], tmp_path: Path
) -> None:
    data = site["data"]
    put(data, EXPORT, b"zip!")
    put(data, "basemaps/pennsylvania.pmtiles", b"x" * 10)
    put(data, "grabs/abcd/1000.tif", b"t" * 5)
    async with site["factory"]() as db:
        library = Library(name="L", root_path=str(tmp_path / "footage"))
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
        await db.flush()
        for kind, size in (("proxy", 700), ("thumbnail", 9)):
            db.add(
                Derivative(
                    asset_id=asset.id,
                    kind=kind,
                    relative_path=f"derivatives/ab/abcd/{kind}.x",
                    media_format="x",
                    status="ready",
                    size_bytes=size,
                )
            )
        db.add(Slideshow(title="A", asset_ids=[], relative_path="renders/a.mp4", size_bytes=40))
        db.add(Slideshow(title="Not rendered yet", asset_ids=[]))
        await db.commit()

    response = await site["client"].get("/api/v1/storage/kinds")
    body = response.json()
    rows = {row["kind"]: row for row in body["kinds"]}

    assert list(rows) == [*stores.KINDS, "grabs"]
    assert body["bulk_root"] is None and body["move"] is None and body["can_move"] is True
    assert body["problem"] is None
    # No move has ever been asked for: the page is told "nothing", not an error.
    never = await site["client"].get("/api/v1/storage/move")
    assert never.status_code == 200 and never.json() is None
    # With nothing configured everything is where it always was, and "there".
    for kind in ("proxies", "renders", "exports", "basemaps", "models", "grabs"):
        assert (rows[kind]["where"], rows[kind]["connected"]) == ("local", True), kind
        assert rows[kind]["free_gb"] is not None and rows[kind]["problem"] is None
    assert rows["proxies"]["path"] == str(data)
    assert rows["renders"]["path"] == rows["renders"]["default_path"] == str(data / "renders")
    # The catalogue knows these two sizes; the thumbnail is not a proxy.
    assert (rows["proxies"]["files"], rows["proxies"]["size_bytes"]) == (1, 700)
    assert (rows["renders"]["files"], rows["renders"]["size_bytes"]) == (1, 40)
    # The others are a handful of big files, counted where they lie.
    assert (rows["exports"]["files"], rows["exports"]["size_bytes"]) == (1, 4)
    assert (rows["basemaps"]["files"], rows["basemaps"]["size_bytes"]) == (1, 10)
    assert (rows["grabs"]["files"], rows["grabs"]["size_bytes"]) == (1, 5)
    # No second copy of the backups is kept until a folder is given for one.
    assert (rows["backups"]["path"], rows["backups"]["where"]) == (None, "none")
    assert rows["backups"]["connected"] is False and rows["backups"]["movable"] is True
    # Frame grabs are shown for their size and stay on this machine.
    assert [kind for kind, row in rows.items() if not row["movable"]] == ["grabs"]
    assert all(row["label"] and row["description"] for row in rows.values())


async def test_a_store_that_is_away_says_so_and_nothing_is_made_in_its_place(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    nas = site["nas"]
    monkeypatch.setenv("FRAMEFOUND_BULK_DIR", str(nas))  # the share is not mounted
    get_settings.cache_clear()
    async with site["factory"]() as db:
        db.add(Slideshow(title="A", asset_ids=[], relative_path="renders/a.mp4", size_bytes=40))
        await db.commit()

    rows = await kinds(site)

    for kind in ("proxies", "renders", "exports", "basemaps", "backups"):
        assert (rows[kind]["where"], rows[kind]["connected"]) == ("bulk", False), kind
        assert "not connected" in rows[kind]["problem"]
        assert rows[kind]["path"] == str(nas / kind) and rows[kind]["bulk_path"] is None
    # What the catalogue knows is still shown; what would need the share is not guessed.
    assert rows["renders"]["size_bytes"] == 40 and rows["exports"]["size_bytes"] is None
    assert rows["grabs"]["connected"] is True  # this machine's own disk is unaffected
    assert list(nas.iterdir()) == []

    stores.mark_bulk_root(nas)
    rows = await kinds(site)
    assert rows["exports"]["connected"] is True and rows["exports"]["files"] == 0
    assert rows["exports"]["total_gb"] is not None


async def test_a_record_that_cannot_be_read_is_said_and_nothing_is_moved_on_a_guess(
    site: dict[str, Any],
) -> None:
    client, data, nas = site["client"], site["data"], site["nas"]
    (data / stores.STORES_FILE).write_text("{ not json", encoding="utf-8")

    body = (await client.get("/api/v1/storage/kinds")).json()

    assert "cannot be read" in body["problem"] and body["can_move"] is False
    rows = {row["kind"]: row for row in body["kinds"]}
    assert rows["renders"]["connected"] is False and "cannot be read" in rows["renders"]["problem"]
    assert rows["grabs"]["connected"] is True
    refused = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(nas / "renders"), **SURE}
    )
    assert refused.status_code == 400 and "cannot be read" in message(refused)
    assert site["queued"] == []


async def test_a_share_that_does_not_answer_is_reported_not_waited_for(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page asks every few seconds. A hung share must cost one thread,
    not one per question."""
    asked: list[str] = []
    release = threading.Event()
    real = storage_api._measure

    def hung(kind: str, root: Path, settings: Any) -> Any:
        asked.append(kind)
        if kind == "basemaps":
            release.wait(5)
        return real(kind, root, settings)

    monkeypatch.setattr(storage_api, "_measure", hung)
    # Long enough for the folders that are there to answer on a busy machine.
    monkeypatch.setattr(storage_api, "PROBE_TIMEOUT_S", 0.75)
    try:
        started = time.monotonic()
        rows = await kinds(site)
        assert time.monotonic() - started < 4, "the page did not wait for the share"
        assert rows["basemaps"]["connected"] is False
        assert "did not answer" in rows["basemaps"]["problem"]
        assert rows["exports"]["connected"] is True  # the others answered

        await kinds(site)
        assert asked.count("basemaps") == 1, "the unanswered question was not asked again"
        assert asked.count("exports") == 2
    finally:
        release.set()


async def test_inside_the_cache_window_the_folders_are_not_asked_again(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    real = storage_api._measure

    def counting(kind: str, root: Path, settings: Any) -> Any:
        asked.append(kind)
        return real(kind, root, settings)

    monkeypatch.setattr(storage_api, "_measure", counting)
    monkeypatch.setattr(storage_api, "KINDS_CACHE_S", 30.0)

    await kinds(site)
    first = len(asked)
    await kinds(site)
    assert len(asked) == first
    # A kind that has moved is somewhere new, and that is asked about at once.
    stores.mark_store(site["nas"] / "renders", BULK_STORE_MARKER)
    stores.set_override(site["data"], "renders", site["nas"] / "renders")
    assert (await kinds(site))["renders"]["where"] == "custom"
    assert len(asked) > first


# --- the bulk folder ---------------------------------------------------------------


async def test_naming_the_bulk_folder_marks_it_and_moves_nothing(site: dict[str, Any]) -> None:
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")
    root = nas / "Framefound"

    # A folder on the disk FrameFound's own data is on is what a share that
    # is not mounted looks like: asked about first, never silently used.
    asks = await client.post("/api/v1/storage/bulk", json={"path": str(root)})
    assert asks.status_code == 428 and "same disk" in message(asks)
    assert asks.json()["error"]["code"] == "confirmation_required"
    assert stores.bulk_root() is None

    done = await client.post("/api/v1/storage/bulk", json={"path": str(root), **SURE})

    assert done.status_code == 200, done.text
    assert done.json() == {"bulk_root": str(root), "staying": ["renders"]}
    assert (root / BULK_STORE_MARKER).is_file() and (root / "exports" / BULK_STORE_MARKER).is_file()
    assert (data / RENDER).read_bytes() == b"video"  # nothing was moved
    rows = await kinds(site)
    # The kind with files stays findable where it is, with its place in the
    # bulk folder offered as where to move it…
    assert rows["renders"]["where"] == "local"
    assert rows["renders"]["bulk_path"] == str(root / "renders")
    # …and a kind with nothing yet starts in the bulk folder straight away.
    assert (rows["exports"]["where"], rows["exports"]["connected"]) == ("bulk", True)
    assert rows["backups"]["path"] == str(root / "backups")
    assert await audited(site) == [
        ("storage.bulk_folder_set", {"folder": str(root), "staying": ["renders"]})
    ]


async def test_a_folder_that_must_not_be_the_bulk_folder_is_refused_with_the_reason(
    site: dict[str, Any],
) -> None:
    client, nas = site["client"], site["nas"]
    put(nas, "Family/2019.jpg")

    theirs = await client.post("/api/v1/storage/bulk", json={"path": str(nas), **SURE})
    assert theirs.status_code == 400 and "did not put there" in message(theirs)
    relative = await client.post("/api/v1/storage/bulk", json={"path": "Framefound", **SURE})
    assert relative.status_code == 400 and "full path" in message(relative)
    unplugged = await client.post(
        "/api/v1/storage/bulk", json={"path": str(nas / "Photos" / "Framefound"), **SURE}
    )
    assert unplugged.status_code == 400 and "Connect the drive" in message(unplugged)

    assert stores.bulk_root() is None and not (nas / BULK_STORE_MARKER).exists()
    assert await audited(site) == []


# --- a move ------------------------------------------------------------------------


async def test_a_move_is_queued_shows_its_progress_and_switches_with_nothing_deleted(
    site: dict[str, Any],
) -> None:
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")
    put(data, EXPORT, b"zip")
    target = nas / "renders"

    asks = await client.post("/api/v1/storage/kinds/renders/move", json={"target": str(target)})
    assert asks.status_code == 428 and site["queued"] == []

    started = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(target), **SURE}
    )

    assert started.status_code == 202, started.text
    move = started.json()
    assert (move["kind"], move["phase"], move["active"]) == ("renders", "queued", True)
    assert (move["source"], move["target"]) == (str(data / "renders"), str(target))
    assert move["requested_by"] == ADMIN["email"] and move["label"] == "Slideshow videos"
    assert site["queued"] == [move["id"]]
    # Nothing has moved yet, and the page can say what it is waiting for.
    assert stores.path_for(RENDER) == data / RENDER
    listing = (await client.get("/api/v1/storage/kinds")).json()
    assert listing["move"]["id"] == move["id"] and listing["can_move"] is False

    # One move at a time, whatever the kind.
    second = await client.post(
        "/api/v1/storage/kinds/exports/move", json={"target": str(nas / "exports"), **SURE}
    )
    assert second.status_code == 409 and "Slideshow videos are being moved" in message(second)
    bulk = await client.post("/api/v1/storage/bulk", json={"path": str(nas / "F"), **SURE})
    assert bulk.status_code == 409
    assert site["queued"] == [move["id"]]

    await worker_runs(site, move["id"])

    after = (await client.get("/api/v1/storage/move")).json()
    assert (after["phase"], after["active"], after["error"]) == ("done", False, None)
    assert (after["files_done"], after["files_total"], after["copied"]) == (1, 1, 1)
    assert after["bytes_done"] == after["bytes_total"] == 5
    assert after["started_at"] and after["finished_at"]
    assert (target / "show.mp4").read_bytes() == b"video"
    assert stores.path_for(RENDER) == target / "show.mp4"
    rows = await kinds(site)
    assert (rows["renders"]["where"], rows["renders"]["connected"]) == ("custom", True)
    # The old copy is still there, and the page is told where.
    assert (data / RENDER).is_file() and rows["renders"]["old_path"] == str(data / "renders")
    # Another move may start now.
    assert (await client.get("/api/v1/storage/kinds")).json()["can_move"] is True
    assert await audited(site) == [
        (
            "storage.move_started",
            {"kind": "renders", "from": str(data / "renders"), "to": str(target), "mode": "copy"},
        )
    ]


async def test_the_old_copies_are_removed_only_when_asked(site: dict[str, Any]) -> None:
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")
    put(data, "renders/lost.mp4", b"another")
    put(data, "renders/work/show/piece.mp4", b"scratch")
    url = "/api/v1/storage/kinds/renders/cleanup"

    nothing = await client.post(url)
    assert nothing.status_code == 400 and "no old copies" in message(nothing)

    started = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(nas / "renders"), **SURE}
    )
    busy = await client.post(url)
    assert busy.status_code == 409 and "Nothing was removed" in message(busy)
    await worker_runs(site, started.json()["id"])
    (nas / "renders" / "lost.mp4").unlink()  # gone from the new folder since, somehow

    removed = await client.post(url)

    assert removed.status_code == 200, removed.text
    assert removed.json() == {
        "kind": "renders",
        "folder": str(data / "renders"),
        "files": 1,
        "bytes": 5,
        "kept": 1,  # the only copy there is of that one: left alone, and said
    }
    assert not (data / RENDER).exists() and (data / "renders" / "lost.mp4").is_file()
    assert (data / "renders/work/show/piece.mp4").is_file()  # not a finished video: not touched
    assert (await kinds(site))["renders"]["old_path"] == str(data / "renders")

    # Told the rest is not wanted either, it finishes and forgets the folder.
    rest = await client.post(url, json={"everything": True})
    assert (rest.json()["files"], rest.json()["kept"]) == (1, 0)
    assert (await kinds(site))["renders"]["old_path"] is None
    events = [event for event, _detail in await audited(site)]
    assert events == [
        "storage.move_started",
        "storage.old_copies_removed",
        "storage.old_copies_removed",
    ]


async def test_a_move_that_cannot_work_is_refused_at_the_click(site: dict[str, Any]) -> None:
    client, data, nas = site["client"], site["data"], site["nas"]
    put(nas, "Family/2019.jpg")

    async def refused(kind: str, target: Path, **more: Any) -> tuple[int, str]:
        response = await client.post(
            f"/api/v1/storage/kinds/{kind}/move", json={"target": str(target), **SURE, **more}
        )
        return response.status_code, message(response)

    assert (await refused("renders", nas))[0] == 400  # somebody's photographs
    assert "did not put there" in (await refused("renders", nas))[1]
    assert "full path" in (await refused("renders", Path("renders")))[1]
    assert "Connect the drive" in (await refused("renders", nas / "Photos" / "renders"))[1]
    assert "already live in" in (await refused("renders", data / "renders"))[1]
    # Spelt the long way round, the data folder is still the data folder.
    roundabout = nas / ".." / "data" / "exports-on-nas"
    assert "inside FrameFound's own data folder" in (await refused("exports", roundabout))[1]
    assert await refused("grabs", nas / "grabs") == (400, "Frame grabs cannot be moved.")
    assert (await refused("thumbnails", nas / "t"))[0] == 404

    # A store that is away cannot be copied from — said now, not in a
    # progress row a minute later — but it can be replaced.
    stores.set_override(data, "exports", nas / "unplugged")
    status, why = await refused("exports", nas / "exports")
    assert status == 400 and "cannot be copied from" in why and "start the new folder empty" in why
    assert site["queued"] == [] and await audited(site) == []

    empty = await client.post(
        "/api/v1/storage/kinds/exports/move",
        json={"target": str(nas / "exports"), "mode": "empty", **SURE},
    )
    assert empty.status_code == 202 and empty.json()["mode"] == "empty"
    await worker_runs(site, empty.json()["id"])
    assert (await client.get("/api/v1/storage/move")).json()["phase"] == "done"
    assert stores.kind_root("exports") == nas / "exports"
    assert stores.kind_problem("exports") is None


async def test_a_move_that_fails_in_the_worker_says_why_and_changes_nothing(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")
    started = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(nas / "renders"), **SURE}
    )
    monkeypatch.setattr(moves, "_copy_file", lambda *_args: None)  # a copy that copied nothing

    await worker_runs(site, started.json()["id"])

    after = (await client.get("/api/v1/storage/move")).json()
    assert (after["phase"], after["active"]) == ("failed", False)
    assert "0 of 1" in after["error"] and "Nothing was changed" in after["error"]
    assert stores.path_for(RENDER) == data / RENDER
    assert stores.write_problem("renders") is None  # renders are not left paused
    # …and it does not stand in the way of trying again.
    again = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(nas / "renders"), **SURE}
    )
    assert again.status_code == 202


async def test_a_move_can_be_cancelled_before_it_starts_and_while_it_copies(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")
    body = {"target": str(nas / "renders"), **SURE}

    # Still waiting for a worker: cancelled at once, and its turn does nothing.
    waiting = (await client.post("/api/v1/storage/kinds/renders/move", json=body)).json()
    cancelled = await client.post("/api/v1/storage/move/cancel")
    assert (cancelled.json()["phase"], cancelled.json()["active"]) == ("cancelled", False)
    await worker_runs(site, waiting["id"])
    assert (await client.get("/api/v1/storage/move")).json()["phase"] == "cancelled"
    # (The folder was made when it was checked, to prove it can be written.)
    assert stores.path_for(RENDER) == data / RENDER and list((nas / "renders").iterdir()) == []

    # Copying: it stops at its next look, with nothing switched.
    running = (await client.post("/api/v1/storage/kinds/renders/move", json=body)).json()
    reached, seen = threading.Event(), threading.Event()
    real_tick = move_job._tick

    async def tick(factory: Any, move_id: str, latest: Any, stop: threading.Event, last: float):  # type: ignore[no-untyped-def]
        wrote = await real_tick(factory, move_id, latest, stop, last)
        if stop.is_set():
            seen.set()  # the job has read the request to stop
        return wrote

    def the_copy_is_under_way(_seconds: float) -> None:
        reached.set()
        seen.wait(5)

    monkeypatch.setattr(move_job, "_tick", tick)
    job = asyncio.ensure_future(worker_runs(site, running["id"], sleep=the_copy_is_under_way))
    await asyncio.to_thread(reached.wait, 5)
    stopping = (await client.post("/api/v1/storage/move/cancel")).json()
    assert (stopping["active"], stopping["cancelling"]) == (True, True)
    await job

    after = (await client.get("/api/v1/storage/move")).json()
    assert (after["phase"], after["active"], after["cancelling"]) == ("cancelled", False, False)
    assert "Nothing was changed" in after["error"]
    assert stores.path_for(RENDER) == data / RENDER  # still where it was
    assert stores.write_problem("renders") is None  # and not left paused
    assert (nas / "renders" / "show.mp4").is_file()  # what was copied is kept for next time
    # A cancel with nothing to cancel is not an error.
    assert (await client.post("/api/v1/storage/move/cancel")).json()["phase"] == "cancelled"
    events = [event for event, _detail in await audited(site)]
    assert events.count("storage.move_cancelled") == 2


async def test_a_cancel_survives_the_job_writing_its_progress_at_the_same_moment(
    site: dict[str, Any],
) -> None:
    """The job rewrites the row every two seconds from what it read a moment
    before. Kept in that row, a Cancel that landed between the read and the
    write would simply vanish."""
    running = move_job.new("renders", site["nas"] / "r", source=None, mode="copy", requested_by="x")
    running.phase = "copying"
    async with site["factory"]() as db:
        await move_job.save(db, running)

    async with site["factory"]() as job, site["factory"]() as page:
        read_before_the_click = await move_job.load(job)
        clicked = await move_job.load(page)
        assert read_before_the_click is not None and clicked is not None
        await move_job.ask_to_stop(page, clicked)
        read_before_the_click.files_done = 5
        await move_job.save(job, read_before_the_click)  # lands second

    async with site["factory"]() as db:
        after = await move_job.load(db)
        assert after is not None and after.cancel is True and after.files_done == 5
    # A request to stop one move says nothing about the next.
    following = move_job.new(
        "exports", site["nas"] / "e", source=None, mode="copy", requested_by="x"
    )
    async with site["factory"]() as db:
        await move_job.save(db, following)
        loaded = await move_job.load(db)
        assert loaded is not None and loaded.cancel is False

    # And a move cancelled in the instant its job took its turn stays
    # cancelled: the job stops instead of writing "copying" over it.
    following.phase = "cancelled"
    async with site["factory"]() as db:
        await move_job.save(db, following)
    latest, stop = move_job._Latest(), threading.Event()
    latest.progress = ("copying", 9, 1, 900, 100)
    await move_job._tick(site["factory"], following.id, latest, stop, time.monotonic())
    assert stop.is_set()
    async with site["factory"]() as db:
        kept = await move_job.load(db)
        assert kept is not None and kept.phase == "cancelled"


async def test_a_move_whose_worker_died_does_not_block_the_next_one(
    site: dict[str, Any],
) -> None:
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")
    dead = move_job.new(
        "exports", nas / "exports", source=data / "exports", mode="copy", requested_by="x"
    )
    dead.phase = "copying"
    dead.beat_at = time.time() - move_job.STALE_AFTER_S - 1
    async with site["factory"]() as db:
        await move_job.save(db, dead)

    shown = (await client.get("/api/v1/storage/move")).json()
    assert (shown["phase"], shown["active"]) == ("failed", False)
    assert "restarted or lost" in shown["error"]

    replaced = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(nas / "renders"), **SURE}
    )
    assert replaced.status_code == 202
    # The dead move's message, should the queue ever deliver it, is not this
    # move's turn; nor is a second delivery of a move that has already run.
    await worker_runs(site, dead.id)
    assert (await client.get("/api/v1/storage/move")).json()["phase"] == "queued"
    await worker_runs(site, replaced.json()["id"])
    (nas / "renders" / "show.mp4").unlink()
    await worker_runs(site, replaced.json()["id"])
    assert not (nas / "renders" / "show.mp4").exists(), "a second delivery copied again"
    assert (await client.get("/api/v1/storage/move")).json()["phase"] == "done"


async def test_a_move_that_is_copying_keeps_saying_it_is_alive(site: dict[str, Any]) -> None:
    """One huge file is minutes without a change in the numbers. The row is
    still touched, or the page would call the move dead."""
    state = move_job.new("renders", site["nas"] / "r", source=None, mode="copy", requested_by="x")
    state.phase = "copying"
    state.beat_at = time.time() - 60
    async with site["factory"]() as db:
        await move_job.save(db, state)
    latest, stop = move_job._Latest(), threading.Event()

    quiet = time.monotonic() - move_job.HEARTBEAT_S - 1
    wrote = await move_job._tick(site["factory"], state.id, latest, stop, quiet)

    assert wrote > quiet and not stop.is_set()
    async with site["factory"]() as db:
        after = await move_job.load(db)
        assert after is not None and time.time() - after.beat_at < 30
    # Nothing new and a recent heartbeat: no write.
    assert await move_job._tick(site["factory"], state.id, latest, stop, wrote) == wrote


async def test_a_queue_that_is_down_is_said_and_leaves_nothing_waiting(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.processing.tasks import move_storage_kind

    def down(_move_id: str) -> None:
        raise ConnectionError("no broker")

    monkeypatch.setattr(move_storage_kind, "delay", down)
    put(site["data"], RENDER, b"video")

    refused = await site["client"].post(
        "/api/v1/storage/kinds/renders/move",
        json={"target": str(site["nas"] / "renders"), **SURE},
    )

    assert refused.status_code == 503 and "queue" in message(refused)
    shown = (await site["client"].get("/api/v1/storage/move")).json()
    assert (shown["phase"], shown["active"]) == ("failed", False)
    # Nothing started, so the audit log does not say that something did.
    assert await audited(site) == []


def test_the_move_runs_with_the_exports_and_is_never_retried() -> None:
    """`exports` is consumed by the server's own workers in both installs
    (pinned in test_native_install.py), and a move that fails waits for the
    operator rather than starting again by itself."""
    from framefound.processing.tasks import move_storage_kind

    assert move_storage_kind.name == "framefound.move_storage_kind"
    assert move_storage_kind.queue == "exports"
    assert move_storage_kind.max_retries == 0
    assert not getattr(move_storage_kind, "autoretry_for", ())


def test_a_move_from_the_page_has_no_way_to_stop_a_service() -> None:
    """It runs inside the worker the command-line move used to stop first.
    Stopping that would be the job killing itself, and the queue handing it
    straight back; so the code it is made of cannot reach launchd at all."""
    import inspect

    for module in (move_job, moves):
        source = inspect.getsource(module)
        assert "framefound.native" not in source and "import subprocess" not in source


# --- what the click is told ------------------------------------------------------------


async def test_a_move_started_from_a_terminal_is_seen_and_waited_for(
    site: dict[str, Any],
) -> None:
    """`framefound storage move` runs in its own process and was in no row
    the page reads. A second move started here ran beside it, and each
    lifted the pause the other needed for its last pass."""
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")
    put(data, EXPORT, b"zip")

    with moves._turn("renders", nas / "renders", get_settings()):  # the terminal's move is running
        listing = (await client.get("/api/v1/storage/kinds")).json()
        assert listing["can_move"] is False and listing["move"] is None
        assert "Slideshow videos are being moved" in listing["busy"]
        refused = await client.post(
            "/api/v1/storage/kinds/exports/move", json={"target": str(nas / "exports"), **SURE}
        )
        assert refused.status_code == 409 and "Slideshow videos are being moved" in message(refused)
        bulk = await client.post("/api/v1/storage/bulk", json={"path": str(nas / "F"), **SURE})
        assert bulk.status_code == 409
        assert site["queued"] == [] and list(nas.iterdir()) == []

    listing = (await client.get("/api/v1/storage/kinds")).json()
    assert listing["can_move"] is True and listing["busy"] is None
    started = await client.post(
        "/api/v1/storage/kinds/exports/move", json={"target": str(nas / "exports"), **SURE}
    )
    assert started.status_code == 202

    # And the other way about: the page's job finds a terminal's move in
    # its way when its turn comes, and says so rather than running beside it.
    with moves._turn("renders", nas / "renders", get_settings()):
        await worker_runs(site, started.json()["id"])
    after = (await client.get("/api/v1/storage/move")).json()
    assert after["phase"] == "failed" and "Slideshow videos are being moved" in after["error"]
    assert stores.kind_root("exports") == data / "exports"


async def test_a_move_that_will_not_fit_is_refused_at_the_click(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")
    floor = int(get_settings().min_free_gb * 1024**3)
    monkeypatch.setattr(moves, "_free_bytes", lambda _folder: floor + 1)

    refused = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(nas / "renders"), **SURE}
    )

    assert refused.status_code == 400 and "not enough room" in message(refused)
    assert site["queued"] == [] and await audited(site) == []
    # Proxies are too many to count on a share inside a request: what the
    # catalogue says they take is used instead.
    async with site["factory"]() as db:
        library = Library(name="L", root_path=str(nas / "footage"))
        db.add(library)
        await db.flush()
        clip = Asset(
            library_id=library.id,
            relative_path="clip.mp4",
            filename="clip.mp4",
            extension="mp4",
            media_type="video",
            size_bytes=1,
            mtime=datetime.now(UTC),
        )
        db.add(clip)
        await db.flush()
        db.add(
            Derivative(
                asset_id=clip.id,
                kind="proxy",
                relative_path="derivatives/ab/abcd/proxy.mp4",
                media_format="mp4",
                status="ready",
                size_bytes=700 * 1024**2,
            )
        )
        await db.commit()
    proxies = await client.post(
        "/api/v1/storage/kinds/proxies/move", json={"target": str(nas / "proxies"), **SURE}
    )
    assert proxies.status_code == 400 and "700 MB" in message(proxies)


async def test_a_no_to_the_same_disk_question_leaves_nothing_behind(site: dict[str, Any]) -> None:
    """The question is for a share that is not mounted. A folder made in the
    place it mounts on, before the answer, is the first file of the mistake
    the question is there to stop."""
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")

    move = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(nas / "renders")}
    )
    bulk = await client.post("/api/v1/storage/bulk", json={"path": str(nas / "Framefound")})

    assert (move.status_code, bulk.status_code) == (428, 428)
    assert list(nas.iterdir()) == []


async def test_a_bulk_folder_that_no_longer_looks_like_one_is_asked_about(
    site: dict[str, Any],
) -> None:
    import shutil

    client, nas = site["client"], site["nas"]
    root = nas / "Framefound"
    assert (
        await client.post("/api/v1/storage/bulk", json={"path": str(root), **SURE})
    ).status_code == 200
    shutil.rmtree(root)  # the share went, and its folder with it

    asks = await client.post("/api/v1/storage/bulk", json={"path": str(root)})

    assert asks.status_code == 428 and "marker file is missing" in message(asks)
    assert not root.exists()
    assert (await kinds(site))["exports"]["connected"] is False
    # Told the drive was replaced, it is started again, empty.
    again = await client.post("/api/v1/storage/bulk", json={"path": str(root), **SURE})
    assert again.status_code == 200 and (await kinds(site))["exports"]["connected"] is True


async def test_a_store_that_is_gone_can_be_started_empty_where_it_was(
    site: dict[str, Any],
) -> None:
    """A replacement drive given the old one's name mounts at the same path.
    "Already live in" was the answer, to the one remedy the docs give."""
    client, data, nas = site["client"], site["data"], site["nas"]
    store = nas / "renders"
    store.mkdir()  # the new drive: the same path, and no marker
    stores.set_override(data, "renders", store)
    assert (await kinds(site))["renders"]["connected"] is False
    url = "/api/v1/storage/kinds/renders/move"

    copy = await client.post(url, json={"target": str(store), **SURE})
    assert copy.status_code == 400 and "start the folder empty" in message(copy)

    empty = await client.post(url, json={"target": str(store), "mode": "empty", **SURE})
    assert empty.status_code == 202, empty.text
    await worker_runs(site, empty.json()["id"])

    assert (await client.get("/api/v1/storage/move")).json()["phase"] == "done"
    assert (await kinds(site))["renders"]["connected"] is True


async def test_the_folder_a_kind_is_in_is_not_a_target_under_another_name(
    site: dict[str, Any],
) -> None:
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")
    started = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(nas / "renders"), **SURE}
    )
    await worker_runs(site, started.json()["id"])
    # A second name for the folder, as another capitalisation is on a Mac's
    # case-insensitive volumes.
    alias = nas / "renders-by-another-name"
    try:
        alias.symlink_to(nas / "renders", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine will not make a symlink")

    refused = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(alias), **SURE}
    )

    assert refused.status_code == 400 and "under another name" in message(refused)
    # The long way round to the same folder is seen through before that.
    roundabout = nas / "renders" / ".." / "renders"
    same = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(roundabout), **SURE}
    )
    assert same.status_code == 400 and "already live in" in message(same)
    assert (await kinds(site))["renders"]["old_path"] == str(data / "renders")


async def test_a_kind_in_the_bulk_folder_under_another_spelling_is_not_offered_a_move_into_it(
    site: dict[str, Any],
) -> None:
    """An install made with `--proxies <folder>/proxies`, and the bulk folder
    then typed with another capital: one folder, two strings. The page said
    "a folder of its own" and offered to move the proxies onto themselves."""
    client, data, nas = site["client"], site["data"], site["nas"]
    real = nas / "Framefound"
    stores.mark_proxy_store(real / "proxies")
    stores.set_override(data, "proxies", real / "proxies")
    as_typed = nas / "the-same-folder-as-typed"
    try:
        as_typed.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine will not make a symlink")

    done = await client.post("/api/v1/storage/bulk", json={"path": str(as_typed), **SURE})

    assert done.status_code == 200, done.text
    row = (await kinds(site))["proxies"]
    assert (row["where"], row["bulk_path"], row["connected"]) == ("bulk", None, True)


# --- who may ------------------------------------------------------------------------


async def test_only_an_administrator_may_change_where_things_live(site: dict[str, Any]) -> None:
    from framefound.auth import service

    async with site["factory"]() as db:
        await service.create_user(db, "editor@example.com", "another-strong-password", "user")
        await db.commit()
    nas = site["nas"]
    async with AsyncClient(transport=ASGITransport(app=site["app"]), base_url="http://t") as editor:
        signed_in = await editor.post(
            "/api/v1/auth/login",
            json={"email": "editor@example.com", "password": "another-strong-password"},
        )
        assert signed_in.status_code == 200, signed_in.text

        # Looking is for anyone signed in; the page just has no buttons.
        listing = await editor.get("/api/v1/storage/kinds")
        assert listing.status_code == 200 and listing.json()["can_move"] is False
        assert (await editor.get("/api/v1/storage/move")).status_code == 200

        for path, body in (
            ("/api/v1/storage/bulk", {"path": str(nas / "Framefound"), **SURE}),
            ("/api/v1/storage/kinds/renders/move", {"target": str(nas / "renders"), **SURE}),
            ("/api/v1/storage/move/cancel", {}),
            ("/api/v1/storage/kinds/renders/cleanup", {}),
            ("/api/v1/storage/proxies/rebuild", {}),
        ):
            assert (await editor.post(path, json=body)).status_code == 403, path

    assert list(nas.iterdir()) == [] and site["queued"] == []
    assert stores.bulk_root() is None and await audited(site) == []


# --- the System page ----------------------------------------------------------------


async def test_the_system_page_lists_bulk_storage_once_and_a_kind_on_its_own_drive(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unplugged, the bulk folder is one thing that is away, not five."""
    client, nas, ssd = site["client"], site["nas"], tmp_path / "ssd"
    monkeypatch.setenv("FRAMEFOUND_BULK_DIR", str(nas))  # there, but not mounted
    get_settings.cache_clear()
    stores.set_override(site["data"], "proxies", ssd)  # proxies on a drive of their own
    ssd.mkdir()

    async def volumes() -> dict[str, str]:
        system_api._VOLUMES_CACHE.clear()
        async with site["factory"]() as db:
            listed = await system_api._volumes(db, get_settings())
        return {volume.label: volume.status for volume in listed}

    away = await volumes()
    assert away["Bulk storage"] == "unreachable" and away["Proxies"] == "unreachable"
    assert not {"Slideshow videos", "Listing exports", "Basemaps", "Backup copies"} & set(away)
    alerts = (await client.get("/api/v1/system/alerts")).json()
    assert alerts[0]["title"] == "Bulk storage and Proxies are unreachable"

    stores.mark_bulk_root(nas)
    stores.mark_proxy_store(ssd)
    back = await volumes()
    assert "unreachable" not in back.values()


async def test_a_server_that_cannot_mount_drives_says_so_instead_of_offering_the_form(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Mac install has no mount table and no mount helper: the page was
    showing it the Linux add-a-drive form and a docker command."""
    monkeypatch.setattr(storage_api, "_can_mount", lambda: False)
    report = (await site["client"].get("/api/v1/storage")).json()
    assert report["drive_mounting"] is False
    assert "Finder" in report["hint"]

    monkeypatch.setattr(storage_api, "_can_mount", lambda: True)
    report = (await site["client"].get("/api/v1/storage")).json()
    assert report["drive_mounting"] is True
    assert "read-only" in report["hint"]


# --- removing old copies: only the folder the page showed ----------------------------


async def test_old_copies_are_only_removed_from_the_folder_the_page_showed(
    site: dict[str, Any],
) -> None:
    """The button names no folder: it removes from whatever "old" is when the
    click arrives. A second move finished while the page was open must not
    turn "remove the old copies in A" into "empty B"."""
    client, data, nas = site["client"], site["data"], site["nas"]
    put(data, RENDER, b"video")
    url = "/api/v1/storage/kinds/renders/cleanup"
    started = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(nas / "renders"), **SURE}
    )
    await worker_runs(site, started.json()["id"])
    shown = (await kinds(site))["renders"]["old_path"]
    assert shown == str(data / "renders")

    elsewhere = await client.post(url, json={"expected_folder": str(nas / "older")})

    assert elsewhere.status_code == 409
    assert message(elsewhere) == (
        f"The page showed old copies of slideshow videos in {nas / 'older'}, but the old "
        f"copies on record are in {data / 'renders'} now. Nothing was removed. Look at the "
        "Storage page again before removing anything."
    )
    assert (data / RENDER).is_file()
    assert [event for event, _ in await audited(site)] == ["storage.move_started"]

    # The folder it showed, spelt the long way round, is the same folder.
    roundabout = str(data / "renders" / ".." / "renders")
    removed = await client.post(url, json={"expected_folder": roundabout})
    assert removed.status_code == 200, removed.text
    assert (removed.json()["files"], removed.json()["folder"]) == (1, str(data / "renders"))
    assert not (data / RENDER).exists()

    # Shown before that removal and clicked again after it: nothing on record now.
    again = await client.post(url, json={"expected_folder": shown})
    assert again.status_code == 409 and "no old copies on record now" in message(again)
    # An older page that sends no folder is answered as before.
    assert (await client.post(url, json={})).status_code == 400


# --- making the missing proxies again, without a terminal ------------------------------


async def _proxy_wants(site: dict[str, Any]) -> tuple[Asset, Asset]:
    """A video whose proxy is on record and gone, and one that never got one."""
    async with site["factory"]() as db:
        library = Library(name="L", root_path="/media/x", generate_proxies=True)
        db.add(library)
        await db.flush()

        def video(name: str) -> Asset:
            return Asset(
                library_id=library.id,
                relative_path=f"{name}.mp4",
                filename=f"{name}.mp4",
                extension="mp4",
                media_type="video",
                size_bytes=1,
                mtime=datetime.now(UTC),
                processing_status="ready",
            )

        lost, skipped = video("lost"), video("skipped")
        db.add_all([lost, skipped])
        await db.flush()
        db.add(
            Derivative(
                asset_id=lost.id,
                kind="proxy",
                relative_path=f"derivatives/ab/{lost.id}/proxy.mp4",
                media_format="mp4",
                status="ready",
            )
        )
        await db.commit()
        return lost, skipped


async def test_the_missing_proxies_are_made_again_from_the_page(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.processing.tasks import generate_proxy

    lost, skipped = await _proxy_wants(site)
    asked: list[tuple[str, bool]] = []

    def queue(asset_id: str, requested: bool) -> None:
        asked.append((asset_id, requested))

    monkeypatch.setattr(generate_proxy, "delay", queue)

    response = await site["client"].post("/api/v1/storage/proxies/rebuild")

    assert response.status_code == 200, response.text
    assert response.json() == {"on_record": 1, "missing": 1, "never_made": 1, "queued": 2}
    # The one that existed is wanted whatever the library says today; the
    # one never made is left to the library's proxy switch, as the CLI does.
    assert asked == [(str(lost.id), True), (str(skipped.id), False)]
    assert await audited(site) == [
        ("storage.proxies_rebuilt", {"missing": 1, "never_made": 1, "queued": 2})
    ]


async def test_making_the_proxies_again_says_plainly_why_it_could_not(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.processing.tasks import generate_proxy

    await _proxy_wants(site)
    url = "/api/v1/storage/proxies/rebuild"

    def down(asset_id: str, requested: bool) -> None:
        raise ConnectionError("redis is not running")

    monkeypatch.setattr(generate_proxy, "delay", down)
    no_queue = await site["client"].post(url)
    assert no_queue.status_code == 503
    assert message(no_queue) == "The processing queue is unavailable"
    assert await audited(site) == []

    # Part-way: what was queued is said, and recorded.
    taken: list[str] = []

    def one_then_down(asset_id: str, requested: bool) -> None:
        if taken:
            raise ConnectionError("redis went away")
        taken.append(asset_id)

    monkeypatch.setattr(generate_proxy, "delay", one_then_down)
    halfway = await site["client"].post(url)
    assert halfway.status_code == 503 and "after 1 of 2 were queued" in message(halfway)
    assert [detail["queued"] for _event, detail in await audited(site)] == [1]

    # A proxy store that is away is not looked through: every proxy would
    # look missing, and thousands of them would be queued for nothing.
    asked: list[str] = []

    def queue(asset_id: str, requested: bool) -> None:
        asked.append(asset_id)

    monkeypatch.setattr(generate_proxy, "delay", queue)
    monkeypatch.setenv("FRAMEFOUND_PROXY_DIR", str(site["nas"] / "proxies"))
    get_settings.cache_clear()
    away = await site["client"].post(url)
    assert away.status_code == 503 and "proxy store is not connected" in message(away)
    assert str(site["nas"]) not in message(away) and asked == []


# --- the model library -----------------------------------------------------------------


async def test_a_folder_inside_the_one_a_kind_lives_in_is_not_a_target(
    site: dict[str, Any], tmp_path: Path
) -> None:
    """The kind's files are listed from its folder recursively, so a copy
    made inside that folder would be found and copied again on every pass,
    one level deeper each time. The model library lives outside the data
    folder from the start, so its very first move can name such a folder."""
    client, data, nas = site["client"], site["data"], site["nas"]
    put(tmp_path / "models" / "library" / "google" / "m-1" / ("a" * 40), "w.safetensors", b"w")

    nested = tmp_path / "models" / "library" / "moved"
    refused = await client.post(
        "/api/v1/storage/kinds/models/move", json={"target": str(nested), **SURE}
    )

    assert refused.status_code == 400, refused.text
    assert "is inside the folder models live in now" in message(refused)
    assert not nested.exists()
    assert (await client.get("/api/v1/storage/move")).json() is None  # no move was started

    # The same for a kind already moved to the NAS, and the long way round.
    put(data, RENDER, b"video")
    started = await client.post(
        "/api/v1/storage/kinds/renders/move", json={"target": str(nas / "renders"), **SURE}
    )
    await worker_runs(site, started.json()["id"])
    for target in (nas / "renders" / "deeper", nas / "other" / ".." / "renders" / "deeper"):
        again = await client.post(
            "/api/v1/storage/kinds/renders/move", json={"target": str(target), **SURE}
        )
        assert again.status_code == 400 and "is inside the folder" in message(again)
    assert (await kinds(site))["renders"]["path"] == str(nas / "renders")


async def test_the_model_library_is_sized_by_its_files_and_can_be_moved(
    site: dict[str, Any], tmp_path: Path
) -> None:
    """Counted as the move counts it: the files in its `library` folder. Its
    folder by default is the models folder, which also holds the ONNX and
    Whisper caches: those are not the library, and are not counted. A model
    still downloading has files in the library already, and they count."""
    put(tmp_path / "models", "clip/model.onnx", b"x" * 1000)
    shelf = tmp_path / "models" / "library" / "google"
    looker = shelf / "m-1" / ("a" * 40)
    put(looker, "model.safetensors", b"w" * 300)
    write_manifest(
        looker,
        Manifest(
            repo_id="google/m-1",
            revision="a" * 40,
            variant="",
            runtime="mlx-vlm",
            files=(RepoFile("model.safetensors", 300, None, None),),
            completed_at="2026-11-12T18:39:10Z",
        ),
    )
    manifest_size = (looker / "manifest.json").stat().st_size
    put(shelf / "m-2" / ("b" * 40), "model.safetensors", b"w" * 200)
    async with site["factory"]() as db:
        model = LocalModel(
            repo_id="google/m-1",
            repo_key="google/m-1",
            revision="a" * 40,
            publisher="google",
            runtime="mlx-vlm",
            size_bytes=300,
            state="installed",
            files=[{"path": "model.safetensors", "size": 300, "sha256": None, "blob_id": None}],
        )
        db.add(model)
        await db.flush()
        db.add(LocalAiRole(role="looker", model_id=model.id))
        await db.commit()

    row = (await kinds(site))["models"]

    assert (row["files"], row["size_bytes"]) == (3, 500 + manifest_size)
    assert row["movable"] is True
    # On this machine the models run from the folder; the page says so, and
    # what a move to another drive would add, never "nothing is copied".
    assert "runs from it while it is on this machine" in row["description"]
    assert "a model that has a role is also copied to this machine" in row["description"]
    assert "nothing is copied" not in row["description"]
    moved = await site["client"].post(
        "/api/v1/storage/kinds/models/move", json={"target": str(site["nas"] / "models"), **SURE}
    )
    assert moved.status_code == 202, moved.text
    await worker_runs(site, moved.json()["id"])
    assert stores.kind_root("models") == site["nas"] / "models"
    assert not (site["nas"] / "models" / "clip").exists()
    row = (await kinds(site))["models"]
    assert (row["files"], row["size_bytes"]) == (3, 500 + manifest_size)
    assert "copied to this machine" in row["description"]
    # The model with a role was given its copy here before the library left.
    here = tmp_path / "models" / "local-ai" / "google" / "m-1" / ("a" * 40)
    assert folder_problem(here) is None
    assert not (tmp_path / "models" / "local-ai" / "google" / "m-2").exists()
