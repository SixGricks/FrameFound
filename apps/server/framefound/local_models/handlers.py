"""The four kinds of L1's work on the `local_ai` lane, registered at import.

- `download`: fetch a model's files into the library; the row becomes
  `installed`, or `incomplete` with the sentence saying why and that Resume
  carries on.
- `place`: copy a model to this machine, then give it the role it was
  waiting for. Until the copy is whole the old model keeps the role (D13).
- `runtime_install`: install a version of a runtime into its own folder,
  then switch to it. A failure leaves the version in use alone.
- `test`: ask a model one question about a sample and keep the answer, the
  time and the memory on the job's own row. Nothing a test says reaches the
  catalogue.

`processing/local_ai_tasks.py` imports this module, so both the worker and
the API know these kinds. A later phase's kinds that live in this package
are imported at the foot of this file; any other package's at the foot of
`local_ai_tasks` (13.2).
"""

import asyncio
import contextlib
import io
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.auth.crypto import SecretUnavailable
from framefound.config import Settings
from framefound.db.models import Asset, Derivative, LocalAiJob, LocalModel, catalogued
from framefound.local_models import ROLE_TITLES, ROLES, library, registry, runtimes
from framefound.local_models.ask import (
    NO_SUCH_MODEL,
    Answer,
    ModelFailed,
    NotNow,
    ServerRefused,
    ServerUnreachable,
    ask,
)
from framefound.local_models.hub import UNREACHABLE, HubError, Stopped
from framefound.local_models.jobs import Cancelled, Failed, JobContext, Why, in_use, register
from framefound.local_models.kinds import is_demo_runtime
from framefound.local_models.settings_store import load_config
from framefound.storage import stores

log = structlog.get_logger()

_GB = 1024**3
RAW_LENGTH = 4000

TOKEN_UNREADABLE = "The stored Hugging Face token can no longer be read. Paste it again."  # noqa: S105 - a sentence about the token, not one
NO_UV = "FrameFound's installer (uv) is missing on this Mac. Run `framefound install` again."
DOWNLOAD_STOPPED = (
    "The download stopped without finishing: the worker running it was restarted. "
    "What was downloaded is kept; Resume carries on from {done} GB."
)
DOWNLOAD_KEPT = "What was downloaded is kept; Resume carries on from {done} GB."
NO_SUCH_PICTURE = "That picture is no longer in the catalogue."
COPY_NOT_FINISHED = (
    "It has the role now, but its copy on this machine could not be finished. "
    f"{library.NOT_ON_THIS_MACHINE}"
)

# --- the test ----------------------------------------------------------------------

TEST_PROMPT_VERSION = "test-1"


class TestLook(BaseModel):
    caption: str = Field(max_length=300)
    tags: list[str] = Field(max_length=5)


class TestText(BaseModel):
    order: list[int] = Field(min_length=3, max_length=3)
    reason: str = Field(max_length=300)


LOOK_INSTRUCTIONS = (
    "You describe photographs and video stills for a media catalogue. "
    "Answer with one JSON object and nothing else."
)
LOOK_PROMPT = (
    "Describe this picture in one sentence, then list up to five plain tags for what is in it."
)
TEXT_PROMPT = (
    "Put these three shots in a sensible order for the opening of a property video and say why "
    "in one line. 1: slow push in on the front door, golden light. 2: high wide of the whole lot, "
    "overcast. 3: orbit right around the house, golden light."
)

NEWEST = "The newest photograph in the catalogue"
CHOSEN = "The picture you chose"
DRAWN = "A picture FrameFound drew"
WORDS = "A question in words (it does not read pictures)"
# How many of the newest photographs are tried before the drawn one is used.
_CANDIDATES = 3
# How long a picture's store is given to hand it over. Previews may live on a
# share; one that has hung must not hold the lane (it runs one job at a time).
SAMPLE_WAIT_S = 60.0
SAMPLE_SILENT = "its store did not answer within a minute."


@dataclass(frozen=True)
class Sample:
    jpeg: bytes | None  # None for a text test
    asset_id: uuid.UUID | None
    label: str
    media_type: str | None = None
    filename: str | None = None


