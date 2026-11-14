"""The chat panel's tool for the Looker (framefound/looking/chat_tool.py; design L3 S9).

`start_folder_review` is held against the page: the Mac, its library and
the app are test_looking_api.py's (`Api`), so the same folder can be asked
for through `POST /looks/reviews` and through the tool, and what each
leaves and says compared. `chat_tool.start` is called with a session and a
user; the panel's side goes through C1's own dispatcher (`chat.tools.run`)
with the caller bound as the chat engine binds it. Waking the lane is a
list, and the lane is `jobs.run_next` awaited by the test.

No conversation is started and Claude is never called; nothing starts a
process, reaches a model or reaches a network — trying to fails the test.
"""

import ast
import importlib.util
import json
import re
import shutil
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
import test_looking_api
from sqlalchemy import delete, select, update

# The app on a Mac with a Looker, and the rule that nothing runs: shared
# with the API's tests.
from test_looking_api import ADMIN, URL, Api, said
from test_looking_api import NAME as LOOKER_NAME
from test_looking_runner import (  # noqa: F401 - `_nothing_runs` is an autouse fixture
    FOLDER,
    SECRET,
    _asset,
    _nothing_runs,
)

from framefound import looking
from framefound.api.v1 import local_models as local_models_api
from framefound.api.v1 import looks as api
from framefound.api.v1.local_models import get_deps
from framefound.chat import tools
from framefound.config import Settings, get_settings
from framefound.db.models import (
    AuditLog,
    Library,
    LocalAiJob,
    LocalAiRole,
    LocalModel,
    Look,
    LookRequest,
    User,
)
from framefound.local_models import UNAVAILABLE, Availability, ask, schedule
from framefound.local_models import settings_store as local_ai_settings
from framefound.local_models.kinds import MlxVlm
from framefound.looking import chat_tool, estimate, reviews, store
from framefound.looking.prompt import PROMPT_VERSION
from framefound.mcp import auth
from framefound.nle import draft_chat
from framefound.processing import local_ai_tasks

# The app, its database and the Mac behind it, as the API's tests build them.
env = test_looking_api.env

NO_ESTIMATE = estimate.NO_ESTIMATE
PETE_DYE = "To look at: 2 photographs and 1 clip (2 shots)."


def _wake(env: Api) -> chat_tool.Wake:
    """The lane's wake-up as the API's tests fake it: counted, and down
    when the test says the queue is."""

    async def wake() -> bool:
        env.woken.append(1)
        if env.while_waking is not None:
            await env.while_waking()
        return env.queue_up

    return wake


async def _user(env: Api, email: str = ADMIN["email"]) -> User:
    async with env.factory() as db:
        found = await db.scalar(select(User).where(User.email == email))
        assert found is not None
        return found


async def _tool(env: Api, email: str = ADMIN["email"], **args: Any) -> str:
    """The tool as `email`, asked for Pete Dye in GELCO unless told otherwise."""
    asked = {"library": "GELCO", "folder": FOLDER, **args}
    async with env.factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        return await chat_tool.start(db, user, asked, deps=env.site.deps(), wake=_wake(env))


async def _refused(env: Api, email: str = ADMIN["email"], **args: Any) -> str:
    with pytest.raises(tools.ToolProblem) as raised:
        await _tool(env, email, **args)
    return str(raised.value)


async def _left(env: Api) -> dict[str, Any]:
    """Everything starting a review leaves in the database, without what
    differs from one review to the next (ids, times)."""
    async with env.factory() as db:
        requests = list(await db.scalars(select(LookRequest)))
        jobs_ = list(await db.scalars(select(LocalAiJob)))
        audits = list(
            await db.scalars(select(AuditLog).where(AuditLog.event == reviews.AUDIT_STARTED))
        )
    by_id = {job.id: job.kind for job in jobs_}
    return {
        "requests": [
            {
                "library": request.library_id,
                "folder": request.folder,
                "when": request.run_when,
                "redo": request.redo,
                "prepare": request.prepare,
                "model_id": request.model_id,
                "model_name": request.model_name,
                "state": request.state,
                "done": request.done,
                "total": request.total,
                "prep_state": request.prep_state,
                "requested_by": request.requested_by,
                "look_job": by_id.get(request.look_job_id),
                "prepare_job": by_id.get(request.prepare_job_id),
            }
            for request in requests
        ],
        "jobs": sorted(
            (
                {
                    "kind": job.kind,
                    "subject": job.subject,
                    "state": job.state,
                    "priority": job.priority,
                    "role": job.role,
                    "model_id": job.model_id,
                    "requested_by": job.requested_by,
                    "waits": job.not_before is not None,
                    # The review it belongs to.
                    "ours": job.payload == {"request_id": str(requests[0].id)},
                }
                for job in jobs_
            ),
            key=lambda job: str(job["kind"]),
        ),
        "audits": [
            {"event": row.event, "actor": row.actor_user_id, "detail": dict(row.detail)}
            for row in audits
        ],
    }


