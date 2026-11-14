"""Folder reviews through the API (design L3 sections 6.3-6.9, step S8).

Through `create_app()` and an ASGI transport, on a file-backed SQLite
database. The Mac is test_looking_runner.py's `Site` (MLX-VLM installed, a
model library, the Looker given its role, `fake.FakeModelServer` answering),
handed to the routes by overriding L1's `get_deps`; waking the lane is a
list (`looks.get_wake` overridden), and the lane is `jobs.run_next` awaited
by the test. Nothing starts a process or reaches a network — trying to fails
the test.
"""

import shutil
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# The Mac, its footage and the rule that nothing runs: shared with the runner's tests.
from test_looking_runner import (  # noqa: F401 - `_nothing_runs` is an autouse fixture
    AFTERNOON,
    FOLDER,
    LOOKER,
    SECRET,
    WINDOWS,
    Pipeline,
    Site,
    _asset,
    _clip,
    _nothing_runs,
    _photo,
    _switch_on,
)

from framefound.api.v1 import looks as api
from framefound.api.v1.local_models import QUEUE_DOWN, get_deps
from framefound.auth import service
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    Asset,
    AssetTag,
    AuditLog,
    Library,
    LocalAiJob,
    LocalAiRole,
    LocalModel,
    Look,
    LookRequest,
    Tag,
)
from framefound.local_models import UNAVAILABLE, ask, jobs, schedule
from framefound.local_models.ask import NO_SUCH_MODEL
from framefound.local_models.kinds import MlxVlm
from framefound.looking import reviews, runner, store
from framefound.looking.prompt import PROMPT_VERSION
from framefound.processing.shots import window_moments

URL = "/api/v1/looks"
ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
PASSWORD = "another-strong-password"
NAME = LOOKER.rpartition("/")[2]
# The two shots' middles: where a suggestion on the clip points.
MIDDLES = {window_moments(start, end)[1] for start, end in WINDOWS}


