"""The local mode of auto-edit: asking the local model about a listing's
photographs (design L4 S7), and applying what it proposed (S8).

The requests go through the real API and the job runs on the real lane:
`jobs.run_next` is awaited directly (no queue, no worker), the real `ask()`
stands in front of `fake.FakeModelServer`, and the Looker is L1's fixture
model shrunk into a library under `tmp_path` (`test_colour_jobs.Site`).
Waking the lane is a list's `append`, or False for "the queue is down".
Apply's `.delay` is a recorder; what it recorded is then run as the exports
worker would, `tasks.ai_edit_listing` in a thread. The photographs are
drawn JPEGs; nothing starts a process or reaches a network — trying to
fails the test.
"""

import asyncio
import json
import sqlite3
import subprocess
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_colour_jobs import ADMIN, LOOKER, SECRET, Site
from test_looks import _install

from framefound.api.v1 import clip_looks
from framefound.api.v1 import listing_suggestions as api
from framefound.api.v1 import listings as listings_api
from framefound.api.v1.local_models import QUEUE_DOWN, get_deps
from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    AiUsage,
    Asset,
    AssetEdit,
    AssetInpaint,
    Library,
    Listing,
    ListingEditSuggestion,
    ListingItem,
    LocalAiJob,
    User,
)
from framefound.local_models import fake, jobs
from framefound.media import develop, photo_judge
from framefound.processing import colour_jobs, local_ai_tasks, tasks
from framefound.storage import stores

Factory = async_sessionmaker[AsyncSession]

BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
SHOOT = "00-00 130 Davis Rd"

_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_MODELS_DIR",
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
    "FRAMEFOUND_ROOT_MAP",
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
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "0")
    # Progress written, and Cancel looked for, at every step.
    monkeypatch.setattr(jobs, "PROGRESS_EVERY_S", 0.0)
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    fake.FakeModelServer.answer_for("PhotoJudgement", None)
    get_settings.cache_clear()
    stores._CACHE.clear()


def _photo(path: Path, shade: int) -> None:
    """A drawn photograph: a sky over a lawn, a little different each time."""
    y, x = np.mgrid[0:240, 0:320]
    sky = y < 100
    arr = np.stack(
        [
            np.where(sky, 150 + shade, 60 + x * 60 / 320),
            np.where(sky, 170 + shade, 110 + y * 40 / 240),
            np.where(sky, 200, 50 + shade),
        ],
        axis=-1,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "RGB").save(path, "JPEG", quality=92)


def judged(
    *,
    rotate: float = 1.2,
    keystone: float = 0.0,
    sky: bool = False,
    caption: str = "Front exterior over the front lawn",
    file_name: str = "front-exterior-brick-colonial",
    note: str = "Levelled the roofline.",
) -> str:
    """A model's answer, as the text it writes."""
    return json.dumps(
        {
            "rotate": rotate,
            "keystone": keystone,
            "sky_worth_replacing": sky,
            "caption": caption,
            "file_name": file_name,
            "note": note,
        }
    )


class Shoot:
    """A listing of drawn photographs, made as rows (gallery order is the
    rows' positions, nothing rearranges them)."""

    def __init__(self, factory: Factory, root: Path, library_id: uuid.UUID) -> None:
        self.factory = factory
        self.root = root
        self.library_id = library_id
        self.listing_id = uuid.uuid4()
        self.items: list[uuid.UUID] = []
        self.assets: list[uuid.UUID] = []
        self.names: list[str] = []

    async def make(self, name: str = "130 Davis Rd Auction") -> "Shoot":
        async with self.factory() as db:
            db.add(Listing(id=self.listing_id, name=name))
            await db.commit()
        return self

    async def add(self, filename: str, *, readable: bool = True) -> uuid.UUID:
        relative = f"{SHOOT}/{filename}"
        path = self.root / relative
        if readable:
            _photo(path, len(self.items) * 7)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"this is not a photograph")
        async with self.factory() as db:
            asset = Asset(
                library_id=self.library_id,
                relative_path=relative,
                filename=filename,
                extension="jpg",
                media_type="image",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            item = ListingItem(
                listing_id=self.listing_id, asset_id=asset.id, position=len(self.items)
            )
            db.add(item)
            await db.commit()
        self.items.append(item.id)
        self.assets.append(asset.id)
        self.names.append(filename)
        return item.id


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    database = tmp_path / "listing.db"
    url = f"sqlite+aiosqlite:///{database.as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", SECRET)
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    # The exports worker reads its settings from the environment: the same
    # data folder as the lane's (`Site.data`), where a learned look lives.
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    # Apply's queue: what `.delay` was handed, run later by `_apply_run`.
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tasks.ai_edit_listing, "delay", lambda *args: queued.append(args))
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    site = Site(tmp_path)

    def wake(_settings: Any = None) -> bool:
        site.woken.append(1)
        return site.queue_up

    monkeypatch.setattr(local_ai_tasks, "wake", wake)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_deps] = site.deps
    root = tmp_path / "NAS"
    async with factory() as db:
        lib = Library(name="NAS", root_path=str(root))
        db.add(lib)
        await db.commit()
    shoot = await Shoot(factory, root, lib.id).make()
    for n in range(1, 4):
        await shoot.add(f"IMG_000{n}.jpg")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        setup = await http.post(
            "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
        )
        assert setup.status_code == 201, setup.text
        yield {
            "http": http,
            "factory": factory,
            "site": site,
            "shoot": shoot,
            "library": lib,
            "database": database,
            "queued": queued,
        }
    await engine.dispose()
    get_settings.cache_clear()


async def _drain(env: dict[str, Any], limit: int = 20) -> None:
    """What the lane does when woken: run jobs until none is queued."""
    for _ in range(limit):
        if not await jobs.run_next(env["factory"], env["site"].deps()):
            return
    raise AssertionError("the lane never ran out of work")


def _url(env: dict[str, Any], tail: str = "") -> str:
    return f"/api/v1/listings/{env['shoot'].listing_id}/local-edit{tail}"


async def _ask(env: dict[str, Any], **body: Any) -> httpx.Response:
    response: httpx.Response = await env["http"].post(_url(env), json=body or None)
    return response


async def _read(env: dict[str, Any]) -> dict[str, Any]:
    answer = await env["http"].get(_url(env))
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


async def _rows(env: dict[str, Any], model: type[Any]) -> list[Any]:
    async with env["factory"]() as db:
        return list(await db.scalars(select(model)))


async def _items(env: dict[str, Any]) -> list[dict[str, Any]]:
    """Every listing item, every column: what Asking must never change."""
    columns = [column.name for column in ListingItem.__table__.columns]
    return sorted(
        ({name: getattr(item, name) for name in columns} for item in await _rows(env, ListingItem)),
        key=lambda row: str(row["id"]),
    )


async def _job(env: dict[str, Any]) -> LocalAiJob:
    async with env["factory"]() as db:
        found = await db.scalar(
            select(LocalAiJob)
            .where(LocalAiJob.kind == colour_jobs.LISTING_KIND)
            .order_by(LocalAiJob.created_at.desc())
            .limit(1)
        )
    assert found is not None
    return found


def _texts(body: dict[str, Any]) -> str:
    return "\n".join(
        part["text"] for part in body["messages"][0]["content"] if part["type"] == "text"
    )