async def _addresses(env: Api) -> list[str | None]:
    """Where each review that started was asked from, as its audit row keeps it."""
    async with env.factory() as db:
        found = await db.scalars(select(AuditLog.ip).where(AuditLog.event == reviews.AUDIT_STARTED))
        return list(found)


async def _nothing_left(env: Api) -> bool:
    return (
        await env.count(LookRequest),
        await env.count(LocalAiJob),
        await env.audits(reviews.AUDIT_STARTED),
    ) == (0, 0, [])


async def _forget(env: Api) -> None:
    """The database as it was before any review was asked for."""
    async with env.factory() as db:
        await db.execute(delete(LookRequest))
        await db.execute(delete(LocalAiJob))
        await db.execute(delete(AuditLog).where(AuditLog.event == reviews.AUDIT_STARTED))
        await db.commit()


Ran = Callable[..., Awaitable[tuple[bool, str]]]


@pytest.fixture()
def panel(env: Api, monkeypatch: pytest.MonkeyPatch) -> Ran:
    """The tool as the panel runs it, through C1's dispatcher: registered
    from this install's own settings, the caller bound, the outside world
    the Mac's, and the connector's package not installed. Answers
    (is_error, the text Claude is told)."""
    monkeypatch.setattr(tools, "CHAT_ONLY", {})
    assert chat_tool.register()
    monkeypatch.setattr(chat_tool, "deps_for", lambda: env.site.deps())

    def lane(_settings: Settings | None = None) -> bool:
        env.woken.append(1)
        return env.queue_up

    monkeypatch.setattr(local_ai_tasks, "wake", lane)
    monkeypatch.setitem(sys.modules, "mcp", None)  # `import mcp` now fails

    async def sessions() -> AsyncIterator[Any]:
        async with env.factory() as db:
            yield db

    async def ran(email: str = ADMIN["email"], role: str | None = None, **args: Any) -> Any:
        user = await _user(env, email)
        caller = tools.principal_for(
            user_id=user.id, email=user.email, role=role or user.role, base_url="http://t"
        )
        with auth.bound(caller, sessions):
            outcome = await tools.run(chat_tool.NAME, args, pictures=True)
        [block] = outcome.blocks
        return outcome.is_error, block["text"]

    return ran


# --- the confirm chip -----------------------------------------------------------------