class Api:
    """The app, its database, the Mac behind it, and signed-in clients."""

    def __init__(self, app: Any, factory: async_sessionmaker[AsyncSession], site: Site) -> None:
        self.app = app
        self.factory = factory
        self.site = site
        self.env: dict[str, Any] = {}
        self.http: AsyncClient
        self.woken: list[int] = []
        self.queue_up = True
        # What happens elsewhere while the wake-up is on its way to the broker.
        self.while_waking: Callable[[], Awaitable[None]] | None = None
        self.others: list[AsyncClient] = []

    @property
    def library(self) -> uuid.UUID:
        found: uuid.UUID = self.env["library"]
        return found

    async def account(self, email: str, role: str) -> AsyncClient:
        """A second account, signed in on its own client."""
        async with self.factory() as db:
            await service.create_user(db, email, PASSWORD, role)
            await db.commit()
        client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://t")
        entered = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert entered.status_code == 200, entered.text
        self.others.append(client)
        return client

    async def drain(self, limit: int = 20) -> None:
        """What the lane does when woken: run jobs until none is queued."""
        for _ in range(limit):
            if not await jobs.run_next(self.factory, self.site.deps()):
                return
        raise AssertionError("the lane never ran out of work")

    async def start(self, client: AsyncClient | None = None, **more: Any) -> Any:
        sent: dict[str, Any] = {"library_id": str(self.library), "folder": FOLDER}
        sent.update(more)
        return await (client or self.http).post(f"{URL}/reviews", json=sent)

    async def started(self, **more: Any) -> dict[str, Any]:
        response = await self.start(**more)
        assert response.status_code == 202, response.text
        found: dict[str, Any] = response.json()
        return found

    async def get(self, path: str, client: AsyncClient | None = None, **params: Any) -> Any:
        response = await (client or self.http).get(path, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    async def preview(self, client: AsyncClient | None = None, **params: Any) -> dict[str, Any]:
        found: dict[str, Any] = await self.get(
            f"{URL}/preview",
            client,
            **{"library_id": str(self.library), "folder": FOLDER, **params},
        )
        return found

    async def count(self, table: Any) -> int:
        async with self.factory() as db:
            return int(await db.scalar(select(func.count()).select_from(table)) or 0)

    async def audits(self, event: str) -> list[dict[str, Any]]:
        async with self.factory() as db:
            rows = await db.scalars(select(AuditLog).where(AuditLog.event == event))
            return [dict(row.detail) for row in rows]

    async def rolled_up(self, asset_id: uuid.UUID) -> tuple[int | None, str | None]:
        """The score and caption on the asset's own row: what the roll-up
        chose, and what search, culling and the grid read."""
        async with self.factory() as db:
            asset = await db.get(Asset, asset_id)
            assert asset is not None
            return asset.look_score, asset.look_caption


def said(response: Any) -> str:
    return str(response.json()["error"]["message"])


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Api]:
    """The app on a Mac with a Looker, signed in as its administrator, and a
    library whose folder holds two photographs and a clip of two shots, all
    ready to be looked at; "green" is the catalogue's one tag."""
    import asyncio

    url = f"sqlite+aiosqlite:///{(tmp_path / 'looks-api.db').as_posix()}"
    for folder in ("data", "models", "runtimes"):
        (tmp_path / folder).mkdir()
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("FRAMEFOUND_RUNTIMES_DIR", str(tmp_path / "runtimes"))
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", SECRET)
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "0")
    get_settings.cache_clear()
    settings = get_settings()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    site = Site(tmp_path, settings, asyncio.get_running_loop())
    model_id = await site.looker(factory)

    from framefound.main import create_app

    app = create_app()
    found = Api(app, factory, site)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    def wake() -> Any:
        async def woken() -> bool:
            found.woken.append(1)
            if found.while_waking is not None:
                await found.while_waking()
            return found.queue_up

        return woken

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_deps] = site.deps
    app.dependency_overrides[api.get_wake] = wake
    async with factory() as db:
        lib = Library(name="GELCO", root_path=str(tmp_path / "GELCO"))
        db.add(lib)
        await db.commit()
    found.env = {
        "factory": factory,
        "settings": settings,
        "library": lib.id,
        "model_id": model_id,
        "tmp": tmp_path,
    }
    elsewhere = await _asset(found.env, "Other/IMG_9000.JPG", "image", ())
    async with factory() as db:
        tag = Tag(name="green", slug="green")
        db.add(tag)
        await db.flush()
        db.add(AssetTag(asset_id=elsewhere, tag_id=tag.id, source="confirmed"))
        await db.commit()
    found.env["tag"] = tag.id
    found.env["elsewhere"] = elsewhere
    found.env["clip"] = await _clip(found.env, "DJI_0001.MP4")
    found.env["photos"] = [
        await _photo(found.env, "IMG_0001.JPG"),
        await _photo(found.env, "IMG_0002.JPG"),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        made = await http.post(
            "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
        )
        assert made.status_code == 201, made.text
        found.http = http
        yield found
    for other in found.others:
        await other.aclose()
    await engine.dispose()
    get_settings.cache_clear()


# --- the whole path ---------------------------------------------------------------------


async def test_a_folder_is_reviewed_through_the_routes_and_every_page_reads_the_result(
    env: Api,
) -> None:
    photo, clip = env.env["photos"][0], env.env["clip"]

    # Before: nothing has a look, and a photograph has no shots.
    assert (await env.get(f"/api/v1/assets/{photo}"))["look"] is None
    assert await env.get(f"/api/v1/assets/{photo}/shots") == []
    shots = await env.get(f"/api/v1/assets/{clip}/shots")
    assert [(s["idx"], s["start_ms"], s["end_ms"], s["look"]) for s in shots] == [
        (0, 0, 4000, None),
        (1, 4000, 9000, None),
    ]
    assert [s["still_ms"] for s in shots] == [window_moments(*w)[1] for w in WINDOWS]
    assert (shots[0]["move"], shots[0]["light"], shots[0]["subjects"]) == ("orbit", "golden", [])
    missing = await env.http.get(f"/api/v1/assets/{uuid.uuid4()}/shots")
    assert missing.status_code == 404

    status = await env.get(f"{URL}/status")
    assert (status["available"], status["mode"], status["reason"], status["can_manage"]) == (
        True,
        "ready",
        None,
        True,
    )
    assert status["looker"] == {
        "model_id": str(env.env["model_id"]),
        "name": NAME,
        "problem": None,
    }
    assert [
        (m["id"], m["name"], m["repo_id"], m["fits"], m["is_looker"]) for m in status["models"]
    ] == [(str(env.env["model_id"]), NAME, LOOKER, "day", True)]
    assert status["models"][0]["why_not_now"] is None
    assert status["now"] == {
        "overnight": False,
        "night_start": "21:30",
        "night_end": "05:30",
        "paused": False,
    }
    assert status["settings"] == {"new_tags": True, "stills_per_shot": 3}
    assert status["pending_suggestions"] == 0

    preview = await env.preview()
    assert (preview["available"], preview["library_name"], preview["folder"]) == (
        True,
        "GELCO",
        FOLDER,
    )
    assert preview["todo"] == {"photos": 2, "clips": 1, "shots": 2, "stills_only": 0}
    assert preview["already"] == {"items": 0, "by_another_model": 0}
    assert preview["model"]["name"] == NAME and preview["model"]["is_looker"] is True
    assert preview["active"] is None

    started = await env.started()
    assert env.woken == [1]
    assert (started["state"], started["message"], started["can_cancel"]) == (
        "waiting",
        reviews.WAITING_TURN,
        True,
    )
    assert (started["library_name"], started["folder"], started["when"]) == ("GELCO", FOLDER, "now")
    assert (started["model_name"], started["requested_by"], started["demo"]) == (
        NAME,
        ADMIN["email"],
        False,
    )
    assert (started["done"], started["total"]) == (0, 4)
    assert started["preparing"] == {"state": "", "total": 0, "ready": 0, "gave_up": 0}
    assert await env.audits("looks.review_started") == [
        {
            "library": "GELCO",
            "folder": FOLDER,
            "when": "now",
            "model": NAME,
            "redo": False,
            "photos": 2,
            "shots": 2,
            "need_copy": 0,
        }
    ]
    # The dialog opened again shows the review running, not the form.
    assert (await env.preview())["active"]["id"] == started["id"]

    await env.drain()

    listing = await env.get(f"{URL}/reviews")
    assert listing["available"] is True
    (done,) = listing["reviews"]
    assert (done["id"], done["state"], done["done"], done["total"], done["failed"]) == (
        started["id"],
        "done",
        4,
        4,
        0,
    )
    assert done["message"] == "Finished: 4 of 4 were looked at."
    assert (done["can_cancel"], done["seconds_left"]) == (False, None)
    assert done["finished_at"].endswith("Z")

    # The photograph's look, and who said it.
    look = (await env.get(f"/api/v1/assets/{photo}"))["look"]
    assert (look["score"], look["caption"], look["whole"], look["complete"]) == (
        82,
        "A green with a flag, seen from above.",
        True,
        True,
    )
    assert (look["start_ms"], look["end_ms"]) == (None, None)
    # L1's stand-in server marks its every answer as a demonstration's; the
    # flag is the look's own, passed on as it was stored.
    async with env.factory() as db:
        stored = await db.scalar(select(Look).where(Look.asset_id == photo, Look.current))
        assert stored is not None
    assert look["demo"] is stored.demo
    assert (look["model"], look["runtime"], look["prompt_version"]) == (
        LOOKER,
        "mlx-vlm",
        PROMPT_VERSION,
    )
    assert look["revision_short"] == look["model_revision"][:7] and len(look["revision_short"]) == 7
    # The page names the look the asset's own row was rolled up from.
    assert (look["score"], look["caption"]) == await env.rolled_up(photo)

    # The clip's is its best shot's (two equal scores: the earlier shot).
    clip_look = (await env.get(f"/api/v1/assets/{clip}"))["look"]
    assert (clip_look["whole"], clip_look["start_ms"], clip_look["end_ms"]) == (False, 0, 4000)
    assert clip_look["complete"] is True
    assert (clip_look["score"], clip_look["caption"]) == await env.rolled_up(clip)
    shots = await env.get(f"/api/v1/assets/{clip}/shots")
    assert [(s["look"]["score"], s["look"]["whole"]) for s in shots] == [(82, False), (82, False)]
    assert [(s["look"]["start_ms"], s["look"]["end_ms"]) for s in shots] == list(WINDOWS)

    # Its tags wait on the Tags page as the local model's, the clip's at its moment.
    pending = await env.get(f"/api/v1/tags/{env.env['tag']}/pending")
    by_asset = {uuid.UUID(p["asset_id"]): p for p in pending}
    assert set(by_asset) == {clip, *env.env["photos"]}
    assert {(p["origin"], p["model"], p["demo"]) for p in pending} == {("local", NAME, stored.demo)}
    assert by_asset[clip]["at_ms"] in MIDDLES
    assert by_asset[photo]["at_ms"] is None
    assert (await env.get(f"{URL}/status"))["pending_suggestions"] == len(pending)

    # Asked again: nothing new, and the estimate now comes from what was timed.
    again = await env.start()
    assert (again.status_code, said(again)) == (409, reviews.NOTHING_NEW)
    after = await env.preview()
    assert after["todo"] == {"photos": 0, "clips": 0, "shots": 0, "stills_only": 0}
    assert after["already"]["items"] == 4
    assert after["estimate"]["basis"] == "measured"


async def test_the_preview_reaches_the_page_with_what_was_looked_at_by_kind(env: Api) -> None:
    # The response model once dropped these, so the dialog said a bare
    # "4" for two photographs and one clip of two shots.
    none = {"photos": 0, "clips": 0, "shots": 0}
    before = await env.preview()
    assert (before["already_kinds"], before["by_another_kinds"]) == (none, none)

    await env.started()
    await env.drain()

    after = await env.preview()
    assert after["already"] == {"items": 4, "by_another_model": 0}
    assert after["already_kinds"] == {"photos": 2, "clips": 1, "shots": 2}
    assert after["by_another_kinds"] == none

    # A folder the dialog cannot review still sends the breakdown, all zeros.
    nowhere = await env.preview(folder="No/Such/Folder")
    assert (nowhere["already_kinds"], nowhere["by_another_kinds"]) == (none, none)


# --- who may do what --------------------------------------------------------------------


async def test_anyone_can_read_the_counts_and_only_an_administrator_can_start(env: Api) -> None:
    member = await env.account("crew@example.com", "user")

    status = await env.get(f"{URL}/status", member)
    assert (status["available"], status["can_manage"], status["looker"]["name"]) == (
        True,
        False,
        NAME,
    )
    preview = await env.preview(member)
    assert preview["todo"]["photos"] == 2
    assert (await env.get(f"{URL}/reviews", member))["reviews"] == []

    refused = await env.start(member)
    assert refused.status_code == 403
    assert (await member.put(f"{URL}/settings", json={"stills_per_shot": 1})).status_code == 403
    # Reading never started anything.
    assert (await env.count(LookRequest), await env.count(LocalAiJob)) == (0, 0)
    assert env.woken == []

    started = await env.started()
    cancel = await member.post(f"{URL}/reviews/{started['id']}/cancel")
    assert cancel.status_code == 403
    # Active, but not this reader's to cancel: the page offers no button.
    (theirs,) = (await env.get(f"{URL}/reviews", member))["reviews"]
    assert (theirs["state"], theirs["can_cancel"]) == ("waiting", False)
    assert (await env.preview(member))["active"]["can_cancel"] is False
    (ours,) = (await env.get(f"{URL}/reviews"))["reviews"]
    assert ours["can_cancel"] is True

    # No server path in anything a reader is sent.
    tmp = env.env["tmp"]
    for path in (
        f"{URL}/status",
        f"{URL}/reviews",
        f"/api/v1/assets/{env.env['clip']}",
        f"/api/v1/assets/{env.env['clip']}/shots",
    ):
        text = (await member.get(path)).text
        assert str(tmp) not in text and tmp.as_posix() not in text, path
    text = (
        await member.get(
            f"{URL}/preview", params={"library_id": str(env.library), "folder": FOLDER}
        )
    ).text
    assert str(tmp) not in text and tmp.as_posix() not in text


async def test_every_write_is_refused_plainly_where_models_cannot_run(
    env: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR")
    get_settings.cache_clear()
    env.app.dependency_overrides[get_deps] = lambda: replace(
        env.site.deps(), settings=get_settings()
    )

    status = await env.get(f"{URL}/status")
    assert (status["available"], status["mode"], status["reason"]) == (
        False,
        "unavailable",
        UNAVAILABLE,
    )
    assert (status["can_manage"], status["looker"], status["models"]) == (False, None, [])
    preview = await env.preview()
    assert (preview["available"], preview["reason"]) == (False, UNAVAILABLE)
    assert preview["todo"] == {"photos": 0, "clips": 0, "shots": 0, "stills_only": 0}
    assert await env.get(f"{URL}/reviews") == {"available": False, "reviews": [], "ingest": []}

    for response in (
        await env.start(),
        await env.http.post(f"{URL}/reviews/{uuid.uuid4()}/cancel"),
        await env.http.put(f"{URL}/settings", json={"new_tags": False}),
    ):
        assert (response.status_code, said(response)) == (409, UNAVAILABLE)
    assert (await env.count(LookRequest), await env.count(LocalAiJob)) == (0, 0)
    assert env.woken == []
    # The asset page reads as before.
    assert (await env.get(f"/api/v1/assets/{env.env['clip']}"))["look"] is None


async def test_a_queue_that_is_down_leaves_no_review_behind(env: Api) -> None:
    env.queue_up = False
    response = await env.start()
    assert (response.status_code, said(response)) == (503, QUEUE_DOWN)
    assert env.woken == [1]
    assert (await env.count(LookRequest), await env.count(LocalAiJob)) == (0, 0)
    assert await env.audits("looks.review_started") == []
    # Nothing stands in the way of trying again once it is back.
    env.queue_up = True
    assert (await env.started())["state"] == "waiting"


async def test_a_model_whose_program_is_not_installed_is_refused_before_a_review_starts(
    env: Api,
) -> None:
    """Started, such a review failed at once ("… is not installed"), from the
    dialog and the library box alike. Now the dialog says so before Start is
    pressed, and the start is refused with the same sentence, as Compare
    refuses such a model."""
    settings = env.env["settings"]
    shutil.rmtree(settings.runtimes_dir / "mlx-vlm")
    sentence = ask.program_problem("mlx-vlm", settings, {"mlx-vlm": MlxVlm()})
    assert sentence is not None and "Install it under Software that runs models" in sentence

    (choice,) = (await env.get(f"{URL}/status"))["models"]
    assert (choice["program_missing"], choice["problem"]) == (True, sentence)
    model = (await env.preview())["model"]
    assert (model["problem"], model["program_missing"]) == (sentence, True)
    assert (model["why_not_now"], model["tonight_problem"]) == (sentence, sentence)

    for when in ("now", "tonight"):
        refused = await env.start(when=when)
        assert (refused.status_code, said(refused)) == (409, sentence)
    named = await env.start(model_id=str(env.env["model_id"]))
    assert (named.status_code, said(named)) == (409, sentence)
    assert (await env.count(LookRequest), await env.count(LocalAiJob)) == (0, 0)
    assert env.woken == []
    assert await env.audits("looks.review_started") == []


async def test_a_model_whose_program_is_installed_is_offered_as_before(env: Api) -> None:
    (choice,) = (await env.get(f"{URL}/status"))["models"]
    assert (choice["program_missing"], choice["problem"]) == (False, None)
    model = (await env.preview())["model"]
    assert (model["problem"], model["program_missing"], model["why_not_now"]) == (
        None,
        False,
        None,
    )


async def test_a_review_the_lane_took_while_the_wake_up_failed_is_not_taken_back(env: Api) -> None:
    """The review is committed before the lane is woken, so the once-a-minute
    tick can claim one of its jobs while the wake-up is failing (and a
    wake-up can fail after its message was sent). That job's row was deleted
    under it and the administrator told nothing had started, while playable
    copies were being made."""
    # A clip with no playable copy: the review has a make-ready job too,
    # first in line.
    await _asset(env.env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))
    beat = env.site.clock().astimezone(UTC)

    async def claimed() -> None:
        async with env.factory() as db:
            await db.execute(
                update(LocalAiJob)
                .where(LocalAiJob.kind == "look_prepare", LocalAiJob.state == "queued")
                .values(state="running", started_at=beat, beat_at=beat)
            )
            await db.commit()

    env.while_waking = claimed
    env.queue_up = False
    response = await env.start()
    assert response.status_code == 202, response.text
    started = response.json()
    assert (started["state"], started["can_cancel"]) == ("waiting", True)
    assert started["preparing"]["state"] == "running"

    # Both jobs are still there: the one that was claimed, and the one that was not.
    async with env.factory() as db:
        states = dict((await db.execute(select(LocalAiJob.kind, LocalAiJob.state))).all())
    assert states == {"look_prepare": "running", "look_folder": "queued"}
    assert await env.count(LookRequest) == 1
    assert [a["folder"] for a in await env.audits("looks.review_started")] == [FOLDER]
    (listed,) = (await env.get(f"{URL}/reviews"))["reviews"]
    assert listed["id"] == started["id"]


async def test_a_tonight_review_whose_copies_were_already_asked_for_is_not_taken_back(
    env: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane's tick ran the make-ready job to the end of its first run
    while the wake-up was failing: the playable copies are asked for, and
    the job rests in `waiting`, as the look job does until tonight. Two
    waiting jobs read as a review nobody had touched: it was deleted, and
    the administrator told "Nothing was started" while the copies were made."""
    pipeline = Pipeline(monkeypatch)
    clip = await _asset(env.env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))

    async def the_tick_runs() -> None:
        # One job at most: the make-ready job, the only one that is queued.
        await jobs.run_next(env.factory, env.site.deps())

    env.while_waking = the_tick_runs
    env.queue_up = False
    response = await env.start(when="tonight")
    assert response.status_code == 202, response.text
    started = response.json()
    assert (started["state"], started["can_cancel"]) == ("tonight", True)

    assert pipeline.copies == [(str(clip), True)]
    async with env.factory() as db:
        states = dict((await db.execute(select(LocalAiJob.kind, LocalAiJob.state))).all())
    assert states == {"look_prepare": "waiting", "look_folder": "waiting"}
    assert await env.count(LookRequest) == 1
    # It started, so the record says who started it.
    assert [a["folder"] for a in await env.audits("looks.review_started")] == [FOLDER]


# --- cancelling -------------------------------------------------------------------------


async def test_cancel_stops_a_review_and_says_what_was_kept(env: Api) -> None:
    started = await env.started()
    response = await env.http.post(f"{URL}/reviews/{started['id']}/cancel")
    assert response.status_code == 200, response.text
    cancelled = response.json()
    assert (cancelled["state"], cancelled["can_cancel"]) == ("cancelled", False)
    assert cancelled["message"] == "Cancelled. 0 of 4 were looked at; their results are kept."
    async with env.factory() as db:
        request = await db.get(LookRequest, uuid.UUID(started["id"]))
        assert request is not None and request.state == "cancelled"
        job = await db.get(LocalAiJob, request.look_job_id)
        assert job is not None and job.state == "cancelled"
    assert await env.audits("looks.review_cancelled") == [
        {"library": "GELCO", "folder": FOLDER, "done": 0, "total": 4}
    ]

    again = await env.http.post(f"{URL}/reviews/{started['id']}/cancel")
    assert (again.status_code, said(again)) == (409, reviews.FINISHED_ALREADY)
    unknown = await env.http.post(f"{URL}/reviews/{uuid.uuid4()}/cancel")
    assert (unknown.status_code, said(unknown)) == (404, reviews.NO_SUCH_REVIEW)
    # The lane finds nothing to do.
    await env.drain()
    assert await env.count(Look) == 0


async def test_a_job_cancelled_elsewhere_does_not_leave_the_review_active(env: Api) -> None:
    started = await env.started()
    async with env.factory() as db:
        request = await db.get(LookRequest, uuid.UUID(started["id"]))
        assert request is not None
        job_id = request.look_job_id
    # The AI models page's Cancel, on the job itself.
    response = await env.http.post(f"/api/v1/local-models/jobs/{job_id}/cancel")
    assert response.status_code == 200, response.text

    (review,) = (await env.get(f"{URL}/reviews"))["reviews"]
    assert (review["state"], review["can_cancel"]) == ("cancelled", False)
    assert review["message"].startswith("Cancelled. 0 of 4")
    async with env.factory() as db:
        request = await db.get(LookRequest, uuid.UUID(started["id"]))
        assert request is not None and request.state == "cancelled"
    # The folder can be reviewed again.
    assert (await env.started())["id"] != started["id"]


# --- refusals ---------------------------------------------------------------------------


async def test_starting_is_refused_in_plain_words(env: Api) -> None:
    for more, status, sentence in (
        ({"folder": "../elsewhere"}, 400, None),
        ({"library_id": str(uuid.uuid4())}, 404, reviews.NO_SUCH_LIBRARY),
        ({"model_id": str(uuid.uuid4())}, 404, NO_SUCH_MODEL),
        ({"when": "later"}, 400, reviews.BAD_WHEN),
        ({"folder": "Nowhere"}, 409, reviews.NOTHING_HERE),
    ):
        response = await env.start(**more)
        assert response.status_code == status, (more, response.text)
        if sentence is not None:
            assert said(response) == sentence
    assert "cannot use . or .." in said(await env.start(folder="../elsewhere"))
    # The dialog is refused the same way.
    gone = await env.http.get(
        f"{URL}/preview", params={"library_id": str(uuid.uuid4()), "folder": FOLDER}
    )
    assert (gone.status_code, said(gone)) == (404, reviews.NO_SUCH_LIBRARY)
    no_model = await env.http.get(
        f"{URL}/preview",
        params={"library_id": str(env.library), "folder": FOLDER, "model_id": str(uuid.uuid4())},
    )
    assert (no_model.status_code, said(no_model)) == (404, NO_SUCH_MODEL)
    bad = await env.http.get(
        f"{URL}/preview", params={"library_id": str(env.library), "folder": "a//b"}
    )
    assert bad.status_code == 400

    # A model that only fits overnight cannot start in the afternoon …
    async with env.factory() as db:
        await db.execute(
            update(LocalModel)
            .where(LocalModel.id == env.env["model_id"])
            .values(memory_gb=12.0, measured_memory_gb=None)
        )
        await db.commit()
    now = await env.start()
    assert now.status_code == 409
    assert said(now) == (
        f"This model only runs overnight (needs about {schedule.gb_text(12.0)} GB; "
        f"the daytime budget is {schedule.gb_text(8.0)} GB). Choose Tonight."
    )
    (choice,) = (await env.get(f"{URL}/status"))["models"]
    assert (choice["fits"], choice["why_not_now"]) == (
        "night",
        said(now).removesuffix(" Choose Tonight."),
    )
    # … and waits for the window when asked for Tonight.
    tonight = await env.started(when="tonight")
    assert (tonight["state"], tonight["message"]) == ("tonight", "Starts tonight at 21:30.")
    assert tonight["not_before"] is not None and tonight["not_before"].endswith("Z")
    running = await env.start(when="tonight")
    assert (running.status_code, said(running)) == (409, reviews.ALREADY_RUNNING)

    # With no Looker, nothing can start, and the status says why.
    async with env.factory() as db:
        await db.execute(delete(LocalAiRole))
        await db.commit()
    nobody = await env.start(folder="Other")
    assert (nobody.status_code, said(nobody)) == (409, reviews.NO_LOOKER)
    assert (await env.get(f"{URL}/status"))["looker"] == {
        "model_id": None,
        "name": None,
        "problem": reviews.NO_LOOKER,
    }
    assert (await env.preview(folder="Other"))["model"] is None


# --- the estimate -----------------------------------------------------------------------


async def test_the_estimate_comes_from_what_was_measured_else_the_one_test_else_nothing(
    env: Api,
) -> None:
    nothing = (await env.preview())["estimate"]
    assert (nothing["basis"], nothing["seconds"], nothing["sample"]) == ("none", None, 0)

    model_id = env.env["model_id"]
    async with env.factory() as db:
        db.add(
            LocalAiJob(
                kind="test",
                subject="the Looker's Test",
                model_id=model_id,
                state="done",
                seconds=9.0,
                result={"kind": "picture"},
                finished_at=datetime.now(UTC),
            )
        )
        await db.commit()
    tested = (await env.preview())["estimate"]
    assert (tested["basis"], tested["sample"], tested["per_photo_s"]) == ("test", 1, 9.0)
    # Two photographs at 9 s and two shots at 1.5 times that.
    assert tested["seconds"] == 2 * 9 + 2 * 13.5
    assert tested["words"].startswith("Under a minute, from the one test")

    async with env.factory() as db:
        model = await db.get(LocalModel, model_id)
        assert model is not None
        for start, end, seconds in ((None, None, 4.0), (0, 4000, 6.0)):
            db.add(
                Look(
                    asset_id=env.env["elsewhere"],
                    start_ms=start,
                    end_ms=end,
                    key=store.key_for(model),
                    model=model.repo_id,
                    variant=model.variant,
                    model_revision=model.revision,
                    prompt_version=PROMPT_VERSION,
                    score=70,
                    seconds=seconds,
                )
            )
        await db.commit()
    measured = (await env.preview())["estimate"]
    assert (measured["basis"], measured["sample"]) == ("measured", 2)
    assert (measured["per_photo_s"], measured["per_shot_s"], measured["seconds"]) == (4.0, 6.0, 20)


# --- the settings -----------------------------------------------------------------------


async def test_the_two_settings_are_saved_and_refused_plainly(env: Api) -> None:
    saved = await env.http.put(f"{URL}/settings", json={"stills_per_shot": 1})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"new_tags": True, "stills_per_shot": 1}
    kept = await env.http.put(f"{URL}/settings", json={"new_tags": False})
    assert kept.json() == {"new_tags": False, "stills_per_shot": 1}
    assert (await env.get(f"{URL}/status"))["settings"] == {
        "new_tags": False,
        "stills_per_shot": 1,
    }
    bad = await env.http.put(f"{URL}/settings", json={"stills_per_shot": 2})
    assert (bad.status_code, said(bad)) == (400, api.BAD_STILLS)
    # Nothing changed: nothing to audit.
    assert (await env.http.put(f"{URL}/settings", json={})).json()["stills_per_shot"] == 1
    assert await env.audits("looks.settings_updated") == [
        {"changed": ["stills_per_shot"]},
        {"changed": ["new_tags"]},
    ]


# --- what the pages are told about a look -----------------------------------------------


async def test_a_look_says_whether_it_was_made_up_and_a_look_of_a_moved_shot_is_not_shown(
    env: Api,
) -> None:
    """Rows as a real install and as the demonstration write them, side by
    side. (L1's stand-in server marks every answer as made up, so the looks
    a review writes in these tests cannot show the difference.)"""
    photo, real_photo, clip = env.env["photos"][0], env.env["photos"][1], env.env["clip"]
    learned = await _photo(env.env, "IMG_0003.JPG")
    second = WINDOWS[1]
    async with env.factory() as db:
        made_up = Look(
            asset_id=photo,
            key="demo",
            model="google/demo-vision",
            model_revision="a" * 40,
            runtime="demo",
            prompt_version=PROMPT_VERSION,
            score=61,
            caption="A made-up caption.",
            reason="Made up.",
            demo=True,
        )
        real = Look(
            asset_id=real_photo,
            key="real",
            model=LOOKER,
            model_revision="c" * 40,
            runtime="mlx-vlm",
            prompt_version=PROMPT_VERSION,
            score=70,
            caption="A barn at the end of a lane.",
        )
        # The clip's two shots: the second is the better one.
        shot_looks = [
            Look(
                asset_id=clip,
                start_ms=start,
                end_ms=end,
                key="real",
                model=LOOKER,
                model_revision="c" * 40,
                runtime="mlx-vlm",
                prompt_version=PROMPT_VERSION,
                score=score,
                caption=caption,
            )
            for (start, end), score, caption in zip(
                WINDOWS, (40, 75), ("The drone lifts off.", "The green from above."), strict=True
            )
        ]
        # A look of the clip at times no shot of it has any more.
        moved = Look(
            asset_id=clip,
            start_ms=100,
            end_ms=200,
            key="old",
            model=LOOKER,
            model_revision="b" * 40,
            score=99,
        )
        db.add_all([made_up, real, *shot_looks, moved])
        await db.flush()
        for asset_id in (photo, real_photo, clip):
            await store.roll_up(db, asset_id)
        for asset_id, origin, confidence, look, at_ms in (
            (photo, "local", 0.8, made_up, None),
            (real_photo, "local", 0.75, real, None),
            (clip, "local", 0.72, shot_looks[1], sum(second) // 2),
            (learned, "clip", 0.7, None, None),
        ):
            db.add(
                AssetTag(
                    asset_id=asset_id,
                    tag_id=env.env["tag"],
                    source="suggested",
                    origin=origin,
                    confidence=confidence,
                    look_id=None if look is None else look.id,
                    at_ms=at_ms,
                )
            )
        await db.commit()

    look = (await env.get(f"/api/v1/assets/{photo}"))["look"]
    assert (look["demo"], look["runtime"], look["model"], look["score"]) == (
        True,
        "demo",
        "google/demo-vision",
        61,
    )
    # A real model's look is not passed off as the demonstration's …
    real_look = (await env.get(f"/api/v1/assets/{real_photo}"))["look"]
    assert (real_look["demo"], real_look["model"], real_look["score"]) == (False, LOOKER, 70)
    # … on the clip either, whose look is its best shot's: not the first
    # shot's, and not the one of times it no longer has, whatever its score.
    clip_look = (await env.get(f"/api/v1/assets/{clip}"))["look"]
    assert (clip_look["start_ms"], clip_look["end_ms"]) == second
    assert (clip_look["score"], clip_look["demo"]) == (75, False)
    shots = await env.get(f"/api/v1/assets/{clip}/shots")
    assert [(s["look"]["score"], s["look"]["demo"]) for s in shots] == [(40, False), (75, False)]
    # Every page's look is the one the asset's own row was rolled up from.
    for asset_id, shown in ((photo, look), (real_photo, real_look), (clip, clip_look)):
        assert (shown["score"], shown["caption"]) == await env.rolled_up(asset_id)

    pending = await env.get(f"/api/v1/tags/{env.env['tag']}/pending")
    assert [(p["origin"], p["model"], p["demo"], p["at_ms"]) for p in pending] == [
        ("local", "demo-vision", True, None),
        ("local", NAME, False, None),
        ("local", NAME, False, sum(second) // 2),
        ("clip", None, False, None),
    ]


async def test_the_page_and_the_asset_row_are_chosen_by_one_rule(
    env: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which of a clip's looks speaks for it used to be written twice: once
    for the score on the asset's row (search, culling, the grid) and once for
    the look the asset page shows. Change the rule in its one place and both
    follow, so the page can never name a look the row was not made from."""
    clip = env.env["clip"]

    async def looked_at() -> None:
        async with env.factory() as db:
            await store.roll_up(db, clip)
            await db.commit()

    async with env.factory() as db:
        for (start, end), score, caption in zip(
            WINDOWS, (40, 75), ("The drone lifts off.", "The green from above."), strict=True
        ):
            db.add(
                Look(
                    asset_id=clip,
                    start_ms=start,
                    end_ms=end,
                    key="real",
                    model=LOOKER,
                    model_revision="c" * 40,
                    runtime="mlx-vlm",
                    prompt_version=PROMPT_VERSION,
                    score=score,
                    caption=caption,
                )
            )
        await db.commit()
    await looked_at()
    shown = (await env.get(f"/api/v1/assets/{clip}"))["look"]
    assert (shown["score"], shown["caption"]) == (75, "The green from above.")
    assert (shown["score"], shown["caption"]) == await env.rolled_up(clip)

    # Suppose the rule were "the weakest shot speaks for the clip".
    monkeypatch.setattr(
        store, "_best", lambda looks: min(looks, key=lambda look: look.score or 0, default=None)
    )
    await looked_at()
    shown = (await env.get(f"/api/v1/assets/{clip}"))["look"]
    assert (shown["score"], shown["caption"]) == (40, "The drone lifts off.")
    assert (shown["score"], shown["caption"]) == await env.rolled_up(clip)


async def test_a_library_left_alone_after_its_looker_failed_says_so_on_the_progress_page(
    env: Api,
) -> None:
    """The library's line read "idle" with four files waiting, for the six
    hours in which nothing was going to look at them."""
    await _switch_on(env.env)
    idle = (await env.get(f"{URL}/reviews"))["ingest"]
    assert [(one["state"], one["waiting"], one["retry_at"]) for one in idle] == [("idle", 4, None)]

    # New footage arrives, and the Looker will not load.
    env.site.server.refuse("load_failed")
    async with env.factory() as db:
        assert await runner.ensure_ingest(db, env.site.clock()) == 1
        await db.commit()
    failed_at = env.site.clock().astimezone(UTC)
    await env.drain()

    (line,) = (await env.get(f"{URL}/reviews"))["ingest"]
    assert line == {
        "library_id": str(env.library),
        "library_name": "GELCO",
        "waiting": 4,
        "state": "failed",
        "message": (
            "Looking at new footage stopped because of a problem with the model, and is "
            "tried again in about 6 hours."
        ),
        "done": 0,
        "total": 0,
        "retry_at": (failed_at + reviews.INGEST_HOLD)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    # Anyone signed in reads it; nothing in it is a path.
    member = await env.account("crew@example.com", "user")
    assert (await env.get(f"{URL}/reviews", member))["ingest"] == [line]

    # Five hours on it says how long is left; after six it is tried again.
    env.site.clock.advance(5 * 3600)
    (line,) = (await env.get(f"{URL}/reviews"))["ingest"]
    assert line["message"].endswith("and is tried again in about an hour.")
    env.site.clock.advance(3600)
    (line,) = (await env.get(f"{URL}/reviews"))["ingest"]
    assert (line["state"], line["message"], line["retry_at"]) == ("idle", "", None)