# --- asking, and what it leaves alone -----------------------------------------------------


async def test_asking_changes_no_photograph(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    shoot: Shoot = env["shoot"]
    await site.looker(env["factory"])
    async with env["factory"]() as db:
        # The operator named the second photograph themselves.
        item = await db.get(ListingItem, shoot.items[1])
        assert item is not None
        item.caption, item.slug, item.naming_source = "Kitchen", "kitchen-island", "confirmed"
        await db.commit()
    before = await _items(env)
    first = await _read(env)
    assert (first["mode"], first["ok"], first["note"]) == ("ready", True, None)
    assert (first["photographs"], first["proposed"], first["suggestions"]) == (3, 0, [])
    assert first["tone"] == {"from": "preset", "examples": 0}
    assert first["job"] is None and first["last_run"] is None

    asked = await _ask(env)
    assert asked.status_code == 202, asked.text
    assert asked.json()["to_look_at"] == 3
    assert asked.json()["job"]["state"] == "queued" and site.woken == [1]
    await _drain(env)

    # Each photograph shown once, on its own, with the photograph question.
    assert len(site.server.requests) == 3
    for request in site.server.requests:
        assert len([p for p in request["messages"][0]["content"] if p["type"] != "text"]) == 1
        assert photo_judge.INSTRUCTIONS in _texts(request)
        assert request["response_format"]["json_schema"]["name"] == "PhotoJudgement"

    # Proposals stored, and nothing else: no edit version, no name, no run.
    assert await _rows(env, AssetEdit) == []
    assert await _items(env) == before
    async with env["factory"]() as db:
        listing = await db.get(Listing, shoot.listing_id)
        assert listing is not None
        assert (listing.ai_edit_state, listing.ai_edit_mode) == ("idle", "")

    body = await _read(env)
    assert (body["photographs"], body["proposed"], body["applied"], body["dismissed"]) == (
        3,
        3,
        0,
        0,
    )
    assert [s["filename"] for s in body["suggestions"]] == shoot.names
    assert [s["position"] for s in body["suggestions"]] == [0, 1, 2]
    assert [s["name_is_yours"] for s in body["suggestions"]] == [False, True, False]
    for suggestion in body["suggestions"]:
        assert suggestion["state"] == "proposed"
        assert suggestion["provenance"]["model"] == LOOKER
        assert suggestion["provenance"]["prompt_version"] == photo_judge.PROMPT_VERSION
        # The fake model server marks everything it says as made up.
        assert suggestion["demo"] is True and suggestion["made_at"].endswith("Z")
        assert -5 <= suggestion["rotate"] <= 5 and -1 <= suggestion["keystone"] <= 1
    assert body["job"] is None
    assert body["last_run"] == {"looked": 3, "failed": 0, "unreadable": 0, "first_problem": None}
    job = await _job(env)
    assert (job.state, job.model, job.prompt_version) == ("done", LOOKER, "listing-look-1")
    assert jobs.title_of(job) == "Looking at the photographs of 130 Davis Rd Auction"


async def test_an_answer_becomes_the_proposal_cleaned(env: dict[str, Any]) -> None:
    await env["site"].looker(env["factory"])
    fake.FakeModelServer.answer_for(
        "PhotoJudgement",
        lambda _body: judged(
            rotate=-3.4,
            keystone=0.03,
            sky=True,
            caption="  Front   exterior ",
            file_name="Front Exterior 130 Davis",
        ),
    )
    assert (await _ask(env)).status_code == 202
    await _drain(env)
    first = (await _read(env))["suggestions"][0]
    assert (first["rotate"], first["keystone"], first["large"]) == (-3.4, 0.0, True)
    assert (first["caption"], first["slug"]) == ("Front exterior", "front-exterior-davis")
    assert first["sky_worth_replacing"] is True
    assert first["note"] == "Levelled the roofline."


async def test_one_photograph_the_model_fails_on_does_not_stop_the_rest(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    shoot: Shoot = env["shoot"]
    await shoot.add("IMG_0004.jpg", readable=False)
    await site.looker(env["factory"])
    # The first photograph is answered in words, twice (the ask and its repair).
    site.server.prose_first(2)
    assert (await _ask(env)).json()["to_look_at"] == 4
    await _drain(env)
    # Asked about the first twice, the next two once; the unreadable one never.
    assert len(site.server.requests) == 4
    body = await _read(env)
    assert [s["filename"] for s in body["suggestions"]] == ["IMG_0002.jpg", "IMG_0003.jpg"]
    run = body["last_run"]
    assert (run["looked"], run["failed"], run["unreadable"]) == (2, 1, 1)
    assert run["first_problem"].startswith("IMG_0001.jpg: Its answer was not the form asked for")
    assert body["job"] is None
    assert (await _job(env)).state == "done"

    # Asked again, only those with no suggestion are looked at.
    asked = await _ask(env)
    assert asked.json()["to_look_at"] == 2
    await _drain(env)
    run = (await _read(env))["last_run"]
    assert (run["looked"], run["failed"], run["unreadable"]) == (1, 0, 1)
    assert run["first_problem"] == f"IMG_0004.jpg: {colour_jobs.UNREADABLE}"


async def test_three_failures_in_a_row_stop_the_run_and_say_why(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    shoot: Shoot = env["shoot"]
    for n in range(4, 6):
        await shoot.add(f"IMG_000{n}.jpg")
    await site.looker(env["factory"])
    site.server.refuse("died", times=3)
    assert (await _ask(env)).status_code == 202
    await _drain(env)
    # Three photographs, then nothing more: the other two were never shown.
    assert len(site.server.requests) == 3
    assert await _rows(env, ListingEditSuggestion) == []
    body = await _read(env)
    job = body["job"]
    assert job["state"] == "failed"
    assert job["message"].startswith(
        "The model failed on 3 photographs in a row, so the rest were not looked at."
    )
    assert "stopped unexpectedly" in job["message"]
    run = body["last_run"]
    assert (run["looked"], run["failed"], run["unreadable"]) == (0, 3, 0)
    assert run["first_problem"].startswith("IMG_0001.jpg: The model stopped unexpectedly")

    # A model that will not load stops the run at the first photograph, in L1's words.
    site.server.refuse("load_failed")
    assert (await _ask(env)).status_code == 202
    await _drain(env)
    assert len(site.server.requests) == 4
    job = (await _read(env))["job"]
    assert (job["state"], job["message"]) == ("failed", "It would not load.")
    # And once it answers, the listing is looked at after all.
    assert (await _ask(env)).json()["to_look_at"] == 5
    await _drain(env)
    assert (await _read(env))["proposed"] == 5


async def test_a_run_that_had_to_wait_carries_on_where_it_stopped(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    site: Site = env["site"]
    shoot: Shoot = env["shoot"]
    await shoot.add("IMG_0004.jpg")
    await site.looker(env["factory"])
    server = site.server
    answer = server.chat

    def busy_at_the_third(body: dict[str, Any], **more: Any) -> dict[str, Any]:
        if len(server.requests) == 2:
            server.refuse("busy")
        return answer(body, **more)

    monkeypatch.setattr(server, "chat", busy_at_the_third)
    assert (await _ask(env)).status_code == 202
    await _drain(env)
    # Two looked at, then the server was busy: the job waits, it does not fail.
    job = (await _read(env))["job"]
    assert job["state"] == "waiting" and job["not_before"].endswith("Z")
    assert (job["done"], job["total"]) == (2, 4)
    assert len(await _rows(env, ListingEditSuggestion)) == 2

    site.clock.at += timedelta(minutes=2)
    monkeypatch.setattr(server, "chat", answer)
    await _drain(env)
    # The two it had looked at were not asked about again.
    assert len(server.requests) == 2 + 1 + 2
    body = await _read(env)
    assert body["proposed"] == 4 and body["job"] is None
    assert body["last_run"]["looked"] == 4

    # Every photograph again, with something more urgent queued part-way: it
    # steps aside, and carries on from where it was — not from the start.
    asked: list[int] = []
    urgent = jobs.JobContext.should_yield

    async def once_at_the_second(ctx: jobs.JobContext) -> bool:
        asked.append(1)
        return len(asked) == 2 or await urgent(ctx)

    monkeypatch.setattr(jobs.JobContext, "should_yield", once_at_the_second)
    redo = await _ask(env, redo=True)
    assert redo.status_code == 202 and redo.json()["to_look_at"] == 4
    await _drain(env)
    assert len(server.requests) == 5 + 4
    finished = await _job(env)
    assert finished.state == "done"
    assert finished.result == {"looked": 4, "failed": 0, "unreadable": 0, "first_problem": None}


async def test_asking_again_only_looks_at_what_is_new(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    shoot: Shoot = env["shoot"]
    await site.looker(env["factory"])
    assert (await _ask(env)).status_code == 202
    await _drain(env)
    assert len(site.server.requests) == 3

    nothing_new = await _ask(env)
    assert nothing_new.status_code == 409
    assert nothing_new.json()["error"]["message"] == api.ALL_PROPOSED

    await shoot.add("IMG_0004.jpg")
    asked = await _ask(env)
    assert asked.json()["to_look_at"] == 1
    await _drain(env)
    assert len(site.server.requests) == 4
    assert (await _read(env))["proposed"] == 4


async def test_dismissing_puts_proposals_aside_and_asking_again_brings_them_back(
    env: dict[str, Any],
) -> None:
    shoot: Shoot = env["shoot"]
    await env["site"].looker(env["factory"])
    assert (await _ask(env)).status_code == 202
    await _drain(env)
    one = await env["http"].post(
        _url(env, "/dismiss"), json={"item_ids": [str(shoot.items[0]), str(uuid.uuid4())]}
    )
    assert one.status_code == 200, one.text
    body = one.json()
    assert (body["proposed"], body["dismissed"]) == (2, 1)
    assert body["suggestions"][0]["state"] == "dismissed"
    rest = await env["http"].post(_url(env, "/dismiss"))
    assert (rest.json()["proposed"], rest.json()["dismissed"]) == (0, 3)
    # Dismissed is still asked about: only `redo` looks again.
    assert (await _ask(env)).status_code == 409
    assert (await _ask(env, redo=True)).json()["to_look_at"] == 3
    await _drain(env)
    body = await _read(env)
    assert (body["proposed"], body["dismissed"]) == (3, 0)
    assert await _rows(env, AssetEdit) == []


# --- refusals ----------------------------------------------------------------------------


async def test_asking_is_refused_where_models_cannot_run(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    site: Site = env["site"]
    # No model has the Looker role.
    refused = await _ask(env)
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == clip_looks.NO_LOOKER
    body = await _read(env)
    assert (body["ok"], body["note"]) == (False, clip_looks.NO_LOOKER)
    # The container server: no model can run at all. Reading still works.
    monkeypatch.setattr(site, "settings", Settings(data_dir=site.data, secret_key=SECRET))
    refused = await _ask(env)
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == api.CANNOT_ASK_HERE
    body = await _read(env)
    assert (body["mode"], body["ok"], body["note"]) == (
        "unavailable",
        False,
        api.CANNOT_ASK_HERE,
    )
    assert body["photographs"] == 3
    assert await _rows(env, LocalAiJob) == []
    assert site.woken == []


async def test_a_server_that_cannot_run_models_says_so_in_the_listings_own_words(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clip card's sentence ends with the learned look and the LUT
    download, and a listing page has neither. The listing's names its own two
    buttons as what still works, and they do."""
    site: Site = env["site"]
    monkeypatch.setattr(site, "settings", Settings(data_dir=site.data, secret_key=SECRET))

    note = (await _read(env))["note"]

    assert note == api.CANNOT_ASK_HERE
    assert "Mac install of FrameFound" in note and "This server cannot run one." in note
    assert "LUT" not in note and "learned look" not in note
    assert "Auto-edit photos" in note and "Name photos with AI" in note
    # The clip card keeps the sentence written for it.
    assert clip_looks.CANNOT_SUGGEST_HERE.endswith(
        "The learned look and the LUT download work here as they are."
    )
    assert (await _suggest_status(env)).note == clip_looks.CANNOT_SUGGEST_HERE

    # "Work here as before": naming asks for what it always asked for, an
    # Anthropic key, and says nothing about a model on this machine;
    # Auto-edit starts, with the preset when there is no key.
    url = f"/api/v1/listings/{env['shoot'].listing_id}/ai-edit"
    naming = await env["http"].post(url, json={"mode": "describe"})
    assert naming.status_code == 400
    assert "Anthropic key" in naming.json()["error"]["message"]
    editing = await env["http"].post(url, json={"mode": "edit"})
    assert editing.status_code == 202, editing.text
    assert (editing.json()["queued"], editing.json()["mode"]) == (3, "preset")
    assert [args[2] for args in env["queued"]] == ["preset"]


async def _suggest_status(env: dict[str, Any]) -> Any:
    """What the clip card is told on the same server."""
    async with env["factory"]() as db:
        return await clip_looks._suggest(db, env["site"].deps())


async def test_a_listing_is_asked_about_once_at_a_time_and_never_left_waiting_in_vain(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    site.queue_up = False
    down = await _ask(env)
    assert down.status_code == 503
    assert down.json()["error"]["message"] == QUEUE_DOWN
    assert await _rows(env, LocalAiJob) == []
    assert (await _read(env))["job"] is None

    site.queue_up = True
    assert (await _ask(env)).status_code == 202
    again = await _ask(env, redo=True)
    assert again.status_code == 409
    assert again.json()["error"]["message"] == api.ALREADY_LOOKING
    assert len(await _rows(env, LocalAiJob)) == 1

    # A job whose worker died does not stand in the way.
    async with env["factory"]() as db:
        job = await db.scalar(select(LocalAiJob))
        assert job is not None
        job.state, job.started_at = "running", datetime(2026, 1, 1, tzinfo=UTC)
        job.beat_at = job.started_at
        await db.commit()
    assert (await _ask(env)).status_code == 202
    states = sorted(row.state for row in await _rows(env, LocalAiJob))
    assert states == ["failed", "queued"]


async def test_nothing_to_look_at_is_said_in_words(env: dict[str, Any]) -> None:
    await env["site"].looker(env["factory"])
    missing = await env["http"].post(f"/api/v1/listings/{uuid.uuid4()}/local-edit")
    assert missing.status_code == 404
    empty = await Shoot(env["factory"], env["shoot"].root, env["library"].id).make("Empty")
    refused = await env["http"].post(f"/api/v1/listings/{empty.listing_id}/local-edit")
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == api.NO_PHOTOGRAPHS
    extra = await _ask(env, redo=False, everything=True)
    assert extra.status_code == 422


# --- cancelling ----------------------------------------------------------------------------


async def test_cancel_stops_before_the_next_photograph_and_keeps_what_was_proposed(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    # Cancel while waiting: ended at once, nothing asked.
    assert (await _ask(env)).status_code == 202
    cancelled = await env["http"].post(_url(env, "/cancel"))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["job"]["state"] == "cancelled"
    await _drain(env)
    assert site.server.requests == []
    again = await env["http"].post(_url(env, "/cancel"))
    assert again.status_code == 409
    assert again.json()["error"]["message"] == api.NOTHING_LOOKING

    # Cancel pressed while the model looks at the first photograph. Progress is
    # written (and Cancel read with it) at most every few seconds; a model
    # that answers sooner must still not be asked about the next photograph.
    monkeypatch.setattr(jobs, "PROGRESS_EVERY_S", 3600.0)
    server = site.server
    answer = server.chat

    def press_cancel(body: dict[str, Any], **more: Any) -> dict[str, Any]:
        with sqlite3.connect(env["database"]) as conn:
            conn.execute("UPDATE local_ai_jobs SET cancel_requested = 1 WHERE state = 'running'")
        return answer(body, **more)

    monkeypatch.setattr(server, "chat", press_cancel)
    assert (await _ask(env)).status_code == 202
    await _drain(env)
    assert len(server.requests) == 1
    assert (await _job(env)).state == "cancelled"
    body = await _read(env)
    assert body["proposed"] == 1 and body["job"] is None
    assert body["last_run"]["looked"] == 1


async def test_a_request_cancelled_before_it_looked_keeps_the_last_runs_summary(
    env: dict[str, Any],
) -> None:
    shoot: Shoot = env["shoot"]
    await shoot.add("IMG_0004.jpg", readable=False)
    await env["site"].looker(env["factory"])
    assert (await _ask(env)).status_code == 202
    await _drain(env)
    before = (await _read(env))["last_run"]
    assert (before["looked"], before["unreadable"]) == (3, 1)
    # Asked again by mistake, and cancelled before it looked at anything: the
    # proposals on the page are still the first run's, and so is what it said.
    assert (await _ask(env, redo=True)).status_code == 202
    assert (await env["http"].post(_url(env, "/cancel"))).status_code == 200
    assert (await _job(env)).state == "cancelled"
    assert (await _read(env))["last_run"] == before


async def test_a_library_that_is_away_is_waited_for_not_called_unreadable(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    shoot: Shoot = env["shoot"]
    await site.looker(env["factory"])
    assert (await _ask(env)).status_code == 202
    # The NAS is asleep overnight: its share is not mounted.
    asleep = shoot.root.with_name("NAS asleep")
    shoot.root.rename(asleep)
    await _drain(env)
    assert site.server.requests == []
    job = (await _read(env))["job"]
    assert job["state"] == "waiting" and job["not_before"].endswith("Z")
    assert job["message"] == colour_jobs.LIBRARY_AWAY.format(
        reason="Library folder is not reachable."
    )
    # Nothing counted against the photographs: they are not damaged.
    assert await _rows(env, ListingEditSuggestion) == []
    assert colour_jobs.PROGRESS not in (await _job(env)).payload

    # The share is back: the job carries on by itself.
    asleep.rename(shoot.root)
    site.clock.at += colour_jobs.LIBRARY_RETRY + timedelta(minutes=1)
    await _drain(env)
    body = await _read(env)
    assert body["proposed"] == 3 and body["job"] is None
    assert body["last_run"] == {"looked": 3, "failed": 0, "unreadable": 0, "first_problem": None}


# --- the preview ---------------------------------------------------------------------------


async def test_the_preview_is_the_recipe_apply_would_write_less_the_sky(
    env: dict[str, Any],
) -> None:
    shoot: Shoot = env["shoot"]
    async with env["factory"]() as db:
        db.add(
            ListingEditSuggestion(
                listing_id=shoot.listing_id,
                item_id=shoot.items[0],
                asset_id=shoot.assets[0],
                rotate=2.0,
                keystone=0.0,
                sky_worth_replacing=True,
            )
        )
        await db.commit()
    url = _url(env, f"/{shoot.items[0]}/preview.jpg")
    shown = await env["http"].get(url)
    assert shown.status_code == 200, shown.text
    assert shown.headers["content-type"] == "image/jpeg"
    assert shown.headers["cache-control"] == "no-store"

    original = Image.open(shoot.root / SHOOT / shoot.names[0]).convert("RGB")

    def expected(recipe: dict[str, Any]) -> Any:
        return np.asarray(develop.render(original, develop.clean_recipe(recipe)), dtype=float)

    def got(response: httpx.Response) -> Any:
        import io

        return np.asarray(Image.open(io.BytesIO(response.content)).convert("RGB"), dtype=float)

    straightened = expected({**develop.LISTING_PRESET, "rotate": 2.0})
    as_shot = expected(dict(develop.LISTING_PRESET))
    picture = got(shown)
    assert picture.shape == straightened.shape
    assert np.abs(picture - straightened).mean() < 3.0
    assert np.abs(picture - as_shot).mean() > np.abs(picture - straightened).mean()
    flat = got(await env["http"].get(url, params={"straighten": "false"}))
    assert np.abs(flat - as_shot).mean() < 3.0

    # A photograph with no suggestion, and one whose file is gone.
    none = await env["http"].get(_url(env, f"/{shoot.items[1]}/preview.jpg"))
    assert none.status_code == 404
    assert none.json()["error"]["message"] == api.NO_SUGGESTION_HERE
    (shoot.root / SHOOT / shoot.names[0]).unlink()
    gone = await env["http"].get(url)
    assert gone.status_code == 404
    assert gone.json()["error"]["message"] == api.NOT_REACHABLE


async def test_the_model_and_the_preview_start_from_the_pixels_apply_uses(
    env: dict[str, Any],
) -> None:
    shoot: Shoot = env["shoot"]
    data = env["site"].data
    _photo(data / "inpaint" / "first-v2.jpg", 9)
    async with env["factory"]() as db:
        for asset_id, version, status, relative in (
            (shoot.assets[0], 1, "ready", "inpaint/first-v1.jpg"),
            (shoot.assets[0], 2, "ready", "inpaint/first-v2.jpg"),
            # Not finished: the original is where it starts.
            (shoot.assets[1], 1, "running", "inpaint/second-v1.jpg"),
            # Finished, but its file is gone: the original again.
            (shoot.assets[2], 1, "ready", "inpaint/third-v1.jpg"),
        ):
            db.add(
                AssetInpaint(
                    asset_id=asset_id, version=version, status=status, relative_path=relative
                )
            )
        await db.commit()
        rows = (
            await db.execute(
                select(ListingItem, Asset, Library)
                .join(Asset, Asset.id == ListingItem.asset_id)
                .join(Library, Library.id == Asset.library_id)
                .where(ListingItem.listing_id == shoot.listing_id)
                .order_by(ListingItem.position)
            )
        ).all()
        # The preview and the model (in a thread, under SHARE_WAIT_S), and
        # export and Apply (on the worker's own loop): one rule, two callers.
        ours = await colour_jobs.render_sources(db, data, rows)
        theirs = await tasks._render_sources(db, data, rows)
    assert ours == theirs
    assert ours == {
        shoot.assets[0]: (data / "inpaint" / "first-v2.jpg", True),
        shoot.assets[1]: (shoot.root / SHOOT / shoot.names[1], False),
        shoot.assets[2]: (shoot.root / SHOOT / shoot.names[2], False),
    }


async def test_the_preview_reads_the_share_through_the_same_file_half_as_export(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The listing preview once carried its own copy of where a render starts;
    # a fix to export's rule would not have reached it. Now there is one file
    # half, and every caller goes through it.
    shoot: Shoot = env["shoot"]
    async with env["factory"]() as db:
        db.add(
            ListingEditSuggestion(
                listing_id=shoot.listing_id, item_id=shoot.items[0], asset_id=shoot.assets[0]
            )
        )
        await db.commit()
    asked: list[list[Any]] = []

    def nowhere(data_dir: Path, places: list[Any], removed: dict[Any, str]) -> dict[Any, Any]:
        asked.append(places)
        return {}

    monkeypatch.setattr(tasks, "locate_render_sources", nowhere)
    shown = await env["http"].get(_url(env, f"/{shoot.items[0]}/preview.jpg"))
    assert shown.status_code == 404
    assert shown.json()["error"]["message"] == api.NOT_REACHABLE
    assert asked == [[(shoot.assets[0], f"{SHOOT}/{shoot.names[0]}", str(shoot.root))]]


async def test_a_share_that_hangs_holds_neither_the_preview_nor_the_server(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    shoot: Shoot = env["shoot"]
    async with env["factory"]() as db:
        db.add(
            ListingEditSuggestion(
                listing_id=shoot.listing_id,
                item_id=shoot.items[0],
                asset_id=shoot.assets[0],
                rotate=2.0,
            )
        )
        await db.commit()
    released = threading.Event()
    where = tasks.locate_render_sources

    def hung(*args: Any) -> Any:
        # A stat on a share that does not answer, until the test lets it go.
        released.wait(30)
        return where(*args)

    monkeypatch.setattr(tasks, "locate_render_sources", hung)
    monkeypatch.setattr(colour_jobs, "SHARE_WAIT_S", 0.2)
    try:
        shown, other = await asyncio.gather(
            env["http"].get(_url(env, f"/{shoot.items[0]}/preview.jpg")),
            env["http"].get(_url(env)),
        )
        # The other page was answered while the share was silent.
        assert other.status_code == 200
        assert shown.status_code == 404
        assert shown.json()["error"]["message"] == api.NOT_REACHABLE
    finally:
        released.set()


# --- applying ------------------------------------------------------------------------------


async def _propose(
    env: dict[str, Any],
    index: int,
    *,
    rotate: float = 0.0,
    keystone: float = 0.0,
    sky: bool = False,
    caption: str = "",
    slug: str = "",
    state: str = "proposed",
) -> None:
    """A proposal as the lane stores one, for the shoot's `index`-th photograph."""
    shoot: Shoot = env["shoot"]
    async with env["factory"]() as db:
        db.add(
            ListingEditSuggestion(
                listing_id=shoot.listing_id,
                item_id=shoot.items[index],
                asset_id=shoot.assets[index],
                rotate=rotate,
                keystone=keystone,
                sky_worth_replacing=sky,
                caption=caption,
                slug=slug,
                state=state,
            )
        )
        await db.commit()


async def _apply(env: dict[str, Any], **body: Any) -> httpx.Response:
    response: httpx.Response = await env["http"].post(
        f"/api/v1/listings/{env['shoot'].listing_id}/ai-edit", json={"mode": "local", **body}
    )
    return response


async def _work(env: dict[str, Any]) -> None:
    """The exports worker: run what Apply queued, in order, as Celery would."""
    queued: list[tuple[Any, ...]] = env["queued"]
    while queued:
        await asyncio.to_thread(tasks.ai_edit_listing, *queued.pop(0))


async def _recipes(env: dict[str, Any]) -> dict[uuid.UUID, dict[str, Any]]:
    """Each photograph's newest recipe, by asset."""
    newest: dict[uuid.UUID, dict[str, Any]] = {}
    for edit in sorted(await _rows(env, AssetEdit), key=lambda row: row.version):
        newest[edit.asset_id] = edit.recipe
    return newest


async def _proposals(env: dict[str, Any]) -> dict[uuid.UUID, ListingEditSuggestion]:
    return {row.item_id: row for row in await _rows(env, ListingEditSuggestion)}


async def _listing(env: dict[str, Any]) -> Listing:
    async with env["factory"]() as db:
        found = await db.get(Listing, env["shoot"].listing_id)
    assert found is not None
    return found


async def _admin(env: dict[str, Any]) -> uuid.UUID:
    async with env["factory"]() as db:
        found = await db.scalar(select(User.id).where(User.email == ADMIN["email"]))
    assert found is not None
    return found


async def test_apply_writes_tone_from_the_look_and_geometry_from_the_model(
    env: dict[str, Any],
) -> None:
    shoot: Shoot = env["shoot"]
    _install(env["site"].data, {"exposure": 0.33, "vibrance": 0.21})
    await _propose(env, 0, rotate=1.5, keystone=0.2, caption="Front exterior", slug="front")
    await _propose(env, 1, rotate=-0.8, caption="Kitchen island", slug="kitchen-island")
    await _propose(env, 2)

    asked = await _apply(env)
    assert asked.status_code == 202, asked.text
    assert asked.json() == {"queued": 3, "mode": "local", "look": 12}
    # Queued with the operator's choices, which the worker alone reads.
    [(listing_id, sky_name, mode, options)] = env["queued"]
    assert (listing_id, sky_name, mode) == (str(shoot.listing_id), None, "local")
    assert options == {
        "item_ids": None,
        "straighten": True,
        "names": True,
        "sky": "suggested",
        "user_id": str(await _admin(env)),
    }
    shown = await env["http"].get(f"/api/v1/listings/{shoot.listing_id}")
    assert (shown.json()["ai_edit_state"], shown.json()["ai_edit_mode"]) == ("queued", "local")
    await _work(env)

    recipes = await _recipes(env)
    tone = {"exposure": 0.33, "vibrance": 0.21}
    assert recipes[shoot.assets[0]] == pytest.approx(
        develop.clean_recipe({**tone, "rotate": 1.5, "keystone": 0.2})
    )
    assert recipes[shoot.assets[1]] == pytest.approx(develop.clean_recipe({**tone, "rotate": -0.8}))
    # Nothing to straighten: the tone alone, and never the preset's.
    assert recipes[shoot.assets[2]] == pytest.approx(develop.clean_recipe(tone))
    assert "auto_wb" not in recipes[shoot.assets[2]]

    items = {row["id"]: row for row in await _items(env)}
    assert (items[shoot.items[0]]["caption"], items[shoot.items[0]]["slug"]) == (
        "Front exterior",
        "front",
    )
    assert items[shoot.items[0]]["naming_source"] == "suggested"
    # The model said nothing about the third photograph's name: none is made up.
    assert items[shoot.items[2]]["naming_source"] != "suggested"

    admin = await _admin(env)
    for row in (await _proposals(env)).values():
        assert (row.state, row.applied_version, row.decided_by) == ("applied", 1, admin)
        assert row.decided_at is not None
    listing = await _listing(env)
    assert (listing.ai_edit_state, listing.ai_edit_mode, listing.ai_edit_message) == (
        "done",
        "local",
        "3 edited",
    )
    body = await _read(env)
    assert (body["proposed"], body["applied"]) == (0, 3)
    # Applied once is applied: pressing again has nothing to apply.
    again = await _apply(env)
    assert again.status_code == 400
    assert again.json()["error"]["message"] == listings_api.NOTHING_PROPOSED


async def test_without_a_look_the_tone_is_the_preset(env: dict[str, Any]) -> None:
    shoot: Shoot = env["shoot"]
    await _propose(env, 0, rotate=2.0)
    assert (await _apply(env)).json() == {"queued": 1, "mode": "local", "look": 0}
    await _work(env)
    recipes = await _recipes(env)
    assert recipes == {
        shoot.assets[0]: develop.clean_recipe({**develop.LISTING_PRESET, "rotate": 2.0})
    }


async def test_apply_never_asks_a_model(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.ai import recipe_picker
    from framefound.local_models import ask

    site: Site = env["site"]
    await site.looker(env["factory"])
    assert (await _ask(env)).status_code == 202
    await _drain(env)
    asked = len(site.server.requests)
    assert asked == 3

    # Claude is configured too: Apply must use neither it nor the local model.
    stored = await env["http"].put("/api/v1/develop/settings/ai", json={"api_key": "sk-ant-test"})
    assert stored.status_code == 200, stored.text

    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Apply asked a model")

    for target, name in (
        (recipe_picker, "pick_recipe"),
        (recipe_picker, "describe_photo"),
        (ask, "ask"),
    ):
        monkeypatch.setattr(target, name, never)
    applied = await _apply(env)
    assert applied.status_code == 202, applied.text
    assert applied.json()["mode"] == "local"
    await _work(env)
    assert len(site.server.requests) == asked
    assert await _rows(env, AiUsage) == []
    assert len(await _rows(env, AssetEdit)) == 3
    listing = await _listing(env)
    assert (listing.ai_edit_state, listing.ai_edit_message) == ("done", "3 edited")


async def test_apply_touches_only_the_ticked_photographs(env: dict[str, Any]) -> None:
    shoot: Shoot = env["shoot"]
    for index in range(3):
        await _propose(env, index, rotate=1.0, caption=f"Room {index}", slug=f"room-{index}")
    before = await _items(env)
    ticked = [str(shoot.items[0]), str(shoot.items[2])]
    applied = await _apply(env, item_ids=ticked)
    assert applied.json()["queued"] == 2
    assert env["queued"][0][3]["item_ids"] == ticked
    await _work(env)

    assert set(await _recipes(env)) == {shoot.assets[0], shoot.assets[2]}
    states = {item: row.state for item, row in (await _proposals(env)).items()}
    assert states == {
        shoot.items[0]: "applied",
        shoot.items[1]: "proposed",
        shoot.items[2]: "applied",
    }
    after = {row["id"]: row for row in await _items(env)}
    untouched = next(row for row in before if row["id"] == shoot.items[1])
    assert after[shoot.items[1]] == untouched
    # Ticking a photograph with nothing waiting adds nothing to do.
    nothing = await _apply(env, item_ids=[str(shoot.items[0])])
    assert nothing.status_code == 400
    assert nothing.json()["error"]["message"] == listings_api.NOTHING_PROPOSED


async def test_a_large_correction_is_marked_and_applied_only_when_ticked(
    env: dict[str, Any],
) -> None:
    shoot: Shoot = env["shoot"]
    await _propose(env, 0, rotate=0.6)
    await _propose(env, 1, rotate=4.0, keystone=0.7)
    marked = {s["item_id"]: s["large"] for s in (await _read(env))["suggestions"]}
    assert marked == {str(shoot.items[0]): False, str(shoot.items[1]): True}

    # The page starts the large one unticked; Apply edits what is ticked.
    assert (await _apply(env, item_ids=[str(shoot.items[0])])).status_code == 202
    await _work(env)
    assert set(await _recipes(env)) == {shoot.assets[0]}

    assert (await _apply(env, item_ids=[str(shoot.items[1])])).status_code == 202
    await _work(env)
    large = (await _recipes(env))[shoot.assets[1]]
    assert (large["rotate"], large["keystone"]) == (4.0, 0.7)


async def test_straightening_and_names_can_each_be_left_out(env: dict[str, Any]) -> None:
    shoot: Shoot = env["shoot"]
    await _propose(env, 0, rotate=2.5, keystone=0.3, caption="Front", slug="front")
    before = await _items(env)
    assert (await _apply(env, straighten=False, names=False)).status_code == 202
    await _work(env)
    assert (await _recipes(env))[shoot.assets[0]] == develop.clean_recipe(
        dict(develop.LISTING_PRESET)
    )
    assert await _items(env) == before
    assert (await _proposals(env))[shoot.items[0]].state == "applied"


async def test_a_confirmed_name_is_never_overwritten(env: dict[str, Any]) -> None:
    shoot: Shoot = env["shoot"]
    async with env["factory"]() as db:
        item = await db.get(ListingItem, shoot.items[1])
        assert item is not None
        item.caption, item.slug, item.naming_source = "Kitchen", "kitchen-island", "confirmed"
        await db.commit()
    await _propose(env, 0, caption="Front exterior", slug="front-exterior")
    await _propose(env, 1, rotate=1.0, caption="Dining room", slug="dining-room")
    assert (await _apply(env)).status_code == 202
    await _work(env)
    items = {row["id"]: row for row in await _items(env)}
    assert (items[shoot.items[0]]["slug"], items[shoot.items[0]]["naming_source"]) == (
        "front-exterior",
        "suggested",
    )
    named = items[shoot.items[1]]
    assert (named["caption"], named["slug"], named["naming_source"]) == (
        "Kitchen",
        "kitchen-island",
        "confirmed",
    )
    # Its picture is still edited: only the name is the operator's.
    assert (await _recipes(env))[shoot.assets[1]]["rotate"] == 1.0


async def test_the_sky_goes_only_where_the_model_would_replace_it_unless_told_everywhere(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.ai import skyseg

    shoot: Shoot = env["shoot"]
    # Segmentation finds plenty of sky in every photograph.
    monkeypatch.setattr(skyseg, "sky_fraction", lambda _image: 0.3)
    await _propose(env, 0, sky=True)
    await _propose(env, 1, sky=False)

    async def again() -> None:
        async with env["factory"]() as db:
            for row in await db.scalars(select(ListingEditSuggestion)):
                row.state = "proposed"
            await db.commit()

    # No sky picked: none is added, whatever the model thought.
    assert (await _apply(env)).status_code == 202
    await _work(env)
    assert all("sky" not in recipe for recipe in (await _recipes(env)).values())

    await again()
    assert (await _apply(env, sky_name="dusk.jpg")).status_code == 202
    await _work(env)
    recipes = await _recipes(env)
    assert recipes[shoot.assets[0]]["sky"]["name"] == "dusk.jpg"
    assert "sky" not in recipes[shoot.assets[1]]

    await again()
    assert (await _apply(env, sky_name="dusk.jpg", sky="all")).status_code == 202
    await _work(env)
    recipes = await _recipes(env)
    assert [recipes[asset]["sky"]["name"] for asset in shoot.assets[:2]] == ["dusk.jpg"] * 2

    # The existing floor still holds: no sky found, none composited.
    monkeypatch.setattr(skyseg, "sky_fraction", lambda _image: 0.01)
    await again()
    assert (await _apply(env, sky_name="dusk.jpg", sky="all")).status_code == 202
    await _work(env)
    assert all("sky" not in recipe for recipe in (await _recipes(env)).values())


async def test_a_photograph_apply_could_not_edit_keeps_its_proposal_waiting(
    env: dict[str, Any],
) -> None:
    shoot: Shoot = env["shoot"]
    # With a look installed every photograph is decoded for its tone.
    _install(env["site"].data, {"exposure": 0.33})
    for index in range(3):
        await _propose(env, index, rotate=1.0, caption=f"Room {index}", slug=f"room-{index}")
    before = {row["id"]: row for row in await _items(env)}
    # The second photograph is gone from the share by the time the worker runs.
    (shoot.root / SHOOT / shoot.names[1]).unlink()
    assert (await _apply(env)).json()["queued"] == 3
    await _work(env)

    assert set(await _recipes(env)) == {shoot.assets[0], shoot.assets[2]}
    rows = await _proposals(env)
    failed = rows[shoot.items[1]]
    # Still waiting, with nothing recorded: the page offers it again.
    assert (failed.state, failed.applied_version, failed.decided_at, failed.decided_by) == (
        "proposed",
        None,
        None,
        None,
    )
    assert [rows[shoot.items[i]].state for i in (0, 2)] == ["applied", "applied"]
    assert [rows[shoot.items[i]].applied_version for i in (0, 2)] == [1, 1]
    # Nor was it renamed for an edit that never happened.
    after = {row["id"]: row for row in await _items(env)}
    assert after[shoot.items[1]] == before[shoot.items[1]]
    listing = await _listing(env)
    assert listing.ai_edit_state == "done"
    assert listing.ai_edit_message.startswith(f"2 edited, 1 skipped — first: {shoot.names[1]}")
    assert (await _read(env))["proposed"] == 1


async def test_a_proposal_replaced_while_applying_is_left_waiting_not_marked(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking again (redo) runs on the local-AI lane and can rewrite a row
    while Apply, on the exports lane, renders the photographs before it."""
    from framefound.local_models.ask import Answer

    shoot: Shoot = env["shoot"]
    for index in range(3):
        await _propose(env, index, rotate=1.0, caption=f"Room {index}", slug=f"room-{index}")
    before = {row["id"]: row for row in await _items(env)}
    url = f"sqlite+aiosqlite:///{env['database'].as_posix()}"
    answer = Answer(
        text="",
        parsed=None,
        model_id=uuid.uuid4(),
        repo_id="google/looker",
        revision="r2",
        runtime="mlx",
        runtime_version="1",
        prompt_version="p",
        seconds=0.0,
        load_seconds=0.0,
        memory_gb=None,
        memory_source=None,
        finish_reason="stop",
        ran_from="this_machine",
        attempts=1,
        demo=False,
    )
    newer = photo_judge.Judged(
        rotate=-3.0, keystone=0.4, sky=False, caption="Rewritten", slug="rewritten", note=""
    )

    async def ask_again() -> None:
        """The Looker's own write (`colour_jobs._propose`), from its own process."""
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine)() as db:
                row = await colour_jobs._propose(
                    db, shoot.listing_id, shoot.items[1], shoot.assets[1], newer, answer, ""
                )
                assert row is not None
                await db.commit()
            # And the third is put aside meanwhile: its edit is made, so its
            # row must still say so.
            async with async_sessionmaker(engine)() as db:
                third = await db.scalar(
                    select(ListingEditSuggestion).where(
                        ListingEditSuggestion.item_id == shoot.items[2]
                    )
                )
                assert third is not None
                third.state = "dismissed"
                await db.commit()
        finally:
            await engine.dispose()

    real = photo_judge.proposed_recipe
    calls: list[int] = []

    def between_read_and_commit(*args: Any, **kwargs: Any) -> dict[str, Any]:
        # Called once per photograph after the worker read every proposal and
        # before it commits that photograph's edit: the second one is replaced
        # (and the third dismissed) in exactly that window.
        calls.append(1)
        if len(calls) == 2:
            done = threading.Thread(target=lambda: asyncio.run(ask_again()))
            done.start()
            done.join(30)
        recipe: dict[str, Any] = real(*args, **kwargs)
        return recipe

    monkeypatch.setattr(photo_judge, "proposed_recipe", between_read_and_commit)
    assert (await _apply(env)).json()["queued"] == 3
    await _work(env)
    assert len(calls) == 3

    # No version from the old proposal, and the new one is still waiting,
    # unmarked, with its own values.
    assert set(await _recipes(env)) == {shoot.assets[0], shoot.assets[2]}
    rows = await _proposals(env)
    replaced = rows[shoot.items[1]]
    assert (replaced.state, replaced.applied_version, replaced.decided_at) == (
        "proposed",
        None,
        None,
    )
    assert (replaced.rotate, replaced.caption, replaced.model) == (
        -3.0,
        "Rewritten",
        "google/looker",
    )
    after = {row["id"]: row for row in await _items(env)}
    assert after[shoot.items[1]] == before[shoot.items[1]]
    assert (rows[shoot.items[2]].state, rows[shoot.items[2]].applied_version) == ("applied", 1)
    listing = await _listing(env)
    assert listing.ai_edit_message == (
        f"2 edited, 1 skipped — first: {shoot.names[1]}: its suggestion changed while applying"
        " — apply it again"
    )

    # Applying again lands the new proposal, as it was proposed.
    assert (await _apply(env)).json()["queued"] == 1
    await _work(env)
    landed = (await _recipes(env))[shoot.assets[1]]
    assert (landed["rotate"], landed["keystone"]) == (-3.0, 0.4)
    assert (await _proposals(env))[shoot.items[1]].state == "applied"


async def _seen(env: dict[str, Any]) -> dict[str, str]:
    """What the page sends with Apply: each waiting row's `made_at`, as read."""
    return {
        row["item_id"]: row["made_at"]
        for row in (await _read(env))["suggestions"]
        if row["state"] == "proposed"
    }


async def _asked_again(env: dict[str, Any], index: int, *, rotate: float) -> None:
    """The Looker rewrites a photograph's row in place, as `colour_jobs._propose`
    does, a few seconds after the page last read it."""
    shoot: Shoot = env["shoot"]
    async with env["factory"]() as db:
        row = await db.scalar(
            select(ListingEditSuggestion).where(ListingEditSuggestion.item_id == shoot.items[index])
        )
        assert row is not None
        made = colour_jobs.utc(row.created_at)
        assert made is not None
        row.rotate = rotate
        row.state = colour_jobs.PROPOSED
        row.created_at = made + timedelta(seconds=7)
        await db.commit()


async def test_a_photograph_suggested_again_after_the_operator_looked_is_left_waiting(
    env: dict[str, Any],
) -> None:
    shoot: Shoot = env["shoot"]
    for index in range(3):
        await _propose(env, index, rotate=1.0, caption=f"Room {index}", slug=f"room-{index}")
    seen = await _seen(env)
    # Between the page's read and the press, the model looks at one again.
    await _asked_again(env, 1, rotate=-3.0)

    applied = await _apply(env, item_ids=list(seen), made_at=seen)
    assert applied.status_code == 202, applied.text
    sentence = (
        "1 photograph was left because the local model made a new suggestion for it after you"
        " looked."
    )
    assert applied.json() == {
        "queued": 2,
        "mode": "local",
        "look": 0,
        "left": 1,
        "note": sentence,
    }
    await _work(env)

    # Nobody saw rotate -3.0 before pressing: it is not applied, and waits.
    assert set(await _recipes(env)) == {shoot.assets[0], shoot.assets[2]}
    rows = await _proposals(env)
    assert (rows[shoot.items[1]].state, rows[shoot.items[1]].rotate) == ("proposed", -3.0)
    assert rows[shoot.items[0]].state == rows[shoot.items[2]].state == "applied"
    listing = await _listing(env)
    assert (listing.ai_edit_state, listing.ai_edit_message) == ("done", f"2 edited. {sentence}")

    # Looking again and pressing again applies it, as seen.
    again = await _apply(env, item_ids=[str(shoot.items[1])], made_at=await _seen(env))
    assert again.json() == {"queued": 1, "mode": "local", "look": 0, "left": 0, "note": ""}
    await _work(env)
    assert (await _recipes(env))[shoot.assets[1]]["rotate"] == -3.0


async def test_a_photograph_suggested_again_before_the_worker_reads_it_is_left_too(
    env: dict[str, Any],
) -> None:
    """The press is accepted while every row is as seen; the model's rewrite
    lands while Apply waits in the queue."""
    shoot: Shoot = env["shoot"]
    for index in range(2):
        await _propose(env, index, rotate=1.0)
    seen = await _seen(env)
    applied = await _apply(env, made_at=seen)
    assert applied.json() == {"queued": 2, "mode": "local", "look": 0, "left": 0, "note": ""}
    # The worker is handed what the operator saw.
    [(_, _, _, options)] = env["queued"]
    assert set(options["made_at"]) == set(seen)

    await _asked_again(env, 0, rotate=2.5)
    await _work(env)
    assert set(await _recipes(env)) == {shoot.assets[1]}
    assert (await _proposals(env))[shoot.items[0]].state == "proposed"
    listing = await _listing(env)
    assert listing.ai_edit_message == (
        "1 edited. 1 photograph was left because the local model made a new suggestion for it"
        " after you looked."
    )


async def test_apply_refuses_in_words_when_every_ticked_photograph_was_suggested_again(
    env: dict[str, Any],
) -> None:
    for index in range(2):
        await _propose(env, index, rotate=1.0)
    seen = await _seen(env)
    for index in range(2):
        await _asked_again(env, index, rotate=-1.0)

    refused = await _apply(env, made_at=seen)
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == (
        "2 photographs were left because the local model made new suggestions for them after"
        " you looked."
    )
    # Nothing was queued and the listing does not read as editing.
    assert env["queued"] == []
    assert (await _listing(env)).ai_edit_state != "queued"
    assert await _recipes(env) == {}


async def test_an_older_page_that_sends_no_made_at_applies_what_is_waiting_as_before(
    env: dict[str, Any],
) -> None:
    shoot: Shoot = env["shoot"]
    await _propose(env, 0, rotate=1.0)
    await _asked_again(env, 0, rotate=-2.0)
    applied = await _apply(env, item_ids=[str(shoot.items[0])])
    # The answer and the queued message keep their old shape exactly.
    assert applied.json() == {"queued": 1, "mode": "local", "look": 0}
    [(_, _, _, options)] = env["queued"]
    assert "made_at" not in options
    await _work(env)
    assert (await _recipes(env))[shoot.assets[0]]["rotate"] == -2.0
    assert (await _listing(env)).ai_edit_message == "1 edited"


async def test_apply_with_no_suggestions_is_refused_in_words(env: dict[str, Any]) -> None:
    refused = await _apply(env)
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == listings_api.NOTHING_PROPOSED
    # Put aside, or already applied: still nothing to apply.
    await _propose(env, 0, state="dismissed")
    await _propose(env, 1, state="applied")
    refused = await _apply(env)
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == listings_api.NOTHING_PROPOSED
    assert env["queued"] == []
    listing = await _listing(env)
    assert (listing.ai_edit_state, listing.ai_edit_mode) == ("idle", "")
    for wrong in ({"sky": "somewhere"}, {"item_ids": ["not-an-id"]}, {"mode": "local-ish"}):
        assert (await _apply(env, **wrong)).status_code == 422


async def test_the_other_modes_queue_exactly_as_before(env: dict[str, Any]) -> None:
    shoot: Shoot = env["shoot"]
    url = f"/api/v1/listings/{shoot.listing_id}/ai-edit"
    listing_id = str(shoot.listing_id)

    async def press(body: dict[str, Any] | None) -> None:
        response = await env["http"].post(url, json=body)
        assert response.status_code == 202, response.text
        async with env["factory"]() as db:
            listing = await db.get(Listing, shoot.listing_id)
            assert listing is not None
            listing.ai_edit_state = "done"  # as though the run finished
            await db.commit()

    await press(None)
    await press({"mode": "edit", "sky_name": "dusk.jpg"})
    stored = await env["http"].put("/api/v1/develop/settings/ai", json={"api_key": "sk-ant-test"})
    assert stored.status_code == 200, stored.text
    await press({"mode": "edit"})
    await press({"mode": "describe"})
    # Three arguments each, as every message already in the queue has: the
    # local mode's choices are not sent where they mean nothing.
    assert env["queued"] == [
        (listing_id, None, "preset"),
        (listing_id, "dusk.jpg", "preset"),
        (listing_id, None, "ai"),
        (listing_id, None, "describe"),
    ]


async def test_the_demo_applies_in_the_server_itself(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    shoot: Shoot = env["shoot"]
    monkeypatch.setenv("FRAMEFOUND_LOCAL_AI_DEMO", "1")
    get_settings.cache_clear()
    await _propose(env, 0, rotate=1.0)
    applied = await _apply(env)
    assert applied.status_code == 202, applied.text
    # The review copy has no queue: nothing waits for a worker.
    assert env["queued"] == []
    runs = list(listings_api._DEMO_RUNS)
    assert len(runs) == 1
    await asyncio.wait_for(asyncio.gather(*runs), 60)
    assert set() == listings_api._DEMO_RUNS
    assert (await _recipes(env))[shoot.assets[0]]["rotate"] == 1.0
    assert (await _listing(env)).ai_edit_state == "done"