def test_the_chip_says_what_would_be_reviewed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """From the arguments alone: there is no database in this test to look
    anything up in, and the library named does not exist."""
    said_ = chat_tool.describe
    assert said_({"library": "GELCO", "folder": "Pete Dye"}) == (
        "Review GELCO / Pete Dye with the local model, starting now"
    )
    assert said_({"library": "GELCO", "folder": "Pete Dye", "when": "tonight"}) == (
        "Review GELCO / Pete Dye with the local model, tonight"
    )
    # No folder is the whole library, however that is written.
    for whole in ({}, {"folder": ""}, {"folder": "/"}, {"folder": None}):
        assert said_({"library": "GELCO", **whole}) == (
            "Review all of GELCO with the local model, starting now"
        )
    # The folder reads as the tool will read it: the slashes at its ends dropped.
    assert said_(
        {"library": " GELCO ", "folder": "/Pete Dye/2026/", "when": "now", "redo": True}
    ) == (
        "Review GELCO / Pete Dye/2026 with the local model, starting now, "
        "looking again at what another model saw"
    )
    assert said_({"library": "GELCO", "when": "tonight", "redo": True}) == (
        "Review all of GELCO with the local model, tonight, looking again at what another model saw"
    )
    # Only a real true looks again, on the chip as in the tool.
    for no in (False, None, "true", 1):
        assert "looking again" not in said_({"library": "GELCO", "redo": no})

    # Claude wrote the arguments and nothing has checked them yet: the chip
    # still says what was asked, and the tool refuses it after the yes.
    assert said_({}) == "Review all of ? with the local model, starting now"
    assert said_({"library": 7, "folder": "a/../b", "when": "later"}) == (
        'Review ? / a/../b with the local model, starting "later"'
    )
    assert said_({"library": "GELCO", "folder": 12}) == (
        "Review GELCO / 12 with the local model, starting now"
    )

    # Claude may name the library by its id (a page that only knows the id
    # is on screen). Nothing is looked up, so the chip cannot turn it into a
    # name; it says that it is an id rather than pass it off as one — the
    # operator is approving hours of model time and must know what for.
    by_id = "3f2a8c1e-6b1a-4c1e-9d2f-0123456789ab"
    assert said_({"library": by_id, "folder": "Pete Dye"}) == (
        f"Review library {by_id} / Pete Dye with the local model, starting now"
    )
    assert said_({"library": f" {by_id.upper()} ", "when": "tonight"}) == (
        f"Review all of library {by_id.upper()} with the local model, tonight"
    )
    # So the tool asks Claude for the name, everywhere it says what `library` is.
    wanted = "name as Browse shows it"
    assert wanted in chat_tool.INPUT_SCHEMA["properties"]["library"]["description"]
    assert wanted in chat_tool.DESCRIPTION and wanted in chat_tool.NO_LIBRARY
    for says in (
        chat_tool.INPUT_SCHEMA["properties"]["library"]["description"],
        chat_tool.DESCRIPTION,
        chat_tool.NO_LIBRARY,
    ):
        assert "name or id" not in says and "or its id" not in says

    # It is the sentence C1 puts on the chip, and the panel asks first.
    monkeypatch.setattr(tools, "CHAT_ONLY", {})
    assert chat_tool.register(Settings(secret_key=SECRET, runtimes_dir=tmp_path))
    assert tools.needs_confirm(chat_tool.NAME)
    assert tools.describe(chat_tool.NAME, {"library": "GELCO", "folder": "Pete Dye"}) == (
        "Review GELCO / Pete Dye with the local model, starting now"
    )
    # Written as C1's own chips are: the panel reads several out as one list.
    assert not tools.describe("decide_tag", {"tag": "green", "id": "1", "accept": True}).endswith(
        "."
    )


# --- a review, asked for in the chat ----------------------------------------------------


async def test_a_review_asked_for_in_the_chat_leaves_what_the_page_leaves(env: Api) -> None:
    # Asked for on the page first, to hold the tool against.
    on_the_page = await env.started()
    assert env.woken == [1]
    page_left = await _left(env)
    [page_audit] = await _addresses(env)
    await _forget(env)
    assert await _nothing_left(env)

    told = await _tool(env)

    assert told == (
        f"Started: GELCO / {FOLDER}, with {LOOKER_NAME}. {PETE_DYE} {NO_ESTIMATE} "
        "The operator can follow it on Processing (/processing)."
    )
    assert env.woken == [1, 1]
    admin = await _user(env)
    left = await _left(env)
    assert left == page_left
    # Said out, so the comparison cannot pass on two empty lists.
    assert left["requests"] == [
        {
            "library": env.library,
            "folder": FOLDER,
            "when": "now",
            "redo": False,
            "prepare": True,
            "model_id": None,
            "model_name": LOOKER_NAME,
            "state": "active",
            "done": 0,
            "total": 4,
            "prep_state": "",
            "requested_by": admin.id,
            "look_job": "look_folder",
            "prepare_job": None,
        }
    ]
    assert left["jobs"] == [
        {
            "kind": "look_folder",
            "subject": f"GELCO / {FOLDER}",
            "state": "queued",
            "priority": 5,
            "role": "looker",
            "model_id": None,
            "requested_by": admin.id,
            "waits": False,
            "ours": True,
        }
    ]
    assert left["audits"] == [
        {
            "event": "looks.review_started",
            "actor": admin.id,
            "detail": {
                "library": "GELCO",
                "folder": FOLDER,
                "when": "now",
                "model": LOOKER_NAME,
                "redo": False,
                "photos": 2,
                "shots": 2,
                "need_copy": 0,
            },
        }
    ]
    # The page's row carries the browser's address; the chat's has none to give.
    [chat_audit] = await _addresses(env)
    assert (page_audit, chat_audit) == ("127.0.0.1", None)

    # It reads on Processing as the page's own did, and runs on the lane.
    (shown,) = (await env.get(f"{URL}/reviews"))["reviews"]
    for key in ("state", "message", "requested_by", "total", "when", "model_name", "can_cancel"):
        assert shown[key] == on_the_page[key], key
    assert (shown["state"], shown["requested_by"]) == ("waiting", ADMIN["email"])
    await env.drain()
    (done,) = (await env.get(f"{URL}/reviews"))["reviews"]
    assert (done["id"], done["state"], done["done"]) == (shown["id"], "done", 4)
    assert await env.count(Look) == 4

    # Asked again, in the chat or on the page: the same refusal, nothing queued.
    again = await _refused(env)
    assert again == reviews.NOTHING_NEW == said(await env.start())
    assert await env.count(LocalAiJob) == 1 and env.woken == [1, 1]