def drawn_picture() -> bytes:
    """A 640x480 JPEG drawn here: sky, grass, a red barn. What a Test shows a
    model when the catalogue has no photograph that can be read."""
    from PIL import Image, ImageDraw

    picture = Image.new("RGB", (640, 480), (135, 190, 235))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 330, 640, 480), fill=(86, 150, 70))
    draw.rectangle((220, 210, 420, 360), fill=(170, 40, 35))
    draw.polygon([(205, 215), (320, 130), (435, 215)], fill=(120, 30, 28))
    draw.rectangle((295, 280, 345, 360), fill=(245, 240, 230))
    draw.ellipse((520, 40, 590, 110), fill=(250, 225, 120))
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=85)
    return out.getvalue()


async def pick_sample(
    db: AsyncSession, settings: Settings, model: LocalModel, asset_id: uuid.UUID | None
) -> Sample:
    """What a Test shows the model.

    A model that does not take pictures gets the text test. Otherwise the
    asset asked for (a clip by its poster), else the newest ready photograph
    whose preview can be read, else the drawn picture.
    """
    takes: object = model.takes
    if not isinstance(takes, list) or "image" not in takes:
        return Sample(None, None, WORDS)
    if asset_id is not None:
        asset = await db.get(Asset, asset_id)
        if asset is None:
            raise Failed(NO_SUCH_PICTURE)
        kinds = ("poster",) if asset.media_type == "video" else ("preview", "thumbnail")
        relative = await _derived(db, asset.id, kinds)
        if relative is None:
            raise Failed(f"{asset.filename} has no picture FrameFound can show a model yet.")
        try:
            jpeg = await _picture(relative, settings)
        except _Unreadable as err:
            raise Failed(f"The picture of {asset.filename} could not be read: {err}") from None
        return Sample(jpeg, asset.id, CHOSEN, asset.media_type, asset.filename)
    newest = await db.execute(
        select(Asset.id, Asset.filename, Derivative.relative_path)
        .join(Derivative, Derivative.asset_id == Asset.id)
        .where(
            catalogued(),
            Asset.media_type == "image",
            Asset.processing_status == "ready",
            Derivative.kind == "preview",
            Derivative.status == "ready",
        )
        .order_by(Asset.first_indexed_at.desc())
        .limit(_CANDIDATES)
    )
    for found_id, filename, relative in newest.all():
        try:
            jpeg = await _picture(relative, settings)
        except _Silent:
            break  # the next is most likely on the same store: draw one instead
        except _Unreadable:
            continue  # its store is away, or the file went: try the next
        return Sample(jpeg, found_id, NEWEST, "image", filename)
    return Sample(await asyncio.to_thread(drawn_picture), None, DRAWN, "image", None)


async def _derived(db: AsyncSession, asset_id: uuid.UUID, kinds: tuple[str, ...]) -> str | None:
    rows = await db.execute(
        select(Derivative.kind, Derivative.relative_path).where(
            Derivative.asset_id == asset_id,
            Derivative.kind.in_(kinds),
            Derivative.status == "ready",
        )
    )
    found = {str(kind): str(path) for kind, path in rows.all()}
    return next((found[kind] for kind in kinds if kind in found), None)


class _Unreadable(Exception):
    """A sample's picture could not be read; the message says why."""


class _Silent(_Unreadable):
    """A sample's store did not answer in time."""