async def test_the_answer_says_what_there_is_how_long_and_where_to_follow_it(
    env: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A clip with no playable copy, a second folder, one photograph another
    # model has looked at and one this Looker has, a Looker that has been
    # timed (4 s a photograph, 6 s a shot), and local AI paused.
    await _asset(env.env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))
    await _asset(env.env, "Rev 2/IMG_0100.JPG", "image", ("thumbnail", "preview"))
    async with env.factory() as db:
        model = await db.get(LocalModel, env.env["model_id"])
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
        db.add(
            Look(
                asset_id=env.env["photos"][0],
                key=f"google/another-looker@abc#{PROMPT_VERSION}",
                model="google/another-looker",
                model_revision="abc",
                prompt_version=PROMPT_VERSION,
                score=40,
                caption="A fairway.",
            )
        )
        db.add(
            Look(
                asset_id=env.env["photos"][1],
                key=store.key_for(model),
                model=model.repo_id,
                variant=model.variant,
                model_revision=model.revision,
                prompt_version=PROMPT_VERSION,
                score=60,
                caption="A green.",
            )
        )
        config = await local_ai_settings.load_config(db)
        await local_ai_settings.save_config(db, replace(config, paused=True))
    measured = (
        "Under a minute at this model's measured speed (4 s a photograph, 6 s a shot, "
        "from its last 2 answers). Loading the model and making playable copies are extra."
    )
    not_ready = (
        "1 more clip has to be made ready first; each is looked at when it is, or judged "
        "from one still if it cannot be."
    )
    follow = "The operator can follow it on Processing (/processing)."

    # The library by its id, the folder with the slashes Browse does not show,
    # and looking again at what the other model saw — not at what this one did.
    todo = "To look at: 1 photograph and 1 clip (2 shots)."
    told = await _tool(env, library=str(env.library), folder=f"/{FOLDER}/", redo=True)

    # The estimate is the dialog's own, word for word.
    words = (await env.preview(redo=True))["estimate"]["words"]
    assert words == f"{measured} {estimate.NOT_COUNTED}"
    assert told == (
        f"Started: GELCO / {FOLDER}, with {LOOKER_NAME}. {todo} {not_ready} "
        f"{chat_tool.PAUSED} {words} {follow}"
    )
    left = await _left(env)
    [request] = left["requests"]
    assert (request["folder"], request["redo"], request["prep_state"]) == (FOLDER, True, "running")
    # The clip is made ready, as the dialog's box is ticked by default.
    assert (request["prepare"], request["prepare_job"]) == (True, "look_prepare")
    assert [job["kind"] for job in left["jobs"]] == ["look_folder", "look_prepare"]
    detail = left["audits"][0]["detail"]
    assert (detail["redo"], detail["photos"], detail["need_copy"]) == (True, 1, 1)

    # The whole library, by its name in any capitals, for tonight, leaving
    # what either model looked at as it is: the second folder's photograph.
    told = await _tool(env, library="gelco", folder="", when="tonight")
    assert told == (
        f"Set for tonight: all of GELCO, with {LOOKER_NAME}. "
        f"{todo} {not_ready} "
        f"Starts tonight at 21:30. {chat_tool.PAUSED} "
        "Under a minute at this model's measured speed (4 s a photograph, 6 s a shot, "
        f"from its last 2 answers). {estimate.LEFT_OUT} {estimate.NOT_COUNTED} {follow}"
    )
    async with env.factory() as db:
        whole = await db.scalar(select(LookRequest).where(LookRequest.folder == ""))
        assert whole is not None and whole.run_when == "tonight"
        job = await db.get(LocalAiJob, whole.look_job_id)
        assert job is not None and (job.subject, job.not_before is not None) == ("GELCO", True)

    # On the demonstration install Claude is told the answers are made up.
    monkeypatch.setattr(reviews, "availability", lambda _settings=None: Availability("demo"))
    told = await _tool(env, folder="Rev 2")
    assert told == (
        f"Started: GELCO / Rev 2, with {LOOKER_NAME}. To look at: 1 photograph. "
        f"{chat_tool.PAUSED} {measured} "
        f"{reviews.DEMO_LINE} {follow}"
    )
    # Every one of them asked the lane once, and none of it holds a server path.
    assert env.woken == [1, 1, 1]
    assert str(env.env["tmp"]) not in told and env.env["tmp"].as_posix() not in told


async def test_a_library_is_named_by_its_id_or_its_name_in_any_capitals(env: Api) -> None:
    async with env.factory() as db:
        mixed = Library(name="Weddings", root_path=str(env.env["tmp"] / "Weddings"))
        capitals = Library(name="WEDDINGS", root_path=str(env.env["tmp"] / "WEDDINGS"))
        db.add_all([mixed, capitals])
        await db.commit()
    await _asset({**env.env, "library": mixed.id}, "IMG_0001.JPG", "image", ("thumbnail",))

    # Two that differ only in their capitals: neither is guessed at.
    assert await _refused(env, library="weddings", folder="") == (
        "More than one library is called that apart from its capitals: WEDDINGS, Weddings. "
        "Give the name exactly, or the id."
    )
    # Written exactly, each is itself: the empty one has nothing to look at.
    assert await _refused(env, library="WEDDINGS", folder="") == reviews.NOTHING_HERE
    assert await _refused(env, library=str(capitals.id), folder="") == reviews.NOTHING_HERE
    told = await _tool(env, library="Weddings", folder="")
    assert told.startswith(f"Started: all of Weddings, with {LOOKER_NAME}. To look at: 1 photo")
    async with env.factory() as db:
        [request] = list(await db.scalars(select(LookRequest)))
    assert request.library_id == mixed.id

    # No such library: the page's sentence, and the names Claude can choose from.
    names = "The libraries are: GELCO, WEDDINGS, Weddings."
    for unknown in ("Gelko", str(uuid.uuid4())):
        assert await _refused(env, library=unknown) == f"{reviews.NO_SUCH_LIBRARY} {names}"
    for unnamed in ("", "   ", None, 7, ["GELCO"]):
        assert await _refused(env, library=unnamed) == chat_tool.NO_LIBRARY


# --- what Claude is told when it does not start -----------------------------------------


async def test_a_refusal_is_an_answer_not_an_exception(env: Api, panel: Ran) -> None:
    """Through C1's dispatcher, as the panel runs it — and each refusal in
    the words `POST /looks/reviews` answers the same request with."""

    async def on_the_page(**body: Any) -> str:
        response = await env.start(**body)
        assert response.status_code >= 400, response.text
        return said(response)

    async def both(**args: Any) -> str:
        """The tool's sentence, having checked the page says the same."""
        refused, told = await panel(library="GELCO", **args)
        assert refused is True, told
        assert told == await on_the_page(**args), args
        return told

    assert "cannot use . or .." in await both(folder="../elsewhere")
    assert await both(folder="a//b") == "That folder path has an empty part (two slashes in a row)."
    assert await both(folder=FOLDER, when="later") == reviews.BAD_WHEN
    assert await both(folder="Nowhere") == reviews.NOTHING_HERE
    # A library there is none of: the page's sentence first.
    refused, told = await panel(library="Gelko", folder=FOLDER)
    page = await on_the_page(library_id=str(uuid.uuid4()))
    assert (refused, page) == (True, reviews.NO_SUCH_LIBRARY) and told.startswith(f"{page} ")
    # Arguments that are not what the tool takes are Claude's to fix.
    assert await panel(folder=FOLDER) == (True, chat_tool.NO_LIBRARY)
    assert await panel(library="GELCO", folder=FOLDER, redo="yes") == (True, chat_tool.BAD_REDO)
    assert await panel(library="GELCO", folder=["Pete Dye"]) == (
        True,
        "A folder is written as text, such as Brookside/2026.",
    )
    assert await panel(library="GELCO", folder=FOLDER, when=7) == (True, reviews.BAD_WHEN)
    assert await _nothing_left(env) and env.woken == []

    # The queue is down: nothing is left waiting for a worker that will not come.
    env.queue_up = False
    assert await panel(library="GELCO", folder=FOLDER) == (True, reviews.QUEUE_DOWN)
    assert await on_the_page() == reviews.QUEUE_DOWN == local_models_api.QUEUE_DOWN
    assert await _nothing_left(env) and env.woken == [1, 1]
    env.queue_up = True

    # A model that only fits overnight cannot start in the afternoon …
    async with env.factory() as db:
        await db.execute(
            update(LocalModel)
            .where(LocalModel.id == env.env["model_id"])
            .values(memory_gb=12.0, measured_memory_gb=None)
        )
        await db.commit()
    assert await both(folder=FOLDER) == (
        f"This model only runs overnight (needs about {schedule.gb_text(12.0)} GB; "
        f"the daytime budget is {schedule.gb_text(8.0)} GB). Choose Tonight."
    )
    assert await _nothing_left(env)
    # … waits for the window when asked for tonight, and is then already running.
    started, told = await panel(library="GELCO", folder=FOLDER, when="tonight")
    assert started is False
    assert told.startswith(f"Set for tonight: GELCO / {FOLDER}, with {LOOKER_NAME}. {PETE_DYE}")
    assert "Starts tonight at 21:30." in told
    assert await both(folder=FOLDER, when="tonight") == reviews.ALREADY_RUNNING
    assert await env.count(LookRequest) == 1

    # With no Looker nothing can start.
    async with env.factory() as db:
        await db.execute(delete(LocalAiRole))
        await db.commit()
    assert await both(folder="Other") == reviews.NO_LOOKER
    assert await env.count(LookRequest) == 1 and env.woken == [1, 1, 1]