async def _picture(relative_path: str, settings: Settings) -> bytes:
    """`_read_picture` in a thread, given SAMPLE_WAIT_S. A read that does not come
    back is left to its thread: the Test goes on without it."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_read_picture, relative_path, settings), SAMPLE_WAIT_S
        )
    except TimeoutError:
        raise _Silent(SAMPLE_SILENT) from None


def _read_picture(relative_path: str, settings: Settings) -> bytes:
    """In a thread: the derived picture, re-encoded as the small JPEG a model is shown."""
    from framefound.ai.tag_review import preview_from_file

    located = stores.locate(relative_path, settings)
    if not located.there:
        raise _Unreadable(located.away or "its picture file is missing.")
    try:
        return preview_from_file(located.path)
    except Exception as err:  # a damaged file is a reason to say so, not to crash
        raise _Unreadable(f"it could not be opened ({type(err).__name__}).") from None


async def _test(ctx: JobContext, job: LocalAiJob) -> dict[str, Any]:
    try:
        return await _tested(ctx, job)
    finally:
        if job.model_id is not None:
            await _tidy_copy(ctx, job.model_id)


async def _tidy_copy(ctx: JobContext, model_id: uuid.UUID) -> None:
    """With the Test over, remove the model's copy on this machine when it holds
    and waits for no role. Giving a role away leaves the old holder's copy in
    place while a Test is reading from it; this is where it goes. Best effort."""
    settings = ctx.deps.settings
    try:
        if not await asyncio.to_thread(library.copies_to_this_machine, settings):
            return  # the library is here: there is no separate copy
        async with ctx.factory() as db:
            if model_id in await registry.holders(db):
                return
            files = await _snapshot_or_none(db, model_id)
        if files is not None:
            await asyncio.to_thread(_drop_local, files, settings)
    except Exception:  # a copy left behind is space, never a reason to fail a Test
        log.warning("local_ai.copy_not_tidied", model_id=str(model_id), exc_info=True)


async def _tested(ctx: JobContext, job: LocalAiJob) -> dict[str, Any]:
    settings = ctx.deps.settings
    async with ctx.factory() as db:
        model = await _model_of(db, job)
        sample = await pick_sample(db, settings, model, _asset_id(job.payload))
        picture = sample.jpeg is not None
        schema: type[BaseModel] = TestLook if picture else TestText
        try:
            answer = await ask(
                db,
                model.id,
                prompt=LOOK_PROMPT if picture else TEXT_PROMPT,
                prompt_version=TEST_PROMPT_VERSION,
                instructions=LOOK_INSTRUCTIONS if picture else "",
                pictures=(sample.jpeg,) if sample.jpeg is not None else (),
                schema=schema,
                server=ctx.deps.server,
                now=ctx.deps.now(),
                settings=settings,
            )
        except (ModelFailed, NotNow) as err:
            # What ask() recorded on the model (which runtime last loaded it,
            # the memory seen) is kept whichever way it ended.
            await db.commit()
            if not isinstance(err, ModelFailed) or err.reason != "bad_answer":
                raise
            # An answer in prose is a finding about the model, not an error.
            model = await _model_of(db, job)
            await _record_unparsed(ctx, model)
            return _test_result(sample, model, None, err.text, err.finish_reason)
        await db.commit()
    await ctx.record(answer)
    return _test_result(sample, model, answer, answer.text, answer.finish_reason)


async def _record_unparsed(ctx: JobContext, model: LocalModel) -> None:
    """The job's provenance when no Answer came back: the build asked, the
    prompt, and the runtime the model last loaded under."""
    runtime, _slash, version = (model.last_loaded_with or "").partition("/")
    async with ctx.factory() as db:
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == ctx.job_id)
            .values(
                model=model.repo_id,
                model_revision=model.revision,
                runtime=runtime or model.runtime,
                runtime_version=version or None,
                prompt_version=TEST_PROMPT_VERSION,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()


def _test_result(
    sample: Sample,
    model: LocalModel,
    answer: Answer | None,
    text: str,
    finish_reason: str,
) -> dict[str, Any]:
    """The job's result: what the page's Test card shows (design 6.10)."""
    parsed = None if answer is None or answer.parsed is None else answer.parsed.model_dump()
    runtime, _slash, version = (model.last_loaded_with or "").partition("/")
    return {
        "kind": "picture" if sample.jpeg is not None else "text",
        "sample": {
            "asset_id": None if sample.asset_id is None else str(sample.asset_id),
            "media_type": sample.media_type,
            "filename": sample.filename,
            "label": sample.label,
        },
        "answer": parsed,
        "raw": text[:RAW_LENGTH],
        "structured_ok": parsed is not None,
        "seconds": None if answer is None else answer.seconds,
        "load_seconds": None if answer is None else answer.load_seconds,
        "memory_gb": None if answer is None else answer.memory_gb,
        "memory_source": None if answer is None else answer.memory_source,
        "ran_from": None if answer is None else answer.ran_from,
        "attempts": 2 if answer is None else answer.attempts,
        "finish_reason": finish_reason,
        "model": {"repo_id": model.repo_id, "revision_short": model.revision[:7]},
        "runtime": answer.runtime if answer is not None else (runtime or model.runtime),
        "runtime_version": answer.runtime_version if answer is not None else (version or None),
        "prompt_version": TEST_PROMPT_VERSION,
        "demo": answer.demo if answer is not None else is_demo_runtime(model.runtime),
    }


# --- download ------------------------------------------------------------------------


async def _download(ctx: JobContext, job: LocalAiJob) -> dict[str, Any]:
    settings = ctx.deps.settings
    async with ctx.factory() as db:
        model = await _model_of(db, job)
        try:
            token = (await load_config(db)).hf_token()
        except SecretUnavailable:
            await _model_state(db, model.id, registry.INCOMPLETE, TOKEN_UNREADABLE)
            await db.commit()
            raise Failed(TOKEN_UNREADABLE) from None
        try:
            files = registry.snapshot(model)
        except ValueError as err:
            await _model_state(db, model.id, registry.INCOMPLETE, str(err))
            await db.commit()
            raise Failed(str(err)) from None
        await _model_state(db, model.id, registry.DOWNLOADING, None)
        await db.commit()
    hub = ctx.deps.hub_for(token)

    def fetch(report: library.Report, should_stop: Callable[[], bool]) -> None:
        library.download(
            hub,
            files,
            settings=settings,
            report=report,
            should_stop=should_stop,
            sleep=ctx.deps.sleep,
        )

    try:
        await ctx.run_blocking(fetch)
    except Stopped:
        said = f"Cancelled. Resume carries on from {_gb(ctx.latest.done)} GB."
        await _model_ended(ctx, files.id, said)
        raise Cancelled(said) from None
    except library.LibraryProblem as err:
        await _model_ended(ctx, files.id, str(err))
        raise
    except HubError as err:
        said = _dropped(err, ctx.latest.done, ctx.latest.total)
        await _model_ended(ctx, files.id, said)
        raise Failed(said) from None
    async with ctx.factory() as db:
        await _model_state(db, files.id, registry.INSTALLED, None)
        await db.commit()
    return {"bytes": sum(file.size for file in files.files)}


def _dropped(err: HubError, done: int, total: int) -> str:
    """Row 1 of the way-out table: where it stopped, and that it is kept."""
    if err.problem == "unreachable" and str(err) == UNREACHABLE and total:
        return (
            f"The connection dropped at {_gb(done)} of {_gb(total)} GB. "
            "What was downloaded is kept."
        )
    return f"{err} What was downloaded is kept." if done else str(err)


async def _model_ended(ctx: JobContext, model_id: uuid.UUID, said: str) -> None:
    async with ctx.factory() as db:
        await _model_state(db, model_id, registry.INCOMPLETE, said)
        await db.commit()


async def _download_abandoned(db: AsyncSession, job: LocalAiJob, why: Why) -> None:
    """A download that ended without its handler: the model must not read as
    downloading for ever."""
    if job.model_id is None:
        return
    if why == "cancelled":
        said = f"Cancelled. Resume carries on from {_gb(job.done)} GB."
    elif why == "stopped":
        said = DOWNLOAD_STOPPED.format(done=_gb(job.done))
    elif why == "failed":
        said = f"{job.message} {DOWNLOAD_KEPT.format(done=_gb(job.done))}"
    else:
        said = job.message
    await db.execute(
        update(LocalModel)
        .where(LocalModel.id == job.model_id, LocalModel.state == registry.DOWNLOADING)
        .values(state=registry.INCOMPLETE, error=said[:500])
        .execution_options(synchronize_session=False)
    )


# --- place: a role's copy on this machine ------------------------------------------------


async def _place(ctx: JobContext, job: LocalAiJob) -> dict[str, Any]:
    settings = ctx.deps.settings
    role = _role_of(job)
    async with ctx.factory() as db:
        model = await _model_of(db, job)
        try:
            files = registry.snapshot(model)
        except ValueError as err:
            await registry.drop_pending(db, role, model.id)
            await db.commit()
            raise Failed(str(err)) from None
    # Where the library is as this starts: a move of it while the copy is
    # made is noticed below.
    before = await asyncio.to_thread(_library_folder, settings)

    def copy(report: library.Report, should_stop: Callable[[], bool]) -> None:
        library.place(files, settings=settings, report=report, should_stop=should_stop)

    try:
        await ctx.run_blocking(copy)
    except Stopped:
        await _not_waiting(ctx, role, files.id)
        raise Cancelled() from None
    except library.LibraryProblem:
        # The old model keeps the role: the waiting one does not get it.
        await _not_waiting(ctx, role, files.id)
        raise

    async with ctx.factory() as db:
        handover = await registry.complete_role(db, role, files.id, user_id=job.requested_by)
        held = await registry.holders(db)
        await db.commit()
        dropped: library.ModelFiles | None = None
        previous = handover.previous
        if handover.changed and previous is not None and previous != files.id:
            # A copy another job is reading from stays; that job tidies it.
            if previous not in held and not await in_use(db, previous, ctx.deps.now()):
                dropped = await _snapshot_or_none(db, previous)
        elif not handover.changed and files.id not in held:
            # The operator chose another model, or took the role away, while
            # this copy was made: nothing will run from it.
            dropped = files
    if dropped is not None:
        await asyncio.to_thread(_drop_local, dropped, settings)
    await _unload(ctx)
    if handover.changed:
        await _keep_here(ctx, files, before)
    return {"role": role, "given": handover.changed}


async def _keep_here(ctx: JobContext, files: library.ModelFiles, before: Path | None) -> None:
    """After the role changed hands: make sure its copy is on this machine.

    A move of the model library gives every model that has a role, or is
    waiting for one, its own copy here just before the switch, from the
    roles it reads at that moment. A role given in the seconds between that
    read and the switch gets none — so this waits out a move in its last
    steps and, when the library moved meanwhile or the copy here is not
    whole, places the model again (a no-op when it is already whole).
    """
    settings, sleep = ctx.deps.settings, ctx.deps.sleep

    def keep(report: library.Report, should_stop: Callable[[], bool]) -> bool:
        waited = 0.0
        while waited < library.MOVE_WAIT_S and _moving(settings):
            if should_stop():
                raise Stopped()
            sleep(library.MOVE_POLL_S)
            waited += library.MOVE_POLL_S
        moved = _library_folder(settings) != before
        if not moved and library.presence(files, settings).on_this_machine:
            return False
        library.place(files, settings=settings, report=report, should_stop=should_stop)
        return True

    try:
        if await ctx.run_blocking(keep):
            log.info("local_ai.role_copy_made_again", model_id=str(files.id))
    except (Stopped, library.LibraryProblem) as err:
        raise Failed(COPY_NOT_FINISHED, detail=str(err)) from None


def _moving(settings: Settings) -> bool:
    """A move of the model library is in its last steps (its writers are paused)."""
    return stores.read_state(settings.data_dir).is_paused(library.KIND)


def _library_folder(settings: Settings) -> Path | None:
    try:
        return stores.kind_folder(library.KIND, settings)
    except (ValueError, OSError):
        return None


async def _not_waiting(ctx: JobContext, role: str, model_id: uuid.UUID) -> None:
    async with ctx.factory() as db:
        await registry.drop_pending(db, role, model_id)
        await db.commit()


async def _snapshot_or_none(db: AsyncSession, model_id: uuid.UUID) -> library.ModelFiles | None:
    model = await registry.get_model(db, model_id)
    if model is None:
        return None
    try:
        return registry.snapshot(model)
    except ValueError:
        return None


def _drop_local(files: library.ModelFiles, settings: Settings) -> None:
    """A copy nothing runs from any more. Best effort: it is only space."""
    try:
        library.drop_local(files, settings)
    except (OSError, library.LibraryProblem):
        log.warning("local_ai.copy_not_removed", model_id=str(files.id), exc_info=True)


async def _place_abandoned(db: AsyncSession, job: LocalAiJob, _why: Why) -> None:
    """A copy that ended without its handler: the role stops waiting for it."""
    role = job.payload.get("role") if isinstance(job.payload, dict) else None
    role = role or job.role
    if job.model_id is not None and role in ROLES:
        await registry.drop_pending(db, role, job.model_id)


# --- runtime install -------------------------------------------------------------------


# shutil.which, here so a test can say whether uv is on this machine.
_which: Callable[[str], str | None] = shutil.which


async def _install(ctx: JobContext, job: LocalAiJob) -> dict[str, Any]:
    deps = ctx.deps
    name = job.payload.get("name") if isinstance(job.payload, dict) else None
    kind = deps.kinds.get(name) if isinstance(name, str) else None
    if kind is None:
        raise Failed(registry.NO_PROGRAM_HERE)
    why = kind.supported_here(deps.system, deps.machine)
    if why is not None:
        raise Failed(why)
    if kind.needs_uv and _which(deps.uv) is None:
        raise Failed(NO_UV)
    given = job.payload.get("version")
    asked: str
    if isinstance(given, str) and given:
        asked = given
    else:
        ctx.say(f"Looking up the newest version of {kind.label}…")
        asked = await asyncio.to_thread(kind.latest, deps.fetch_json)
    try:
        version = runtimes.clean_version(kind, asked)
    except ValueError as err:
        raise Failed(str(err)) from None
    root = runtimes.root(deps.settings)

    def install(_report: library.Report, _should_stop: Callable[[], bool]) -> None:
        # An install is not stopped half-way: a venv left half-built is worse
        # than the minutes it takes to finish (a failed one removes itself).
        runtimes.install(kind, version, root, uv=deps.uv, run=deps.run, say=ctx.say)

    await ctx.run_blocking(install)
    await asyncio.to_thread(runtimes.activate, root, kind.name, version)
    # A model loaded under the old version is loaded again under the new one.
    await _unload(ctx)
    return {"version": version}


# --- the parts ---------------------------------------------------------------------------


async def _model_of(db: AsyncSession, job: LocalAiJob) -> LocalModel:
    model = None if job.model_id is None else await registry.get_model(db, job.model_id)
    if model is None:
        raise Failed(NO_SUCH_MODEL)
    return model


async def _model_state(
    db: AsyncSession, model_id: uuid.UUID, state: str, error: str | None
) -> None:
    await db.execute(
        update(LocalModel)
        .where(LocalModel.id == model_id)
        .values(state=state, error=None if error is None else error[:500])
        .execution_options(synchronize_session=False)
    )


def _role_of(job: LocalAiJob) -> str:
    role = job.payload.get("role") if isinstance(job.payload, dict) else None
    role = role or job.role
    if role not in ROLES:
        raise Failed("This copy was not asked for a role FrameFound knows.")
    return str(role)


def _asset_id(payload: object) -> uuid.UUID | None:
    raw = payload.get("asset_id") if isinstance(payload, dict) else None
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise Failed(NO_SUCH_PICTURE) from None


async def _unload(ctx: JobContext) -> None:
    """Ask the model server to let go of what it has loaded. Best effort: the
    next request loads what it needs either way."""
    with contextlib.suppress(ServerRefused, ServerUnreachable):
        await asyncio.to_thread(ctx.deps.server.unload)


def _gb(size: int) -> str:
    return f"{size / _GB:.1f}"


def _name(job: LocalAiJob) -> str:
    """The model's own name from a subject `publisher/name@revision`."""
    return job.subject.partition("@")[0].rpartition("/")[2] or job.subject


register(
    "download",
    _download,
    needs_model=False,
    title=lambda job: f"Downloading {_name(job)}",
    abandoned=_download_abandoned,
)
register(
    "place",
    _place,
    needs_model=False,
    title=lambda job: (
        f"Copying {_name(job)} to this machine for the "
        f"{ROLE_TITLES.get(str(job.payload.get('role') or job.role), 'role')}"
    ),
    abandoned=_place_abandoned,
)
register(
    "runtime_install",
    _install,
    needs_model=False,
    title=lambda job: f"Installing {job.subject}",
)
register("test", _test, needs_model=True, title=lambda job: f"Testing {_name(job)}")

# Later phases' kinds that live in this package are imported here (13.2).
from framefound.local_models import compare_job  # noqa: E402, F401 - registers compare_model