async def test_only_an_administrator_can_start_one_from_chat(env: Api, panel: Ran) -> None:
    member = await env.account("crew@example.com", "user")
    await env.account("viewer@example.com", "readonly")

    for email in ("crew@example.com", "viewer@example.com"):
        assert await _refused(env, email) == reviews.ADMIN_ONLY
        assert await panel(email, library="GELCO", folder=FOLDER) == (True, reviews.ADMIN_ONLY)
    # As the page refuses them.
    assert (await env.start(member)).status_code == 403
    # Who they are is read when the tool runs, not when the turn began: a
    # caller bound as an administrator who no longer is one starts nothing.
    assert await panel("crew@example.com", role="admin", library="GELCO", folder=FOLDER) == (
        True,
        reviews.ADMIN_ONLY,
    )
    # Refused before anything about the call is looked at.
    assert await _refused(env, "crew@example.com", library="Nowhere") == reviews.ADMIN_ONLY
    assert await _nothing_left(env) and env.woken == []

    # An administrator switched off since, and one whose account is gone.
    async with env.factory() as db:
        await db.execute(update(User).where(User.email == ADMIN["email"]).values(disabled=True))
        await db.commit()
    assert await panel(library="GELCO", folder=FOLDER) == (True, reviews.ADMIN_ONLY)
    async with env.factory() as db:
        await db.execute(update(User).where(User.email == ADMIN["email"]).values(disabled=False))
        await db.commit()
    gone = tools.principal_for(
        user_id=uuid.uuid4(), email="gone@example.com", role="admin", base_url="http://t"
    )

    async def sessions() -> AsyncIterator[Any]:
        async with env.factory() as db:
            yield db

    with auth.bound(gone, sessions):
        outcome = await tools.run(
            chat_tool.NAME, {"library": "GELCO", "folder": FOLDER}, pictures=True
        )
    assert (outcome.is_error, outcome.blocks[0]["text"]) == (True, reviews.ADMIN_ONLY)
    assert await _nothing_left(env) and env.woken == []

    # The administrator, asked, starts one.
    started, told = await panel(library="GELCO", folder=FOLDER)
    assert started is False and told.startswith(f"Started: GELCO / {FOLDER}, with ")
    assert env.woken == [1] and await env.count(LookRequest) == 1


async def test_nothing_starts_from_chat_where_models_cannot_run(
    env: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.account("crew@example.com", "user")
    monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR")
    get_settings.cache_clear()
    here = replace(env.site.deps(), settings=get_settings())
    env.app.dependency_overrides[get_deps] = lambda: here

    async def refused(email: str, library: str = "GELCO") -> str:
        async with env.factory() as db:
            user = await db.scalar(select(User).where(User.email == email))
            assert user is not None
            with pytest.raises(tools.ToolProblem) as raised:
                await chat_tool.start(
                    db, user, {"library": library, "folder": FOLDER}, deps=here, wake=_wake(env)
                )
            return str(raised.value)

    # The page's sentence, and the page's order: who is asking comes first,
    # then whether this install can at all, and only then what was asked for.
    assert await refused(ADMIN["email"]) == UNAVAILABLE == said(await env.start())
    assert await refused(ADMIN["email"], "Nowhere") == UNAVAILABLE
    assert await refused("crew@example.com") == reviews.ADMIN_ONLY
    assert await _nothing_left(env) and env.woken == []
    # And the panel is not given the tool there at all.
    monkeypatch.setattr(tools, "CHAT_ONLY", {})
    assert chat_tool.register() is False and tools.CHAT_ONLY == {}


async def test_a_model_whose_program_is_not_installed_is_refused_before_a_review_starts(
    env: Api,
) -> None:
    """The page refuses such a review up front; the chat started it anyway (the
    request, its jobs, the wake-up and the audit row) and it failed at once.
    Now the chat is refused in the page's words and leaves nothing."""
    settings = env.env["settings"]
    shutil.rmtree(settings.runtimes_dir / "mlx-vlm")
    sentence = ask.program_problem("mlx-vlm", settings, {"mlx-vlm": MlxVlm()})
    assert sentence is not None and "Install it under Software that runs models" in sentence

    for when in ("now", "tonight"):
        assert await _refused(env, when=when) == sentence
        assert said(await env.start(when=when)) == sentence
    assert await _nothing_left(env) and env.woken == []
    assert await env.audits(reviews.AUDIT_STARTED) == []


async def test_a_queue_that_is_down_leaves_nothing_and_a_review_the_lane_took_stands(
    env: Api,
) -> None:
    """As the route: the review is committed before the lane is woken, so it
    is taken back when the wake-up fails — unless the once-a-minute tick
    claimed one of its jobs meanwhile, and then it stands as started."""
    env.queue_up = False
    assert await _refused(env) == reviews.QUEUE_DOWN
    assert env.woken == [1] and await _nothing_left(env)

    # A clip with no playable copy: the review has a make-ready job too, and
    # the lane claims it while the wake-up is failing.
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
    told = await _tool(env)
    assert told.startswith(f"Started: GELCO / {FOLDER}, with {LOOKER_NAME}. {PETE_DYE} 1 more clip")
    async with env.factory() as db:
        states = dict((await db.execute(select(LocalAiJob.kind, LocalAiJob.state))).all())
    assert states == {"look_prepare": "running", "look_folder": "queued"}
    assert await env.count(LookRequest) == 1
    assert [row["folder"] for row in await env.audits(reviews.AUDIT_STARTED)] == [FOLDER]

    # Removed in the moment the lane was being woken: nothing started, and
    # no audit row says one did.
    await _forget(env)
    env.queue_up = True

    async def removed() -> None:
        async with env.factory() as db:
            await db.execute(delete(LookRequest))
            await db.commit()

    env.while_waking = removed
    assert await _refused(env) == reviews.NO_SUCH_LIBRARY
    assert await env.audits(reviews.AUDIT_STARTED) == []


# --- where the tool is offered, and what it is ------------------------------------------


def test_the_tool_definition_is_valid_for_claude() -> None:
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", chat_tool.NAME)
    schema = chat_tool.INPUT_SCHEMA
    assert schema["type"] == "object" and schema["required"] == ["library"]
    assert set(schema["properties"]) == {"library", "folder", "when", "redo"}
    assert schema["properties"]["when"]["enum"] == ["now", "tonight"]
    assert all(field.get("type") for field in schema["properties"].values())
    assert json.loads(json.dumps(schema)) == schema, "sent to the API as JSON, byte for byte"
    # What tells Claude what it does, what it costs, and that a person is asked.
    said_ = chat_tool.DESCRIPTION
    assert "local AI model" in said_ and "asked before it starts" in said_
    assert said_.isascii()


def test_the_tool_is_offered_only_where_models_can_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tools, "CHAT_ONLY", {})
    nowhere = Settings(secret_key=SECRET)
    assert not chat_tool.offered(nowhere)
    assert chat_tool.register(nowhere) is False and tools.CHAT_ONLY == {}
    assert not tools.needs_confirm(chat_tool.NAME)
    # The demo shows it (and its answer says the looks are made up) …
    assert chat_tool.offered(Settings(secret_key=SECRET, local_ai_demo=True))
    # … but never somebody's real, public install.
    assert not chat_tool.offered(
        Settings(secret_key=SECRET, local_ai_demo=True, domain="media.example.com")
    )
    assert chat_tool.register(Settings(secret_key=SECRET, runtimes_dir=tmp_path)) is True
    own = tools.CHAT_ONLY[chat_tool.NAME]
    assert (own.run, own.describe, own.confirm) == (chat_tool.run, chat_tool.describe, True)
    assert tools.needs_confirm(chat_tool.NAME)
    # A panel-only tool asks through its own entry, not the connector's list.
    assert chat_tool.NAME not in tools.CONFIRM_FIRST


async def test_the_panel_lists_it_after_the_connectors_tools_and_the_editors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("mcp")
    from framefound.mcp.server import build_server

    connector = [tool.name for tool in await build_server().list_tools()]
    assert chat_tool.NAME not in connector

    monkeypatch.setattr(tools, "CHAT_ONLY", {})
    nowhere = Settings(secret_key=SECRET)
    draft_chat.register(nowhere)
    chat_tool.register(nowhere)
    assert [spec["name"] for spec in await tools.specs()] == connector

    here = Settings(secret_key=SECRET, runtimes_dir=tmp_path)
    assert chat_tool.register(here) and draft_chat.register(here)
    listed = await tools.specs()
    # The connector's in its own order, then the panel's own by name: the
    # order is part of what Claude's prompt cache keys on.
    assert [spec["name"] for spec in listed] == [
        *connector,
        "local_cut_draft",
        "start_folder_review",
    ]
    assert listed[-1] == {
        "name": chat_tool.NAME,
        "description": chat_tool.DESCRIPTION,
        "input_schema": chat_tool.INPUT_SCHEMA,
    }
    # Only the review asks first: a draft changes nothing and costs nothing.
    assert tools.needs_confirm("start_folder_review")
    assert not tools.needs_confirm("local_cut_draft")
    assert [tool.name for tool in await build_server().list_tools()] == connector, (
        "the external connector does not gain the panel's tool"
    )


def _imported_afresh(name: str, path: str) -> Any:
    """A module's file run as an import runs it, without replacing the one
    every other test holds: what its body does on import is done again."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_importing_the_api_is_what_gives_the_panel_the_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a real install nothing calls `register`: the module's own last
    line does, when `api/v1/looks.py` imports it. Every other test here
    registers the tool itself (this process cannot run models when the
    modules are first imported), so with that line gone, or the API's
    import gone, they all passed and the panel never had the tool."""
    name = chat_tool.__name__

    def install(runtimes: Path | None) -> None:
        monkeypatch.setattr(tools, "CHAT_ONLY", {})
        if runtimes is None:
            monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR", raising=False)
        else:
            monkeypatch.setenv("FRAMEFOUND_RUNTIMES_DIR", str(runtimes))
        get_settings.cache_clear()

    # The module, imported on a Mac that runs models: the tool is there,
    # and it is the one that asks first.
    install(tmp_path)
    fresh = _imported_afresh(name, chat_tool.__file__)
    own = tools.CHAT_ONLY[chat_tool.NAME]
    assert (own.run, own.describe, own.confirm) == (fresh.run, fresh.describe, True)
    assert tools.needs_confirm(chat_tool.NAME)
    assert tools.describe(chat_tool.NAME, {"library": "GELCO"}) == (
        "Review all of GELCO with the local model, starting now"
    )
    # Imported where models cannot run (the containers): Claude is not
    # offered a tool that could only answer "not on this install".
    install(None)
    _imported_afresh(name, chat_tool.__file__)
    assert tools.CHAT_ONLY == {}

    # And the API is what imports it, however that import is spelled: with
    # the module not yet loaded, loading `api/v1/looks.py` loads it.
    install(tmp_path)
    monkeypatch.delitem(sys.modules, name)
    monkeypatch.delattr(looking, "chat_tool")
    _imported_afresh(api.__name__, api.__file__)
    assert sys.modules[name] is not chat_tool, "the API's import loaded the module"
    assert list(tools.CHAT_ONLY) == [chat_tool.NAME]
    assert tools.CHAT_ONLY[chat_tool.NAME].confirm is True


def test_the_tool_never_needs_the_connector_or_the_claude_sdk() -> None:
    """The module reads the caller from `framefound.mcp.auth` only inside
    `run`, and never imports the optional `mcp` package or `anthropic`."""
    tree = ast.parse(Path(chat_tool.__file__).read_text(encoding="utf-8"))

    def imports(node: ast.AST) -> list[str]:
        found: list[str] = []
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom):
                found.append(inner.module or "")
            elif isinstance(inner, ast.Import):
                found += [alias.name for alias in inner.names]
        return [
            name
            for name in found
            if name.split(".")[0] in ("mcp", "anthropic") or name.startswith("framefound.mcp")
        ]

    assert imports(tree) == ["framefound.mcp.auth"]
    [run] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
    ]
    assert imports(run) == ["framefound.mcp.auth"], "read only when the panel calls the tool"
    # The chip's sentence takes the arguments and nothing else: it has no
    # session to look anything up with.
    [describe] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "describe"
    ]
    assert [arg.arg for arg in describe.args.args] == ["args"]
