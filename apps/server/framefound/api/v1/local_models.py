"""The AI models page's API (docs/local-models.md; design L1 section 6).

Reading is for anyone signed in: the page shows everything to everybody and
gives only an administrator the buttons (`can_manage`). Every change is an
administrator's, leaves an audit row, and is committed once with it. Every
refusal is a sentence the page shows as it is, in the one error envelope.

On an install that cannot run models (the containers) the overview answers
`mode: "unavailable"` with the sentence and asks nothing else, and every
change is refused with that sentence (409): nothing is queued that nothing
would ever do.

What reaches outside FrameFound — Hugging Face, the model server, the
runtimes on disk, the clock — comes in through `get_deps`, so one
`dependency_overrides` entry swaps every fake; a later phase's router takes
the same dependency (13.5). Work that blocks (the hub, a share, `state.json`,
the model server) runs in a thread. A share is asked under the ten-second
rule `api/v1/storage.py` follows, and a question still unanswered is not
asked a second time: a share that has hung would otherwise take a thread
every time the page polls.

Server paths are an administrator's to see. The library's folder, a
runtime's damaged record (which names its file) and the last lines a program
printed are left out of what anybody else is sent.
"""

import asyncio
import functools
import os
import shutil
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.auth import service as auth_service
from framefound.auth.crypto import SecretUnavailable
from framefound.auth.deps import CurrentUser, DbDep, client_ip, require_admin
from framefound.config import Settings
from framefound.db.models import Asset, LocalAiJob, LocalAiRole, LocalModel, User, catalogued
from framefound.local_models import (
    ROLE_TITLES,
    ROLES,
    UNAVAILABLE,
    availability,
    demo,
    gate,
    handlers,
    jobs,
    kinds,
    library,
    policy,
    preflight,
    registry,
    runtimes,
    schedule,
)
from framefound.local_models.ask import (
    NO_SUCH_MODEL,
    RECORD_DAMAGED,
    ModelServer,
    ServerRefused,
    ServerUnreachable,
    loaded_model,
)
from framefound.local_models.demo import DEMO_NOTE
from framefound.local_models.deps import Deps, deps_for
from framefound.local_models.hub import HubError
from framefound.local_models.kinds import RuntimeKind
from framefound.local_models.settings_store import (
    LocalModelsConfig,
    load_config,
    load_latest,
    save_config,
    save_latest,
)

# The lane's task module, at import: it brings the queue's client for `_wake`,
# and every kind of job registered at its foot (13.2) — a later phase's kinds
# live in other packages, and `jobs.create`, `title_of` and Cancel's tidy-up
# must know them from the API's first request.
from framefound.processing import local_ai_tasks
from framefound.storage import stores

log = structlog.get_logger()
router = APIRouter(prefix="/local-models", tags=["local-models"])

_GB = 1024**3

ROLE_DESCRIPTIONS = {
    "looker": "Looks at pictures and shots: suggests tags, writes a caption, gives a score.",
    "editor": "Reads a brief and the list of shots and drafts a rough cut.",
}

NO_SUCH_JOB = "That job is no longer on the list."
QUEUE_DOWN = "The processing queue is unavailable. Nothing was started."
ALREADY_DOWNLOADING = "It is already downloading."
ALREADY_HERE = "It is already installed."
BEING_DOWNLOADED = "It is being downloaded. Cancel the download first."
BEING_USED = "It is being tested right now. Remove it when the test has finished."
CANNOT_REMOVE = (
    "The model library is not connected, so its files cannot be removed. Reconnect the drive "
    "or share and try again."
)
NOT_A_BUILD = "That is not a build FrameFound showed. Check the model again."
NOT_ON_THIS_MAC = "Not on this Mac."
LIBRARY_SILENT = (
    "The model library did not answer within 10 seconds. Check that the drive or share is "
    "connected."
)
# The design's sentences for the settings (6.2).
WINDOW_NEVER_OPENS = "The window cannot start and end at the same time."
BAD_BUDGET = "Choose a budget between 1 and 512 GB."
NIGHT_SMALLER = "The overnight budget cannot be smaller than the daytime one."
BAD_IDLE = "Choose between 1 and 120 minutes."
NOT_YES_OR_NO = "Say yes or no to pausing."
TOKEN_SPACES = "That does not look like a token (it has spaces in it)."  # noqa: S105 - a sentence about a token, not one
TOKEN_TOO_LONG = "That does not look like a token (it is longer than 300 characters)."  # noqa: S105 - a sentence about a token, not one
TOKEN_ODD = "That does not look like a token (it has characters a token never has)."  # noqa: S105 - a sentence about a token, not one
DOWNLOAD_ANYWAY = "Download it anyway?"
# Roles, Test, the software that runs models, the model server (6.9–6.13).
NO_SUCH_ROLE = "There is no such role. The roles are the Looker and the Editor."
ROLE_TAKEN = "That role is already being given to another model. Cancel that first."
ALREADY_COPYING = "It is already being copied to this machine for that role."
TEST_WAITING = "A test of this model is already waiting."
INSTALL_FIRST = "Install {label} first (Software that runs models, below)."
NO_SUCH_RUNTIME = "FrameFound has no software of that name that runs models here."
INDEX_UNREACHABLE = kinds.INDEX_UNREACHABLE  # MLX-VLM's; llama.cpp's is its own
BEING_INSTALLED = "{label} is already being installed."
IN_USE_ALREADY = "{version} is already the version in use."
WAIT_FOR_INSTALL = "{label} is being installed. Go back to an earlier version once it has finished."
SERVER_NOT_ANSWERING = "The model server is not answering."
SERVER_BUSY = (
    "The model server is busy answering a question. It lets go of the model as soon as that "
    "answer is done."
)
SAMPLE_CACHE = "private, max-age=86400"

MOST_BUDGET_GB = 512
# What macOS and everything else on the Mac are left, at the least.
LEFT_FOR_MACOS_GB = 4
MOST_IDLE_MINUTES = 120
MOST_TOKEN = 300
# How long a share is waited for (the storage page's rule), how long the model
# server, and how long a remembered answer from it stays good (5.2).
LOOK_TIMEOUT_S = 10.0
STATUS_WAIT_S = 1.0
STATUS_KEEP_S = 2.0
# Asking the model server to unload after a pause or a lower budget is best
# effort: the lane's sweep does it within a minute anyway.
UNLOAD_WAIT_S = 2.0
REMOVE_UNLOAD_WAIT_S = 10.0
RECENT_JOBS = 15


# --- the outside world ----------------------------------------------------------------


def get_deps() -> Deps:
    """The outside world for this request (`local_models.deps`): the demo's stand-ins
    in demo mode. Tests, and a later phase's tests, override this one dependency
    to hand in fakes."""
    return deps_for()


DepsDep = Annotated[Deps, Depends(get_deps)]


def _can_change(deps: DepsDep) -> Deps:
    """Every change is refused where models cannot run, with the sentence that says why."""
    found = availability(deps.settings)
    if not found.ok:
        raise HTTPException(409, found.reason or UNAVAILABLE)
    return deps


Changing = Annotated[Deps, Depends(_can_change)]


# --- what the page is sent ------------------------------------------------------------


class _Out(BaseModel):
    # Several fields are called model_…, which is what they are.
    model_config = ConfigDict(protected_namespaces=())


class Machine(_Out):
    system: str
    machine: str
    memory_gb: float | None


class Now(_Out):
    local_time: str  # the Mac's own wall clock, no zone: "2026-11-12T14:05:00"
    overnight: bool
    budget_gb: float
    changes_at: str | None  # the same clock; None when there is no overnight window


class SettingsOut(_Out):
    day_budget_gb: float
    night_budget_gb: float
    night_start: str
    night_end: str
    idle_unload_minutes: int
    paused: bool
    paused_at: str | None
    paused_by: str | None
    hf_token_configured: bool
    hf_token_problem: str | None


class BlockedName(_Out):
    name: str
    built_in: bool


class PublishersOut(_Out):
    makers: list[str]
    converters: list[str]
    blocked: list[BlockedName]


class ServerOut(_Out):
    state: Literal["down", "idle", "loading", "ready", "busy"]
    model_id: uuid.UUID | None = None
    model_name: str | None = None
    runtime: str | None = None
    runtime_version: str | None = None
    memory_gb: float | None = None
    loaded_for_s: int | None = None
    unloads_in_s: int | None = None
    problem: str | None = None


class LibraryOut(_Out):
    path: str | None  # an administrator's only
    where: Literal["local", "bulk", "custom"]
    connected: bool
    problem: str | None
    free_gb: float | None
    copies_to_this_machine: bool


class JobOut(_Out):
    id: uuid.UUID
    kind: str
    title: str
    subject: str
    model_id: uuid.UUID | None
    state: str
    message: str
    detail: str | None  # an administrator's only
    not_before: datetime | None
    phase: str
    done: int
    total: int
    cancelling: bool
    can_cancel: bool
    requested_by: str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class SampleOut(_Out):
    asset_id: uuid.UUID | None
    media_type: str | None
    filename: str | None
    label: str


class ModelRef(_Out):
    repo_id: str
    revision_short: str


class TestOut(_Out):
    job_id: uuid.UUID
    state: str
    message: str
    detail: str | None  # an administrator's only
    not_before: datetime | None
    asked_at: datetime | None
    finished_at: datetime | None
    # Filled in once the test has an answer (or an answer in prose); null before.
    kind: Literal["picture", "text"] | None = None
    sample: SampleOut | None = None
    answer: dict[str, Any] | None = None
    raw: str | None = None
    structured_ok: bool = False
    seconds: float | None = None
    load_seconds: float | None = None
    memory_gb: float | None = None
    memory_source: str | None = None
    ran_from: str | None = None
    attempts: int | None = None
    finish_reason: str | None = None
    model: ModelRef | None = None
    runtime: str | None = None
    runtime_version: str | None = None
    prompt_version: str | None = None
    demo: bool = False


class BuiltFrom(_Out):
    repo_id: str
    publisher: str | None
    publisher_is: str  # maker | converter | blocked | unknown
    found: bool  # False: Hugging Face has no such model now


class ModelOut(_Out):
    id: uuid.UUID
    repo_id: str
    name: str
    revision: str
    revision_short: str
    variant: str
    # The build's name for the page ("Q4_K_M"); "" for a repository of one build.
    variant_label: str
    build_date: datetime | None
    url: str
    publisher: str
    publisher_is: str
    maker: str | None
    built_from: list[BuiltFrom]
    approved: bool
    approval_problem: str | None
    licence: str
    licence_name: str
    licence_url: str | None
    gated: bool
    size_bytes: int
    memory_gb: float
    memory_measured: bool
    fits: Literal["day", "night", "never"]
    takes: list[str]
    runtime: str
    runtime_label: str
    runtime_known: bool
    # Its program asks once before a role is given to a model nobody compared.
    needs_compare: bool
    # Whether there is something to show how it answers, judged for the Looker
    # (for a model that does not read pictures, for the Editor): a finished
    # Compare, or for the Editor a text Test answered in the form asked. Always
    # true when its program does not ask (gate.Evidence.seen).
    compared: bool
    compared_at: datetime | None
    compared_by: Literal["compare", "test"] | None
    runtime_installed: bool
    runtime_problem: str | None
    state: str
    error: str | None
    in_library: bool | None
    on_this_machine: bool | None
    roles: list[str]
    pending_roles: list[str]
    role_problems: dict[str, str | None]
    last_loaded_with: str | None
    job: JobOut | None
    test: TestOut | None
    demo: bool
    added_by: str | None
    added_at: datetime | None


class RoleOut(_Out):
    role: str
    title: str
    description: str
    model_id: uuid.UUID | None
    model_name: str | None
    ready: bool
    problem: str | None
    pending_model_id: uuid.UUID | None
    pending_model_name: str | None
    job: JobOut | None
    assigned_at: datetime | None
    # The role's model has been seen answering for this role (gate); null when
    # its program does not ask, or no model holds the role. False draws the
    # card's "not compared yet".
    compared: bool | None


class RuntimeOut(_Out):
    name: str
    label: str
    summary: str
    demo: bool
    supported: bool
    unsupported_reason: str | None
    installed: str | None
    previous: str | None
    installed_at: str | None
    versions: list[str]
    about: str
    # `about` for the page's "Installed" line: None when there is nothing more to say.
    installed_detail: str | None
    update_note: str
    # The placeholder of "Install a particular version".
    version_hint: str
    # How many models added run on it, whatever their state (downloading, incomplete
    # and dropped ones included): an empty row is then not read as a fault.
    models: int
    latest: str | None
    latest_checked_at: str | None
    latest_problem: str | None
    update_available: bool
    problem: str | None
    job: JobOut | None


class Overview(_Out):
    mode: Literal["ready", "demo", "unavailable"]
    note: str | None
    can_manage: bool
    machine: Machine
    now: Now
    settings: SettingsOut
    publishers: PublishersOut
    server: ServerOut
    library: LibraryOut | None  # None where models cannot run: nothing is asked
    roles: list[RoleOut]
    models: list[ModelOut]
    runtimes: list[RuntimeOut]
    jobs: list[JobOut]


class SettingsSaved(_Out):
    settings: SettingsOut
    now: Now


class NoLongerApproved(_Out):
    id: uuid.UUID
    repo_id: str
    reason: str
    roles: list[str]


class PublishersSaved(_Out):
    publishers: PublishersOut
    no_longer_approved: list[NoLongerApproved]


class TokenSaved(_Out):
    hf_token_configured: bool


class CheckOut(_Out):
    key: str
    label: str
    tone: Literal["ok", "warn", "bad"]
    value: str
    detail: str | None


class PreflightRuntime(_Out):
    name: str
    label: str
    installed: bool


class VariantFileOut(_Out):
    path: str
    size: int
    part: Literal["model", "pictures", "text"]


class VariantOut(_Out):
    id: str
    label: str
    quant: str
    bits: int | None  # bits per weight its name says; None when it says none
    size_bytes: int
    memory_gb: float
    fits: Literal["day", "night", "never"]
    recommended: bool  # exactly one build of the list
    takes: list[str]
    note: str
    files: list[VariantFileOut]


class ExistingOut(_Out):
    model_id: uuid.UUID
    revision_short: str
    variant: str
    same_build: bool


class PreflightOut(_Out):
    repo_id: str
    revision: str
    revision_short: str
    build_date: datetime | None
    ok: bool
    verdict: str
    checks: list[CheckOut]
    problems: list[str]
    warnings: list[str]
    notes: list[str]
    access: Literal["open", "ok", "needs_token", "no_access", "untested"]
    size_bytes: int
    memory_gb: float
    fits: Literal["day", "night", "never"] | None  # None: no build could be sized
    takes: list[str]
    runtime: PreflightRuntime | None
    variants: list[VariantOut]
    variant: str | None
    existing: ExistingOut | None


class Removed(_Out):
    freed_bytes: int


class Unloaded(_Out):
    unloaded: bool


# --- what the page sends --------------------------------------------------------------


class SettingsBody(BaseModel):
    # Any, and checked here, so a wrong value is refused with a sentence
    # rather than a validation dump. Absent (or null) leaves a setting alone.
    day_budget_gb: Any = None
    night_budget_gb: Any = None
    night_start: Any = None
    night_end: Any = None
    idle_unload_minutes: Any = None
    paused: Any = None


class PublishersBody(BaseModel):
    # Each the whole list as it should be; `blocked_added` is the operator's
    # additions only (the built-in names are always in force).
    makers: list[str] | None = None
    converters: list[str] | None = None
    blocked_added: list[str] | None = None


class TokenBody(BaseModel):
    token: str  # "" removes the stored one


class PreflightBody(BaseModel):
    name: str
    variant: str | None = None


class AddBody(BaseModel):
    name: str
    revision: str  # the 40-hex build the check showed
    variant: str | None = None
    accept_warnings: bool = False


class RoleBody(BaseModel):
    # A string, read here: anything that is not a model's id is "no such model",
    # in words, rather than a validation dump. null takes the role away.
    model_id: str | None = None
    # Yes to the Compare question (428): give it the role without a comparison.
    skip_compare: bool = False


class TestBody(BaseModel):
    asset_id: str | None = None  # null: the newest photograph, else a picture FrameFound draws


class InstallBody(BaseModel):
    version: str | None = None  # null (or blank): the newest, looked up now


# --- small things ---------------------------------------------------------------------


def _utc(moment: datetime | None) -> datetime | None:
    """A moment read from the table, in UTC: SQLite hands back what was written
    without its zone, and everything local AI writes is UTC."""
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _name(repo_id: str) -> str:
    return repo_id.rpartition("/")[2] or repo_id


def subject_of(model: LocalModel) -> str:
    """A download's or a copy's subject: `publisher/name@<8 of the build>[/variant]`.
    Unique per build, and `handlers._name` reads the model's name out of it."""
    variant = f"/{model.variant}" if model.variant else ""
    return f"{model.repo_id}@{model.revision[:8]}{variant}"


def _physical_memory_gb() -> float | None:
    """This machine's memory, where the system says (not on Windows)."""
    sysconf: Callable[[str], int] | None = getattr(os, "sysconf", None)
    if sysconf is None:
        return None
    try:
        pages, size = sysconf("SC_PHYS_PAGES"), sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return None
    if pages <= 0 or size <= 0:
        return None
    return round(pages * size / _GB, 1)


def _gb(size: int | None) -> float | None:
    return None if size is None else round(size / _GB, 1)


def _strings(value: object) -> list[str]:
    """A stored list as the page should see it: its strings, nothing else."""
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, str)]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass(frozen=True)
class _Asking:
    """A question under way in a thread, and when it was asked."""

    answer: "asyncio.Future[Any]"
    since: float  # time.monotonic()


_LOOKING: dict[str, _Asking] = {}


async def _bounded[T](key: str, work: Callable[[], T], wait_s: float, fallback: T) -> T:
    """`work()` in a thread, not waited for past `wait_s` (then `fallback`).

    One question per key at a time: a share that has hung holds its thread
    until the system gives up, and the page asks every few seconds. While an
    earlier `work` under the same key is unanswered, a second caller waits for
    that same answer, for what is left of `wait_s` since it was asked — so two
    tabs looking at once both get the answer, and `fallback` means the
    question really went unanswered that long. A key names one question:
    every `work` given under it asks the same thing."""
    loop = asyncio.get_running_loop()
    running = _LOOKING.get(key)
    if running is not None and not running.answer.done() and running.answer.get_loop() is loop:
        asking: asyncio.Future[Any] = running.answer
        left = running.since + wait_s - time.monotonic()
        if left <= 0:
            return fallback
    else:
        asking = asyncio.ensure_future(asyncio.to_thread(work))
        # Looked at even when nobody waits for it any more.
        asking.add_done_callback(lambda done: done.cancelled() or done.exception())
        _LOOKING[key] = _Asking(asking, time.monotonic())
        left = wait_s
    try:
        found: T = await asyncio.wait_for(asyncio.shield(asking), left)
    except TimeoutError:
        return fallback
    return found


async def _wake(settings: Settings) -> bool:
    """Wake the lane; False when the queue is down."""
    return await asyncio.to_thread(local_ai_tasks.wake, settings)


# There is one model server per install, however many `ModelServer` objects the
# requests are handed (each request builds its own): what it said, and the
# question under way to it, are kept under these keys, not under an object.
_STATUS_KEY = "model-server"
_UNLOAD_KEY = "model-server-unload"


def _unload_work(server: ModelServer) -> Callable[[], object]:
    """Ask the model server to unload: True, False (nothing was loaded), None (not
    answering) or its refusal. One question for every caller under _UNLOAD_KEY."""

    def work() -> object:
        try:
            return server.unload()
        except ServerUnreachable:
            return None
        except ServerRefused as err:
            return err

    return work


async def _unload(server: ModelServer, wait_s: float = UNLOAD_WAIT_S) -> None:
    """Ask the model server to let go of what it has loaded. Best effort."""
    fallback: object = False
    await _bounded(_UNLOAD_KEY, _unload_work(server), wait_s, fallback)
    _STATUS.pop(_STATUS_KEY, None)


# --- the model server, as the page sees it --------------------------------------------

_STATUS: dict[str, tuple[float, dict[str, Any] | None]] = {}


async def _server_status(server: ModelServer) -> dict[str, Any] | None:
    """What the model server says is loaded: asked for one second, remembered for two."""
    kept = _STATUS.get(_STATUS_KEY)
    if kept is not None and time.monotonic() - kept[0] < STATUS_KEEP_S:
        return kept[1]

    def ask() -> dict[str, Any] | None:
        try:
            found = server.status()
        except Exception:  # whatever went wrong, it is not answering
            return None
        return found if isinstance(found, dict) else None

    found = await _bounded(_STATUS_KEY, ask, STATUS_WAIT_S, None)
    _STATUS[_STATUS_KEY] = (time.monotonic(), found)
    return found


_SERVER_STATES = ("idle", "loading", "ready", "busy")


async def _server_out(db: AsyncSession, server: ModelServer) -> ServerOut:
    status = await _server_status(server)
    if status is None:
        return ServerOut(state="down")
    state = status.get("state")
    model = await loaded_model(db, status) if status.get("repo_id") else None
    repo_id = _text(status.get("repo_id"))
    seconds = _number(status.get("loaded_for_s"))
    unloads = _number(status.get("unloads_in_s"))
    return ServerOut(
        state=state if state in _SERVER_STATES else "idle",
        model_id=None if model is None else model.id,
        model_name=None if repo_id is None else _name(repo_id),
        runtime=_text(status.get("runtime")),
        runtime_version=_text(status.get("runtime_version")),
        memory_gb=_number(status.get("memory_gb")),
        loaded_for_s=None if seconds is None else round(seconds),
        unloads_in_s=None if unloads is None else round(unloads),
        problem=_text(status.get("problem")),
    )


# --- the library and the runtimes, as the page sees them ------------------------------


@dataclass(frozen=True)
class _Where:
    folder: str | None
    where: Literal["local", "bulk", "custom"]
    copies: bool


def _where(settings: Settings) -> _Where:
    """Where the library is: read from the settings and this machine's stores file."""
    root = stores.kind_root(library.KIND, settings)
    bulk = stores.bulk_root(settings)
    where: Literal["local", "bulk", "custom"]
    if root is None or root == stores.default_root(library.KIND, settings):
        where = "local"
    elif bulk is not None and root == bulk / library.KIND:
        where = "bulk"
    else:
        where = "custom"
    try:
        folder: str | None = str(library.library_root(settings))
    except ValueError:
        folder = None
    return _Where(folder, where, library.copies_to_this_machine(settings))


@dataclass(frozen=True)
class _Looked:
    """What the library said, asked once per request on a thread."""

    answered: bool
    connected: bool
    problem_for_admin: str | None  # with paths
    problem: str | None  # without
    free: int | None
    presence: Mapping[uuid.UUID, library.Presence]


_SILENT = _Looked(False, False, LIBRARY_SILENT, LIBRARY_SILENT, None, {})


def _look(settings: Settings, models: Sequence[library.ModelFiles]) -> _Looked:
    connected = stores.kind_problem(library.KIND, settings) is None
    return _Looked(
        answered=True,
        connected=connected,
        problem_for_admin=library.write_problem(settings),
        problem=library.away(settings),
        free=library.free_bytes(settings),
        presence={model.id: library.presence(model, settings) for model in models},
    )


async def _look_at_library(settings: Settings, models: Sequence[library.ModelFiles]) -> _Looked:
    # Keyed by the models looked at: a request that has just added one must
    # not be handed a look that left it out.
    key = "library-" + ",".join(sorted(str(model.id) for model in models))
    return await _bounded(key, lambda: _look(settings, models), LOOK_TIMEOUT_S, _SILENT)


@dataclass(frozen=True)
class _OnDisk:
    installed: runtimes.Installed | None
    problem: str | None  # the damaged record's sentence (it names its file)
    about: str


def _runtimes_on_disk(deps: Deps) -> dict[str, _OnDisk]:
    """What each kind has on disk (state.json and the version folders: this machine's own)."""
    try:
        root = runtimes.root(deps.settings)
    except runtimes.RuntimeProblem:
        return {}
    found: dict[str, _OnDisk] = {}
    for name, kind in deps.kinds.items():
        try:
            described = runtimes.describe(root, name)
        except runtimes.RuntimeProblem as err:
            found[name] = _OnDisk(None, str(err), "")
            continue
        about = ""
        if described.current is not None:
            try:
                about = kind.about(runtimes.version_dir(root, name, described.current))
            except (OSError, ValueError):
                about = ""
        found[name] = _OnDisk(described, None, about)
    return found


def _installed_versions(deps: Deps) -> dict[str, str | None]:
    return {
        name: None if seen.installed is None else seen.installed.current
        for name, seen in _runtimes_on_disk(deps).items()
    }


# --- assembling the answers -----------------------------------------------------------


@dataclass
class _Seen:
    """Everything about this install the model rows are drawn from, read once."""

    deps: Deps
    admin: bool
    config: LocalModelsConfig
    lists: policy.PublisherLists
    budgets: schedule.Budgets
    disk: dict[str, _OnDisk]
    looked: _Looked
    roles: dict[str, LocalAiRole]
    held: dict[uuid.UUID, list[str]]
    active: list[LocalAiJob]
    tests: dict[uuid.UUID, LocalAiJob]
    emails: dict[uuid.UUID, str]
    now: datetime  # the Mac's wall clock, naive
    # How many models added run on each runtime, by kind name, whatever their
    # state (registry.models_per_runtime).
    on_runtime: dict[str, int]
    # What is known of how each model answers, per role (the Compare question).
    evidence: dict[uuid.UUID, dict[str, gate.Evidence]]

    def runtime_problem(self, name: str) -> str | None:
        kind = self.deps.kinds.get(name)
        if kind is None:
            return registry.NO_PROGRAM_HERE
        seen = self.disk.get(name)
        if seen is not None and seen.problem is not None:
            return seen.problem if self.admin else RECORD_DAMAGED
        if seen is None or seen.installed is None or seen.installed.current is None:
            return f"{kind.label} is not installed."
        return None

    def runtime_installed(self, name: str) -> bool:
        seen = self.disk.get(name)
        return (
            seen is not None and seen.installed is not None and seen.installed.current is not None
        )


async def _seen(
    db: AsyncSession,
    deps: Deps,
    user: User,
    models: Sequence[LocalModel],
    extra_users: Sequence[uuid.UUID | None] = (),
) -> _Seen:
    config = await load_config(db)
    snapshots: list[library.ModelFiles] = []
    for model in models:
        try:
            snapshots.append(registry.snapshot(model))
        except ValueError:
            continue  # a row whose files cannot be read is not looked for
    looked = await _look_at_library(deps.settings, snapshots)
    disk = await asyncio.to_thread(_runtimes_on_disk, deps)
    active = list(
        (
            await db.scalars(
                select(LocalAiJob)
                .where(LocalAiJob.state.in_(jobs.ACTIVE))
                .order_by(LocalAiJob.priority, LocalAiJob.created_at)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    tests = await _newest_tests(db, [model.id for model in models])
    wanted: set[uuid.UUID] = {model.added_by for model in models if model.added_by}
    wanted |= {job.requested_by for job in active if job.requested_by}
    wanted |= {job.requested_by for job in tests.values() if job.requested_by}
    wanted |= {person for person in extra_users if person}
    return _Seen(
        deps=deps,
        admin=user.role == "admin",
        config=config,
        lists=config.lists(),
        budgets=config.budgets(),
        disk=disk,
        looked=looked,
        roles=await registry.roles(db),
        held=await registry.holders(db),
        active=active,
        tests=tests,
        emails=await _emails(db, wanted),
        now=deps.now(),
        on_runtime=await registry.models_per_runtime(db),
        evidence={model.id: await gate.for_roles(db, model, deps.kinds) for model in models},
    )


async def _newest_tests(
    db: AsyncSession, model_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, LocalAiJob]:
    if not model_ids:
        return {}
    newest = (
        select(LocalAiJob.model_id, func.max(LocalAiJob.created_at).label("at"))
        .where(LocalAiJob.kind == "test", LocalAiJob.model_id.in_(model_ids))
        .group_by(LocalAiJob.model_id)
        .subquery()
    )
    rows = await db.scalars(
        select(LocalAiJob)
        .join(
            newest,
            (LocalAiJob.model_id == newest.c.model_id) & (LocalAiJob.created_at == newest.c.at),
        )
        .where(LocalAiJob.kind == "test")
        .execution_options(populate_existing=True)
    )
    found: dict[uuid.UUID, LocalAiJob] = {}
    for job in rows:
        if job.model_id is not None:
            found.setdefault(job.model_id, job)
    return found


async def _emails(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await db.execute(select(User.id, User.email).where(User.id.in_(ids)))
    return {row.id: row.email for row in rows}


def _job_out(
    job: LocalAiJob, *, admin: bool, now: datetime, emails: Mapping[uuid.UUID, str]
) -> JobOut:
    state, message = jobs.shown(job, now)
    cancelling = bool(job.cancel_requested) and state == "running"
    return JobOut(
        id=job.id,
        kind=job.kind,
        title=jobs.title_of(job),
        subject=job.subject,
        model_id=job.model_id,
        state=state,
        message=message,
        detail=job.detail if admin else None,
        not_before=_utc(job.not_before),
        phase=job.phase,
        done=job.done,
        total=job.total,
        cancelling=cancelling,
        can_cancel=state in jobs.ACTIVE and not cancelling,
        requested_by=None if job.requested_by is None else emails.get(job.requested_by),
        created_at=_utc(job.created_at),
        started_at=_utc(job.started_at),
        finished_at=_utc(job.finished_at),
    )


def shown_test(job: LocalAiJob, *, admin: bool, now: datetime) -> TestOut:
    """A `test` job as the Test panel shows it (6.10): the row's state, and once
    it has finished, what the handler kept in `result`."""
    state, message = jobs.shown(job, now)
    result: Mapping[str, Any] = job.result if isinstance(job.result, dict) else {}
    sample = result.get("sample")
    sample_out: SampleOut | None = None
    if isinstance(sample, dict) and isinstance(sample.get("label"), str):
        asset = _text(sample.get("asset_id"))
        try:
            asset_id = None if asset is None else uuid.UUID(asset)
        except ValueError:
            asset_id = None
        sample_out = SampleOut(
            asset_id=asset_id,
            media_type=_text(sample.get("media_type")),
            filename=_text(sample.get("filename")),
            label=sample["label"],
        )
    asked = result.get("model")
    model: ModelRef | None = None
    if isinstance(asked, dict) and isinstance(asked.get("repo_id"), str):
        model = ModelRef(
            repo_id=asked["repo_id"], revision_short=str(asked.get("revision_short", ""))
        )
    elif job.model and job.model_revision:
        model = ModelRef(repo_id=job.model, revision_short=job.model_revision[:7])
    kind = result.get("kind")
    answer = result.get("answer")
    attempts = result.get("attempts")
    return TestOut(
        job_id=job.id,
        state=state,
        message=message,
        detail=job.detail if admin else None,
        not_before=_utc(job.not_before),
        asked_at=_utc(job.created_at),
        finished_at=_utc(job.finished_at),
        kind=kind if kind in ("picture", "text") else None,
        sample=sample_out,
        answer=answer if isinstance(answer, dict) else None,
        raw=_text(result.get("raw")),
        structured_ok=result.get("structured_ok") is True,
        seconds=_number(result.get("seconds")),
        load_seconds=_number(result.get("load_seconds")),
        memory_gb=_number(result.get("memory_gb")),
        memory_source=_text(result.get("memory_source")),
        ran_from=_text(result.get("ran_from")),
        attempts=attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else None,
        finish_reason=_text(result.get("finish_reason")),
        model=model,
        runtime=_text(result.get("runtime")) or job.runtime,
        runtime_version=_text(result.get("runtime_version")) or job.runtime_version,
        prompt_version=_text(result.get("prompt_version")) or job.prompt_version,
        demo=result.get("demo") is True,
    )


def _models_job(seen: _Seen, model_id: uuid.UUID) -> LocalAiJob | None:
    """The download or copy under way for a model (a download first)."""
    mine = [job for job in seen.active if job.model_id == model_id]
    return next(
        (job for job in mine if job.kind == "download"),
        next((job for job in mine if job.kind == "place"), None),
    )


def _model_out(model: LocalModel, seen: _Seen) -> ModelOut:
    lists, budgets = seen.lists, seen.budgets
    chain = registry.nodes(model)
    verdict = registry.judged(model, lists)
    publisher_is = lists.status(model.publisher)
    maker = (
        model.publisher
        if publisher_is == "maker"
        else next(
            (node.publisher for node in chain[1:] if lists.status(node.publisher) == "maker"),
            None,
        )
    )
    memory = registry.memory_gb(model)
    measured = model.measured_memory_gb
    presence = seen.looked.presence.get(model.id)
    held = [role for role in ROLES if seen.roles[role].model_id == model.id]
    pending = [role for role in ROLES if seen.roles[role].pending_model_id == model.id]
    job = _models_job(seen, model.id)
    test = seen.tests.get(model.id)
    kind = seen.deps.kinds.get(model.runtime)
    takes = _strings(model.takes)
    known = seen.evidence.get(model.id, {}).get(gate.shown_role(model), gate.NOT_NEEDED)
    return ModelOut(
        id=model.id,
        repo_id=model.repo_id,
        name=_name(model.repo_id),
        revision=model.revision,
        revision_short=model.revision[:7],
        variant=model.variant or "",
        variant_label=model.variant or "",
        build_date=_utc(model.hub_modified_at),
        url=f"https://huggingface.co/{model.repo_id}/tree/{model.revision}",
        publisher=model.publisher,
        publisher_is=publisher_is,
        maker=maker,
        built_from=[
            BuiltFrom(
                repo_id=node.repo_id,
                publisher=node.publisher,
                publisher_is=lists.status(node.publisher),
                found=node.found,
            )
            for node in chain[1:]
        ],
        approved=verdict.allowed,
        approval_problem=None if verdict.allowed else verdict.reason,
        licence=model.licence,
        licence_name=model.licence_name,
        licence_url=model.licence_url,
        gated=bool(model.gated),
        size_bytes=model.size_bytes,
        memory_gb=memory,
        memory_measured=measured is not None and measured >= model.memory_gb,
        fits=schedule.fits(memory, budgets),
        takes=takes,
        runtime=model.runtime,
        runtime_label=model.runtime if kind is None else kind.label,
        runtime_known=kind is not None,
        needs_compare=kind is not None and kind.needs_compare,
        compared=known.seen,
        compared_at=known.at,
        compared_by=known.by or None,
        runtime_installed=seen.runtime_installed(model.runtime),
        runtime_problem=seen.runtime_problem(model.runtime),
        state=model.state,
        error=model.error,
        in_library=None if presence is None else presence.in_library,
        on_this_machine=None if presence is None else presence.on_this_machine,
        roles=held,
        pending_roles=pending,
        role_problems={
            role: registry.role_problem(model, role, lists, budgets, seen.deps.kinds)
            for role in ROLES
        },
        last_loaded_with=model.last_loaded_with,
        job=None
        if job is None
        else _job_out(job, admin=seen.admin, now=seen.now, emails=seen.emails),
        test=None if test is None else shown_test(test, admin=seen.admin, now=seen.now),
        demo=kinds.is_demo_runtime(model.runtime),
        added_by=None if model.added_by is None else seen.emails.get(model.added_by),
        added_at=_utc(model.created_at),
    )


def _role_out(role: str, models: Mapping[uuid.UUID, LocalModel], seen: _Seen) -> RoleOut:
    row = seen.roles[role]
    model = None if row.model_id is None else models.get(row.model_id)
    pending = None if row.pending_model_id is None else models.get(row.pending_model_id)
    ready, problem = False, None
    compared: bool | None = None
    if model is not None:
        ready, problem = _role_state(model, seen)
        known = seen.evidence.get(model.id, {}).get(role)
        compared = known.seen if known is not None and known.needed else None
    copying = next(
        (job for job in seen.active if job.kind == "place" and _asked_role(job) == role),
        None,
    )
    return RoleOut(
        role=role,
        title=ROLE_TITLES[role],
        description=ROLE_DESCRIPTIONS[role],
        model_id=None if model is None else model.id,
        model_name=None if model is None else _name(model.repo_id),
        ready=ready,
        problem=problem,
        pending_model_id=row.pending_model_id,
        pending_model_name=None if pending is None else _name(pending.repo_id),
        job=None
        if copying is None
        else _job_out(copying, admin=seen.admin, now=seen.now, emails=seen.emails),
        assigned_at=_utc(row.assigned_at),
        compared=compared,
    )


def _asked_role(job: LocalAiJob) -> str | None:
    asked = job.payload.get("role") if isinstance(job.payload, dict) else None
    return str(asked or job.role or "") or None


def _role_state(model: LocalModel, seen: _Seen) -> tuple[bool, str | None]:
    """Whether a role's model is on this machine and may run, and if not, why.
    "Only runs overnight" is information: the model is still ready."""
    if model.state == registry.DOWNLOADING:
        return False, registry.STILL_DOWNLOADING
    if model.state != registry.INSTALLED:
        return False, registry.NOT_COMPLETE
    verdict = registry.judged(model, seen.lists)
    if not verdict.allowed:
        return False, f"No longer approved: {verdict.reason}"
    if model.runtime not in seen.deps.kinds:
        return False, registry.NO_PROGRAM_HERE
    needed = registry.memory_gb(model)
    fits = schedule.fits(needed, seen.budgets)
    if fits == "never":
        return False, schedule.decide(needed, seen.now, seen.budgets).message
    presence = seen.looked.presence.get(model.id)
    if presence is None:
        return False, None  # nobody could look: say nothing that may be untrue
    if not presence.on_this_machine:
        return False, NOT_ON_THIS_MAC
    missing = seen.runtime_problem(model.runtime)
    if missing is not None:
        return False, missing
    if fits == "night":
        start = schedule.clock_text(seen.budgets.night_start)
        end = schedule.clock_text(seen.budgets.night_end)
        return True, f"Only runs overnight ({start}–{end})."
    return True, None


def _runtime_out(
    name: str, latest: Mapping[str, Any], seen: _Seen, *, system: str, machine: str
) -> RuntimeOut:
    kind = seen.deps.kinds[name]
    on_disk = seen.disk.get(name)
    installed = None if on_disk is None else on_disk.installed
    current = None if installed is None else installed.current
    newest = _text(latest.get("version"))
    why_not = kind.supported_here(system, machine)
    problem = None if on_disk is None else on_disk.problem
    installing = next(
        (
            job
            for job in seen.active
            if job.kind == "runtime_install"
            and isinstance(job.payload, dict)
            and job.payload.get("name") == name
        ),
        None,
    )
    return RuntimeOut(
        name=name,
        label=kind.label,
        summary=kind.summary,
        demo=kind.demo,
        supported=why_not is None,
        unsupported_reason=why_not,
        installed=current,
        previous=None if installed is None else installed.previous,
        installed_at=None if installed is None else installed.installed_at,
        versions=[] if installed is None else list(installed.versions),
        about="" if on_disk is None else on_disk.about,
        installed_detail=(on_disk.about or None) if on_disk is not None and current else None,
        update_note=kind.update_note,
        version_hint=kind.version_hint,
        models=seen.on_runtime.get(name, 0),
        latest=newest,
        latest_checked_at=_text(latest.get("checked_at")),
        latest_problem=_text(latest.get("problem")),
        # The kind's own comparison: llama.cpp's versions are not PyPI's (13.4).
        update_available=bool(newest and current and kind.is_newer(newest, current)),
        problem=None if problem is None else (problem if seen.admin else RECORD_DAMAGED),
        job=None
        if installing is None
        else _job_out(installing, admin=seen.admin, now=seen.now, emails=seen.emails),
    )


def _settings_out(config: LocalModelsConfig) -> SettingsOut:
    problem = None
    try:
        config.hf_token()
    except SecretUnavailable:
        problem = handlers.TOKEN_UNREADABLE
    budgets = config.budgets()
    idle = config.idle_unload_minutes
    return SettingsOut(
        day_budget_gb=budgets.day_gb,
        night_budget_gb=budgets.night_gb,
        night_start=schedule.clock_text(budgets.night_start),
        night_end=schedule.clock_text(budgets.night_end),
        idle_unload_minutes=idle if isinstance(idle, int) and not isinstance(idle, bool) else 5,
        paused=config.paused is True,
        paused_at=config.paused_at or None,
        paused_by=config.paused_by or None,
        hf_token_configured=bool(config.hf_token_sealed),
        hf_token_problem=problem,
    )


def _now_out(now: datetime, budgets: schedule.Budgets) -> Now:
    return Now(
        local_time=now.replace(tzinfo=None).isoformat(timespec="seconds"),
        overnight=schedule.is_overnight(now, budgets),
        budget_gb=schedule.budget_gb(now, budgets),
        changes_at=schedule.next_change(now, budgets).isoformat(timespec="seconds")
        if schedule.has_window(budgets)
        else None,
    )


def _publishers_out(config: LocalModelsConfig) -> PublishersOut:
    built_in = {name.casefold() for name in policy.DEFAULT_BLOCKED}
    return PublishersOut(
        makers=_strings(config.makers),
        converters=_strings(config.converters),
        blocked=[BlockedName(name=name, built_in=True) for name in policy.DEFAULT_BLOCKED]
        + [
            BlockedName(name=name, built_in=False)
            for name in _strings(config.blocked_added)
            if name.casefold() not in built_in
        ],
    )


def _machine(deps: Deps) -> Machine:
    return Machine(system=deps.system, machine=deps.machine, memory_gb=_physical_memory_gb())


async def _all_models(db: AsyncSession) -> list[LocalModel]:
    return list(
        (
            await db.scalars(
                select(LocalModel)
                .order_by(LocalModel.created_at.desc(), LocalModel.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _models_out(
    db: AsyncSession, deps: Deps, user: User, models: Sequence[LocalModel]
) -> list[ModelOut]:
    seen = await _seen(db, deps, user, models)
    return [_model_out(model, seen) for model in models]


async def _one_model(db: AsyncSession, deps: Deps, user: User, model_id: uuid.UUID) -> ModelOut:
    model = await registry.get_model(db, model_id)
    if model is None:
        raise HTTPException(404, NO_SUCH_MODEL)
    return (await _models_out(db, deps, user, [model]))[0]


async def _model_or_404(db: AsyncSession, model_id: uuid.UUID) -> LocalModel:
    model = await registry.get_model(db, model_id)
    if model is None:
        raise HTTPException(404, NO_SUCH_MODEL)
    return model


async def _jobs_of(db: AsyncSession, model_id: uuid.UUID) -> list[LocalAiJob]:
    """The model's jobs that have not finished."""
    return list(
        (
            await db.scalars(
                select(LocalAiJob)
                .where(LocalAiJob.model_id == model_id, LocalAiJob.state.in_(jobs.ACTIVE))
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _live_jobs_of(db: AsyncSession, model_id: uuid.UUID, now: datetime) -> list[LocalAiJob]:
    """The model's jobs that have not finished, as the page reads them. One whose
    worker died (the page already says "start it again") is ended here as the
    lane's tick would end it, its kind tidying up, so it does not stand in the
    way of starting it again or of removing the model."""
    found = await _jobs_of(db, model_id)
    for job in found:
        await jobs.reap(db, job, now)
    return [job for job in found if job.state in jobs.ACTIVE]


# --- 6.1 the overview -----------------------------------------------------------------


def _once_each(recent: list[LocalAiJob]) -> list[LocalAiJob]:
    """Each job once, where it was last found.

    `jobs.recent` reads the active jobs, then the finished ones. A job that
    ends between those two reads is in both, as one object already refreshed
    to its end, so the page listed the same finished install twice. It is
    kept among the finished, where it now belongs.
    """
    kept: list[LocalAiJob] = []
    ids: set[uuid.UUID] = set()
    for job in reversed(recent):
        if job.id not in ids:
            ids.add(job.id)
            kept.append(job)
    kept.reverse()
    return kept


@router.get("", response_model=Overview)
async def overview(user: CurrentUser, db: DbDep, deps: DepsDep) -> Overview:
    """Everything the AI models page draws. Never calls the network."""
    found = availability(deps.settings)
    config = await load_config(db)
    now = deps.now()
    budgets = config.budgets()
    admin = user.role == "admin"
    notes = {"unavailable": found.reason, "demo": DEMO_NOTE}
    if found.mode == "demo":
        # The demo has no beat: whatever the seed or a restart left waiting is
        # looked at once somebody opens the page (demo.keep_going).
        demo.keep_going(deps.settings)
    common: dict[str, Any] = {
        "mode": found.mode,
        "note": notes.get(found.mode),
        "can_manage": admin and found.ok,
        "machine": _machine(deps),
        "now": _now_out(now, budgets),
        "settings": _settings_out(config),
        "publishers": _publishers_out(config),
    }
    if not found.ok:
        # Nothing here can run a model: nothing more is asked of anything.
        return Overview(
            **common,
            server=ServerOut(state="down"),
            library=None,
            roles=[],
            models=[],
            runtimes=[],
            jobs=[],
        )

    models = await _all_models(db)
    recent = _once_each(await jobs.recent(db, RECENT_JOBS))
    seen = await _seen(db, deps, user, models, extra_users=[job.requested_by for job in recent])
    by_id = {model.id: model for model in models}
    where = await asyncio.to_thread(_where, deps.settings)
    looked = seen.looked
    latest = await load_latest(db)
    return Overview(
        **common,
        server=await _server_out(db, deps.server),
        library=LibraryOut(
            path=where.folder if admin else None,
            where=where.where,
            connected=looked.connected,
            problem=looked.problem_for_admin if admin else looked.problem,
            free_gb=_gb(looked.free),
            copies_to_this_machine=where.copies,
        ),
        roles=[_role_out(role, by_id, seen) for role in ROLES],
        models=[_model_out(model, seen) for model in models],
        runtimes=[
            _runtime_out(name, latest.get(name, {}), seen, system=deps.system, machine=deps.machine)
            for name in deps.kinds
        ],
        jobs=[_job_out(job, admin=admin, now=now, emails=seen.emails) for job in recent],
    )


# --- 6.2 settings ---------------------------------------------------------------------


def _budget(value: object, memory: float | None) -> float:
    number = _number(value)
    if number is None or not 1 <= number <= MOST_BUDGET_GB:
        raise HTTPException(400, BAD_BUDGET)
    if memory is not None:
        ceiling = max(1, int(memory) - LEFT_FOR_MACOS_GB)
        if number > ceiling:
            raise HTTPException(
                400,
                f"This Mac has {schedule.gb_text(memory)} GB of memory. Leave some for macOS: "
                f"choose {ceiling} GB or less.",
            )
    return number


def _clock(value: object) -> str:
    try:
        return schedule.clock_text(schedule.parse_clock(value if isinstance(value, str) else ""))
    except ValueError:
        raise HTTPException(400, schedule.BAD_CLOCK) from None


def _minutes(value: object) -> int:
    number = _number(value)
    if number is None or not number.is_integer() or not 1 <= number <= MOST_IDLE_MINUTES:
        raise HTTPException(400, BAD_IDLE)
    return int(number)


@router.put("/settings", response_model=SettingsSaved, dependencies=[require_admin])
async def save_settings(
    body: SettingsBody, request: Request, user: CurrentUser, db: DbDep, deps: Changing
) -> SettingsSaved:
    """The budgets, the overnight window, the idle time and the pause switch."""
    config = await load_config(db)
    before = replace(config)
    memory = _physical_memory_gb()
    if body.day_budget_gb is not None:
        config.day_budget_gb = _budget(body.day_budget_gb, memory)
    if body.night_budget_gb is not None:
        config.night_budget_gb = _budget(body.night_budget_gb, memory)
    if body.night_start is not None:
        config.night_start = _clock(body.night_start)
    if body.night_end is not None:
        config.night_end = _clock(body.night_end)
    if body.idle_unload_minutes is not None:
        config.idle_unload_minutes = _minutes(body.idle_unload_minutes)
    if body.paused is not None and not isinstance(body.paused, bool):
        raise HTTPException(400, NOT_YES_OR_NO)
    old, new = before.budgets(), config.budgets()
    if new.night_start == new.night_end:
        raise HTTPException(400, WINDOW_NEVER_OPENS)
    if new.night_gb < new.day_gb:
        raise HTTPException(400, NIGHT_SMALLER)

    changed = [
        name
        for name in (
            "day_budget_gb",
            "night_budget_gb",
            "night_start",
            "night_end",
            "idle_unload_minutes",
        )
        if getattr(before, name) != getattr(config, name)
    ]
    pausing = body.paused is True and not before.paused
    resuming = body.paused is False and before.paused
    ip = client_ip(request)
    if pausing:
        config.paused = True
        config.paused_at = deps.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        config.paused_by = user.email
        await auth_service.audit(db, "local_models.paused", actor_user_id=user.id, ip=ip)
    elif resuming:
        config.paused, config.paused_at, config.paused_by = False, "", ""
        await auth_service.audit(db, "local_models.resumed", actor_user_id=user.id, ip=ip)
    if changed:
        await auth_service.audit(
            db,
            "local_models.settings_updated",
            actor_user_id=user.id,
            ip=ip,
            detail={"changed": changed},
        )
    # A job waiting for the window, or for the pause to end, is looked at again
    # with today's settings: it runs, or waits again with a fresh sentence.
    release = resuming or (old != new)
    if release:
        await jobs.release_waiting(db, deps.now())
    await save_config(db, config)  # commits, with the audit rows
    if release:
        await _wake(deps.settings)
    lowered = new.day_gb < old.day_gb or new.night_gb < old.night_gb
    if pausing or lowered:
        await _unload(deps.server)
    return SettingsSaved(settings=_settings_out(config), now=_now_out(deps.now(), new))


# --- 6.3 publishers -------------------------------------------------------------------


def _differences(before: Sequence[str], after: Sequence[str]) -> dict[str, list[str]]:
    had = {name.casefold() for name in before}
    has = {name.casefold() for name in after}
    return {
        "added": [name for name in after if name.casefold() not in had],
        "removed": [name for name in before if name.casefold() not in has],
    }


@router.put("/publishers", response_model=PublishersSaved, dependencies=[require_admin])
async def save_publishers(
    body: PublishersBody, request: Request, user: CurrentUser, db: DbDep, deps: Changing
) -> PublishersSaved:
    """The three lists. Nothing else changes with them (D10): a model no longer
    approved stays, keeps its role, and runs again when its entry is put back."""
    config = await load_config(db)
    old = (_strings(config.makers), _strings(config.converters), _strings(config.blocked_added))
    try:
        makers, converters, blocked = policy.check_lists(
            old[0] if body.makers is None else body.makers,
            old[1] if body.converters is None else body.converters,
            old[2] if body.blocked_added is None else body.blocked_added,
        )
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    changes = {
        name: _differences(was, now)
        for name, was, now in zip(
            ("makers", "converters", "blocked"), old, (makers, converters, blocked), strict=True
        )
    }
    changes = {name: change for name, change in changes.items() if any(change.values())}
    before = config.lists()
    config.makers, config.converters, config.blocked_added = makers, converters, blocked
    after = config.lists()

    lost: list[NoLongerApproved] = []
    if changes:
        held = await registry.holders(db)
        for model in await _all_models(db):
            if not registry.judged(model, before).allowed:
                continue
            verdict = registry.judged(model, after)
            if not verdict.allowed:
                lost.append(
                    NoLongerApproved(
                        id=model.id,
                        repo_id=model.repo_id,
                        reason=verdict.reason,
                        roles=held.get(model.id, []),
                    )
                )
        await auth_service.audit(
            db,
            "local_models.publishers_updated",
            actor_user_id=user.id,
            ip=client_ip(request),
            detail=changes,
        )
        await save_config(db, config)  # commits, with the audit row
    return PublishersSaved(publishers=_publishers_out(config), no_longer_approved=lost)


# --- 6.4 the Hugging Face token -------------------------------------------------------


@router.put("/token", response_model=TokenSaved, dependencies=[require_admin])
async def save_token(
    body: TokenBody, request: Request, user: CurrentUser, db: DbDep, _deps: Changing
) -> TokenSaved:
    """Store a read token, sealed, or remove it (""). It is never sent back."""
    token = body.token.strip()
    if len(token) > MOST_TOKEN:
        raise HTTPException(400, TOKEN_TOO_LONG)
    if any(letter.isspace() for letter in token):
        raise HTTPException(400, TOKEN_SPACES)
    if any(not 33 <= ord(letter) < 127 for letter in token):
        raise HTTPException(400, TOKEN_ODD)
    config = await load_config(db)
    config.with_hf_token(token)
    await auth_service.audit(
        db,
        "local_models.token_saved" if token else "local_models.token_removed",
        actor_user_id=user.id,
        ip=client_ip(request),
    )
    await save_config(db, config)  # commits, with the audit row
    return TokenSaved(hf_token_configured=bool(token))


# --- 6.5 the check --------------------------------------------------------------------


@dataclass(frozen=True)
class _Room:
    library: int | None
    scratch: int | None  # this machine's own disk, only when it is not the library's


def _free_on(folder: Path) -> int | None:
    try:
        nearest = next(path for path in (folder, *folder.parents) if path.exists())
        return shutil.disk_usage(nearest).free
    except (OSError, StopIteration):
        return None


async def _room(settings: Settings) -> _Room:
    free = await _bounded(
        "library-room", lambda: library.free_bytes(settings), LOOK_TIMEOUT_S, None
    )
    separate = await asyncio.to_thread(library.copies_to_this_machine, settings)
    scratch = (
        await asyncio.to_thread(_free_on, library.scratch_root(settings)) if separate else None
    )
    return _Room(free, scratch)


_HUB_STATUS = {
    "not_found": 404,
    "moved": 409,
    "needs_token": 409,
    "no_access": 409,
    "unreachable": 503,
    "rate_limited": 503,
    "bad_answer": 503,
}


async def _checked(
    db: AsyncSession,
    deps: Deps,
    name: str,
    *,
    revision: str | None = None,
    variant: str | None = None,
) -> tuple[preflight.Preflight, tuple[preflight.Installed, ...]]:
    """The check of 3.8, run here; refusals of the name or of Hugging Face as HTTP."""
    try:
        asked = policy.clean_repo_id(name)
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    config = await load_config(db)
    try:
        token = config.hf_token()
    except SecretUnavailable:
        token = None  # the page says so beside the token; an open model needs none
    # Read before the check's thread starts: a thread never touches a session.
    builds = await registry.builds_of(db, asked)
    installed = await asyncio.to_thread(_installed_versions, deps)
    room = await _room(deps.settings)
    hub = deps.hub_for(token)
    settings = deps.settings

    def check() -> preflight.Preflight:
        return preflight.preflight(
            hub,
            asked,
            config=config,
            kinds=deps.kinds,
            installed=installed,
            free_bytes=room.library,
            min_free_bytes=int(settings.min_free_gb * _GB),
            existing=lambda _name: builds,
            revision=revision,
            variant=variant,
            scratch_free_bytes=room.scratch,
        )

    try:
        return await asyncio.to_thread(check), builds
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    except HubError as err:
        raise HTTPException(_HUB_STATUS.get(err.problem, 503), str(err)) from None


def _preflight_out(
    found: preflight.Preflight, builds: Sequence[preflight.Installed], deps: Deps
) -> PreflightOut:
    info = found.info
    kind = None if found.runtime is None else deps.kinds.get(found.runtime)
    existing: ExistingOut | None = None
    if found.existing is not None:
        build = next((build for build in builds if build.id == found.existing), None)
        existing = ExistingOut(
            model_id=found.existing,
            revision_short="" if build is None else build.revision[:7],
            variant="" if build is None else build.variant,
            same_build=found.same_build,
        )
    built = found.variant is not None
    return PreflightOut(
        repo_id=info.repo_id,
        revision=info.revision,
        revision_short=info.revision[:7],
        build_date=info.modified_at,
        ok=found.ok,
        verdict=found.summary,
        checks=[
            CheckOut(
                key=check.key,
                label=check.label,
                tone=check.tone,
                value=check.value,
                detail=check.detail,
            )
            for check in found.checks
        ],
        problems=list(found.problems),
        warnings=list(found.warnings),
        notes=list(found.notes),
        access=found.access,
        size_bytes=found.size_bytes,
        memory_gb=found.memory_gb,
        fits=found.fits if built else None,
        takes=list(found.takes),
        runtime=None
        if kind is None or found.runtime is None
        else PreflightRuntime(
            name=found.runtime, label=kind.label, installed=found.runtime_installed
        ),
        variants=[]
        if len(found.sized) <= 1
        else [
            VariantOut(
                id=sized.variant.id,
                label=sized.variant.label,
                quant=sized.variant.quant,
                bits=preflight.bits_of(sized.variant),
                size_bytes=sized.size_bytes,
                memory_gb=sized.memory_gb,
                fits=sized.fits,
                recommended=sized.variant.id == found.recommended,
                takes=list(sized.variant.takes or found.support.takes),
                note=sized.variant.note,
                files=[
                    VariantFileOut(
                        path=file.path, size=file.size, part=preflight.part_of(file.path)
                    )
                    for file in sized.variant.files
                ],
            )
            for sized in found.sized
        ],
        variant=None if found.variant is None else (found.variant.id or None),
        existing=existing,
    )


@router.post("/preflight", response_model=PreflightOut, dependencies=[require_admin])
async def check_model(body: PreflightBody, db: DbDep, deps: Changing) -> PreflightOut:
    """The Check button: everything about a model that can be learned without
    downloading it. A refused model is still a 200 (`ok: false`), with every
    check that could be filled in. Nothing is stored and nothing is audited."""
    found, builds = await _checked(db, deps, body.name, variant=body.variant)
    return _preflight_out(found, builds, deps)


# --- 6.6 adding a model; 6.7 resuming its download ------------------------------------


async def _library_away(settings: Settings) -> str | None:
    """Why nothing may be written to the library now (no paths), under the
    ten-second rule; None while it may be."""

    def away() -> str | None:
        return library.away(settings)

    silent: str | None = LIBRARY_SILENT
    return await _bounded("library-away", away, LOOK_TIMEOUT_S, silent)


async def _library_writable(settings: Settings) -> None:
    """503 with the library's own sentence while nothing may be written to it."""
    problem = await _library_away(settings)
    if problem is not None:
        raise HTTPException(503, problem)


@router.post("/models", response_model=ModelOut, status_code=202, dependencies=[require_admin])
async def add_model(
    body: AddBody, request: Request, user: CurrentUser, db: DbDep, deps: Changing
) -> ModelOut:
    """The Download button. The whole check runs again at the build that was shown
    (D11); nothing else the browser sent is trusted."""
    revision = body.revision.strip().lower()
    if len(revision) != 40 or any(letter not in "0123456789abcdef" for letter in revision):
        raise HTTPException(400, NOT_A_BUILD)
    await _library_writable(deps.settings)
    found, _builds = await _checked(db, deps, body.name, revision=revision, variant=body.variant)
    if found.problems:
        raise HTTPException(409, found.summary)
    if found.warnings and not body.accept_warnings:
        raise HTTPException(428, " ".join((*found.warnings, DOWNLOAD_ANYWAY)))
    try:
        model = await registry.add_model(db, found, user.id)
    except registry.Refusal as err:
        raise HTTPException(409, str(err)) from None
    job = await jobs.create(
        db, "download", subject=subject_of(model), model_id=model.id, user_id=user.id
    )
    model_id, job_id = model.id, job.id
    await db.commit()
    if not await _wake(deps.settings):
        # Nothing is left waiting for a worker that will never come.
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id == job_id))
        await registry.delete_model(db, model_id)
        await db.commit()
        raise HTTPException(503, QUEUE_DOWN)
    await auth_service.audit(
        db,
        "local_models.model_added",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={
            "repo_id": model.repo_id,
            "revision": model.revision,
            "variant": model.variant,
            "runtime": model.runtime,
            "publisher": model.publisher,
            "via": found.verdict.via,
            "licence": model.licence,
            "size_bytes": model.size_bytes,
        },
    )
    await db.commit()
    log.info("local_models.model_added", repo_id=model.repo_id, revision=model.revision)
    return await _one_model(db, deps, user, model_id)


@router.post(
    "/models/{model_id}/download",
    response_model=ModelOut,
    status_code=202,
    dependencies=[require_admin],
)
async def resume_download(
    model_id: uuid.UUID, request: Request, user: CurrentUser, db: DbDep, deps: Changing
) -> ModelOut:
    """Resume, or Download again: fetch what is missing, keeping what arrived."""
    model = await _model_or_404(db, model_id)
    if any(job.kind == "download" for job in await _live_jobs_of(db, model.id, deps.now())):
        raise HTTPException(409, ALREADY_DOWNLOADING)
    # A download whose worker died was just ended, and its model put back.
    await registry.get_model(db, model.id)
    if model.runtime not in deps.kinds:
        raise HTTPException(409, registry.NO_PROGRAM_HERE)
    try:
        files = registry.snapshot(model)
    except ValueError as err:
        raise HTTPException(409, str(err)) from None
    if model.state == registry.INSTALLED:
        here = await _bounded(
            f"presence-{model.id}",
            lambda: library.presence(files, deps.settings),
            LOOK_TIMEOUT_S,
            library.Presence(in_library=None, on_this_machine=False),
        )
        if here.in_library is True:
            raise HTTPException(409, ALREADY_HERE)
    await _library_writable(deps.settings)
    was_state, was_error = model.state, model.error
    # It reads as downloading from now, so Remove waits for Cancel.
    await _model_state(db, model.id, registry.DOWNLOADING, None)
    try:
        job = await jobs.create(
            db, "download", subject=subject_of(model), model_id=model.id, user_id=user.id
        )
    except jobs.JobRefused:
        raise HTTPException(409, ALREADY_DOWNLOADING) from None
    job_id = job.id
    await db.commit()
    if not await _wake(deps.settings):
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id == job_id))
        await _model_state(db, model.id, was_state, was_error)
        await db.commit()
        raise HTTPException(503, QUEUE_DOWN)
    await auth_service.audit(
        db,
        "local_models.download_resumed",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"repo_id": model.repo_id, "revision": model.revision, "variant": model.variant},
    )
    await db.commit()
    return await _one_model(db, deps, user, model.id)


async def _model_state(
    db: AsyncSession, model_id: uuid.UUID, state: str, error: str | None
) -> None:
    await db.execute(
        update(LocalModel)
        .where(LocalModel.id == model_id)
        .values(state=state, error=error)
        .execution_options(synchronize_session=False)
    )
    await registry.get_model(db, model_id)  # brings this session's copy up to date


# --- 6.8 removing a model -------------------------------------------------------------


async def _never_reached_the_library(db: AsyncSession, model: LocalModel) -> bool:
    """No file of it can be in the library: it was never installed, and no
    download of it ever got a byte."""
    if model.state == registry.INSTALLED:
        return False
    fetched = await db.scalar(
        select(func.max(LocalAiJob.done)).where(
            LocalAiJob.model_id == model.id, LocalAiJob.kind == "download"
        )
    )
    return not fetched


def _files_for_removal(model: LocalModel) -> library.ModelFiles:
    """The row as the library removes it. A stored file list that cannot be
    read gives none: the library then removes what the folder's own manifest
    lists, and nothing else."""
    try:
        return registry.snapshot(model)
    except ValueError:
        return library.ModelFiles(
            id=model.id,
            repo_id=model.repo_id,
            revision=model.revision,
            variant=model.variant or "",
            runtime=model.runtime,
            files=(),
            demo=kinds.is_demo_runtime(model.runtime),
        )


@router.delete("/models/{model_id}", response_model=Removed, dependencies=[require_admin])
async def remove_model(
    model_id: uuid.UUID, request: Request, user: CurrentUser, db: DbDep, deps: Changing
) -> Removed:
    """Remove a model: its files in the library, its download so far, its copy on
    this machine, and its row. Jobs about it stay as history."""
    model = await _model_or_404(db, model_id)
    in_the_way = await registry.remove_problem(db, model.id)
    if in_the_way is not None:
        raise HTTPException(409, in_the_way)
    now = deps.now()
    pending = await _live_jobs_of(db, model.id, now)
    if any(job.kind == "download" for job in pending):
        raise HTTPException(409, BEING_DOWNLOADED)
    if any(job.state == "running" for job in pending):
        raise HTTPException(409, BEING_USED)
    files = _files_for_removal(model)
    settings = deps.settings
    freed = 0
    away = await _library_away(settings)
    if away is not None:
        if not await _never_reached_the_library(db, model):
            raise HTTPException(503, CANNOT_REMOVE)
        freed = await asyncio.to_thread(library.drop_local, files, settings)
    else:
        status = await _server_status(deps.server)
        if (
            status is not None
            and status.get("repo_id") == model.repo_id
            and status.get("revision") == model.revision
        ):
            await _unload(deps.server, REMOVE_UNLOAD_WAIT_S)
        try:
            # A write is seen through, not abandoned at ten seconds.
            freed = await asyncio.to_thread(library.remove, files, settings)
        except library.LibraryProblem as err:
            if err.resumable:
                raise HTTPException(503, str(err)) from None
            # Its name cannot be a folder: nothing of it can be on disk.
    # Read before the row goes (it leaves this session with it).
    removed = {"repo_id": model.repo_id, "revision": model.revision, "variant": model.variant}
    for job in pending:
        await jobs.request_cancel(db, job.id)
    try:
        await registry.delete_model(db, model.id)
    except registry.Refusal as err:
        raise HTTPException(409, str(err)) from None
    await auth_service.audit(
        db,
        "local_models.model_removed",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={**removed, "freed_bytes": freed},
    )
    await db.commit()
    log.info("local_models.model_removed", repo_id=removed["repo_id"], freed_bytes=freed)
    return Removed(freed_bytes=freed)


# --- 6.11 cancelling a job ------------------------------------------------------------


@router.post("/jobs/{job_id}/cancel", response_model=JobOut, dependencies=[require_admin])
async def cancel_job(
    job_id: uuid.UUID, request: Request, user: CurrentUser, db: DbDep, deps: Changing
) -> JobOut:
    """A job not yet running ends at once (a download's model then reads "Cancelled.
    Resume carries on from …"); a running one is asked to stop, and does so
    between pieces of its work (`cancelling` until then)."""
    try:
        job = await jobs.request_cancel(db, job_id)
    except jobs.NoSuchJob:
        raise HTTPException(404, NO_SUCH_JOB) from None
    except jobs.JobRefused as err:
        raise HTTPException(409, str(err)) from None
    await auth_service.audit(
        db,
        "local_models.job_cancelled",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"kind": job.kind, "subject": job.subject},
    )
    await db.commit()
    emails = await _emails(db, {job.requested_by} if job.requested_by else set())
    return _job_out(job, admin=True, now=deps.now(), emails=emails)


# --- 6.9 who does what ------------------------------------------------------------------


async def _copies_for(db: AsyncSession, role: str, now: datetime) -> list[LocalAiJob]:
    """The copies to this machine under way for a role (as `jobs.shown` reads them)."""
    found = await db.scalars(
        select(LocalAiJob)
        .where(LocalAiJob.kind == "place", LocalAiJob.state.in_(jobs.ACTIVE))
        .execution_options(populate_existing=True)
    )
    return [
        job for job in found if _asked_role(job) == role and jobs.shown(job, now)[0] in jobs.ACTIVE
    ]


async def _subject_by_id(db: AsyncSession, model_id: uuid.UUID | None) -> str | None:
    """A model as the audit log names it (`publisher/name@<8>`), or None."""
    if model_id is None:
        return None
    model = await registry.get_model(db, model_id)
    return None if model is None else subject_of(model)


async def _one_role(db: AsyncSession, deps: Deps, user: User, role: str) -> RoleOut:
    row = (await registry.roles(db))[role]
    models: list[LocalModel] = []
    for model_id in dict.fromkeys((row.model_id, row.pending_model_id)):
        if model_id is None:
            continue
        model = await registry.get_model(db, model_id)
        if model is not None:
            models.append(model)
    seen = await _seen(db, deps, user, models)
    return _role_out(role, {model.id: model for model in models}, seen)


def _drop_copy(files: library.ModelFiles, settings: Settings) -> None:
    """A copy on this machine nothing runs from any more. Best effort: it is only space."""
    try:
        library.drop_local(files, settings)
    except (OSError, library.LibraryProblem):
        log.warning("local_models.copy_not_removed", model_id=str(files.id), exc_info=True)


async def _drop_copies(
    db: AsyncSession, model_ids: Sequence[uuid.UUID | None], settings: Settings, now: datetime
) -> None:
    """Remove the copies on this machine of models that now hold no role and wait
    for none, and that no job is using this moment."""
    held = await registry.holders(db)
    for model_id in dict.fromkeys(model_ids):
        if model_id is None or model_id in held:
            continue
        if await jobs.in_use(db, model_id, now):
            # A Test may be loading from this copy right now. The Test tidies
            # it away when it ends (handlers._test).
            continue
        model = await registry.get_model(db, model_id)
        if model is None:
            continue
        try:
            files = registry.snapshot(model)
        except ValueError:
            continue  # its files cannot be named, so none are removed
        await asyncio.to_thread(_drop_copy, files, settings)


def _models_moving(settings: Settings) -> bool:
    """A move of the model library is in its last steps (its writers are paused)."""
    try:
        return stores.read_state(settings.data_dir).is_paused(library.KIND)
    except (OSError, ValueError):
        return False


async def _queue_copy(
    db: AsyncSession, deps: Deps, user: User, role: str, model: LocalModel
) -> LocalAiJob | None:
    """Line the model up for the role and queue its copy to this machine; committed.
    None when the queue is down (and then nothing is left waiting)."""
    await registry.set_role(db, role, model, pending=True, user_id=user.id)
    job = await jobs.create(
        db,
        "place",
        subject=subject_of(model),
        model_id=model.id,
        role=role,
        payload={"role": role},
        user_id=user.id,
        # Both roles may wait for the same model. A second copy for the same
        # role is refused by `give_role` itself, in its own words.
        unique=False,
    )
    job_id = job.id
    await db.commit()
    if await _wake(deps.settings):
        return job
    await db.execute(delete(LocalAiJob).where(LocalAiJob.id == job_id))
    await registry.drop_pending(db, role, model.id)
    await db.commit()
    return None


async def _close_the_move_gap(
    db: AsyncSession, deps: Deps, user: User, role: str, model: LocalModel
) -> None:
    """After a role was given with no copy (the library is on this machine, or
    the copy here is whole).

    A move of the library in its last steps has already read which models it
    must leave a copy of on this machine, and this one was not among them. A
    copy for the same model ("Copy again") waits out the move and copies it
    if the library left (`handlers._keep_here`). Asked after the commit, so a
    move that pauses later reads this role itself. Best effort: the role is
    given either way, and the role card offers Copy again.
    """
    if not await asyncio.to_thread(_models_moving, deps.settings):
        return
    if await _queue_copy(db, deps, user, role, model) is None:
        log.warning("local_models.role_copy_not_queued", role=role, model_id=str(model.id))


@router.put("/roles/{role}", response_model=RoleOut, dependencies=[require_admin])
async def give_role(
    role: str,
    body: RoleBody,
    request: Request,
    response: Response,
    user: CurrentUser,
    db: DbDep,
    deps: Changing,
) -> RoleOut:
    """Give a role to a model, or take it away (`model_id: null`).

    A model on a program that asks (llama.cpp) and that nothing shows answering
    for this role is not given it before the operator says yes: 428 with the
    question (gate), and the same call with `skip_compare: true` goes ahead.
    The model that has the role or waits for it ("Copy again") is not asked
    again, nor is taking a role away.

    A model already on this machine (or a library that is on it) gets the
    role at once: 200. Otherwise the model is lined up for it and copied
    here first, and whoever has the role keeps it until the copy is whole:
    202, with the copy's job. Sending the model that has the role when its
    copy is missing ("Copy again") lands there too.
    """
    if role not in ROLES:
        raise HTTPException(404, NO_SUCH_ROLE)
    now = deps.now()
    row = (await registry.roles(db))[role]
    holder, waiting = row.model_id, row.pending_model_id
    copying = await _copies_for(db, role, now)
    ip = client_ip(request)

    if body.model_id is None:
        if holder is None and waiting is None and not copying:
            return await _one_role(db, deps, user, role)
        # A copy under way stops between files and its handler keeps what it
        # copied so far; removing that under it would race the copy.
        started = {job.model_id for job in copying if jobs.shown(job, now)[0] == "running"}
        for job in copying:
            try:
                await jobs.request_cancel(db, job.id)
            except (jobs.NoSuchJob, jobs.JobRefused):
                continue  # it ended meanwhile
        await registry.set_role(db, role, None, pending=False, user_id=user.id)
        await auth_service.audit(
            db,
            "local_models.role_cleared",
            actor_user_id=user.id,
            ip=ip,
            detail={"role": role, "from": await _subject_by_id(db, holder), "to": None},
        )
        await db.commit()
        dropped = [holder] if waiting in started else [holder, waiting]
        await _drop_copies(db, dropped, deps.settings, deps.now())
        await _unload(deps.server)
        return await _one_role(db, deps, user, role)

    try:
        model_id = uuid.UUID(body.model_id)
    except ValueError:
        raise HTTPException(404, NO_SUCH_MODEL) from None
    model = await _model_or_404(db, model_id)
    config = await load_config(db)
    problem = registry.role_problem(model, role, config.lists(), config.budgets(), deps.kinds)
    if problem is not None:
        raise HTTPException(409, problem)
    if any(job.model_id == model.id for job in copying):
        raise HTTPException(409, ALREADY_COPYING)
    if copying:
        raise HTTPException(409, ROLE_TAKEN)
    try:
        files = registry.snapshot(model)
    except ValueError as err:
        raise HTTPException(409, str(err)) from None
    # Asked after every refusal that can be known now, so a yes is not met by
    # one the question could have come with. It is never a refusal itself.
    known = await gate.evidence(db, model, role, deps.kinds)
    if known.asks(body.skip_compare) and model.id not in (holder, waiting):
        raise HTTPException(428, known.question)
    separate = await asyncio.to_thread(library.copies_to_this_machine, deps.settings)
    here = await _bounded(
        f"presence-{model.id}",
        lambda: library.presence(files, deps.settings),
        LOOK_TIMEOUT_S,
        library.Presence(in_library=None, on_this_machine=False),
    )
    given_from = await _subject_by_id(db, holder)

    if separate and not here.on_this_machine:
        # The copy reads the library, so only a library that cannot be read
        # stops it (a move's pause stops writing to it, not copying out).
        silent: str | None = LIBRARY_SILENT
        away = await _bounded(
            "library-unreachable",
            lambda: library.unreachable(deps.settings),
            LOOK_TIMEOUT_S,
            silent,
        )
        if away is not None:
            raise HTTPException(503, away)
        if await _queue_copy(db, deps, user, role, model) is None:
            raise HTTPException(503, QUEUE_DOWN)
        await auth_service.audit(
            db,
            "local_models.role_assigned",
            actor_user_id=user.id,
            ip=ip,
            detail={
                "role": role,
                "from": given_from,
                "to": subject_of(model),
                "copy": True,
                "compare": known.said(),
            },
        )
        await db.commit()
        response.status_code = 202
        return await _one_role(db, deps, user, role)

    if holder == model.id and waiting is None:
        return await _one_role(db, deps, user, role)  # it has the role already: nothing changes
    await registry.set_role(db, role, model, pending=False, user_id=user.id)
    await auth_service.audit(
        db,
        "local_models.role_assigned",
        actor_user_id=user.id,
        ip=ip,
        detail={
            "role": role,
            "from": given_from,
            "to": subject_of(model),
            "copy": False,
            "compare": known.said(),
        },
    )
    await db.commit()
    await _drop_copies(db, [holder, waiting], deps.settings, deps.now())
    await _close_the_move_gap(db, deps, user, role, model)
    if await _copies_for(db, role, deps.now()):
        response.status_code = 202
    return await _one_role(db, deps, user, role)


# --- 6.10 Test --------------------------------------------------------------------------


@router.post(
    "/models/{model_id}/test",
    response_model=TestOut,
    status_code=202,
    dependencies=[require_admin],
)
async def test_model(
    model_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    deps: Changing,
    body: TestBody | None = None,
) -> TestOut:
    """Ask the model one question about a picture (or, for a model that reads
    no pictures, in words). Queued ahead of everything else; a model that only
    fits overnight is not refused: its test waits for the window and says so."""
    model = await _model_or_404(db, model_id)
    asset_id: uuid.UUID | None = None
    if body is not None and body.asset_id is not None:
        try:
            asset_id = uuid.UUID(body.asset_id)
        except ValueError:
            raise HTTPException(404, handlers.NO_SUCH_PICTURE) from None
        there = await db.scalar(select(exists().where(Asset.id == asset_id, catalogued())))
        if not there:
            raise HTTPException(404, handlers.NO_SUCH_PICTURE)
    if model.state == registry.DOWNLOADING:
        raise HTTPException(409, registry.STILL_DOWNLOADING)
    if model.state != registry.INSTALLED:
        raise HTTPException(409, registry.NOT_COMPLETE)
    kind = deps.kinds.get(model.runtime)
    if kind is None:
        raise HTTPException(409, registry.NO_PROGRAM_HERE)
    on_disk = (await asyncio.to_thread(_runtimes_on_disk, deps)).get(model.runtime)
    if on_disk is not None and on_disk.problem is not None:
        raise HTTPException(409, on_disk.problem)  # an administrator's: the path may show
    if on_disk is None or on_disk.installed is None or on_disk.installed.current is None:
        raise HTTPException(409, INSTALL_FIRST.format(label=kind.label))
    config = await load_config(db)
    verdict = registry.judged(model, config.lists())
    if not verdict.allowed:
        raise HTTPException(409, f"It is no longer approved: {verdict.reason}")
    needed = registry.memory_gb(model)
    budgets = config.budgets()
    if schedule.fits(needed, budgets) == "never":
        # Not a wait: it would never start until a budget is raised.
        raise HTTPException(409, schedule.decide(needed, deps.now(), budgets).message)
    try:
        job = await jobs.create(
            db,
            "test",
            subject=subject_of(model),
            model_id=model.id,
            payload={} if asset_id is None else {"asset_id": str(asset_id)},
            priority=0,  # somebody pressed the button and is watching
            user_id=user.id,
        )
    except jobs.JobRefused:
        raise HTTPException(409, TEST_WAITING) from None
    job_id = job.id
    await db.commit()
    if not await _wake(deps.settings):
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id == job_id))
        await db.commit()
        raise HTTPException(503, QUEUE_DOWN)
    await auth_service.audit(
        db,
        "local_models.model_tested",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={
            "repo_id": model.repo_id,
            "revision": model.revision,
            "variant": model.variant,
            "asset_id": None if asset_id is None else str(asset_id),
        },
    )
    await db.commit()
    shown = await db.get(LocalAiJob, job_id, populate_existing=True)
    if shown is None:
        raise HTTPException(404, NO_SUCH_JOB)
    return shown_test(shown, admin=True, now=deps.now())


@functools.cache
def _sample_picture() -> bytes:
    return handlers.drawn_picture()


@router.get("/sample.jpg")
async def sample_picture(_user: CurrentUser) -> Response:
    """The picture FrameFound draws for a Test when the catalogue has no
    photograph to show, so the page can show what the model was shown."""
    jpeg = await asyncio.to_thread(_sample_picture)
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": SAMPLE_CACHE})


# --- 6.12 the software that runs models -------------------------------------------------


def _kind_or_404(deps: Deps, name: str) -> RuntimeKind:
    kind = deps.kinds.get(name)
    if kind is None:
        raise HTTPException(404, NO_SUCH_RUNTIME)
    return kind


async def _one_runtime(db: AsyncSession, deps: Deps, user: User, name: str) -> RuntimeOut:
    seen = await _seen(db, deps, user, [])
    latest = await load_latest(db)
    return _runtime_out(name, latest.get(name, {}), seen, system=deps.system, machine=deps.machine)


def _program_gone(deps: Deps, kind: RuntimeKind, version: str) -> bool:
    """`version` is installed on paper (its folder is marked whole) and its program is missing."""
    try:
        folder = runtimes.version_dir(runtimes.root(deps.settings), kind.name, version)
        return (folder / runtimes.WHOLE).is_file() and kind.program(folder) is None
    except (OSError, ValueError, runtimes.RuntimeProblem):
        # A folder that cannot be read is not known to be broken: left alone.
        return False


def _unmark(deps: Deps, kind: RuntimeKind, version: str) -> None:
    folder = runtimes.version_dir(runtimes.root(deps.settings), kind.name, version)
    (folder / runtimes.WHOLE).unlink(missing_ok=True)


async def _installing(db: AsyncSession, name: str, now: datetime) -> bool:
    """An install of this kind is waiting or under way."""
    found = await db.scalars(
        select(LocalAiJob)
        .where(LocalAiJob.kind == "runtime_install", LocalAiJob.state.in_(jobs.ACTIVE))
        .execution_options(populate_existing=True)
    )
    return any(
        isinstance(job.payload, dict)
        and job.payload.get("name") == name
        and jobs.shown(job, now)[0] in jobs.ACTIVE
        for job in found
    )


async def _look_up_latest(db: AsyncSession, deps: Deps, name: str, kind: RuntimeKind) -> str:
    """The newest version on offer, looked up now and remembered (committed).
    503 with the sentence, also remembered, when the index cannot say."""
    try:
        version = await asyncio.to_thread(kind.latest, deps.fetch_json)
    except HubError as err:
        # Each kind says where it asked in its own words (the package index,
        # GitHub's release page), so the sentence is the error's.
        said = str(err)
        await save_latest(db, name, None, said)
        raise HTTPException(503, said) from None
    await save_latest(db, name, version, None)
    return version


@router.post("/runtimes/{name}/check", response_model=RuntimeOut, dependencies=[require_admin])
async def check_runtime(name: str, user: CurrentUser, db: DbDep, deps: Changing) -> RuntimeOut:
    """Check now: look up the newest version on offer. It changes nothing but
    the remembered answer, so it is not audited (as the model check is not)."""
    kind = _kind_or_404(deps, name)
    await _look_up_latest(db, deps, name, kind)
    return await _one_runtime(db, deps, user, name)


@router.post(
    "/runtimes/{name}/install",
    response_model=RuntimeOut,
    status_code=202,
    dependencies=[require_admin],
)
async def install_runtime(
    name: str,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    deps: Changing,
    body: InstallBody | None = None,
) -> RuntimeOut:
    """Install, Update, or Install a particular version: one call. The new
    version is switched to only once it has installed and passed its check;
    until then the one in use carries on.

    A version whose folder is there but whose program has gone (a venv or an
    unpacked archive deleted by hand) is installed afresh, even the one in
    use: that is the repair `framefound doctor` points to."""
    kind = _kind_or_404(deps, name)
    why_not = kind.supported_here(deps.system, deps.machine)
    if why_not is not None:
        raise HTTPException(409, why_not)
    if await _installing(db, name, deps.now()):
        raise HTTPException(409, BEING_INSTALLED.format(label=kind.label))
    asked = None if body is None or body.version is None else body.version.strip()
    if asked:
        try:
            version = runtimes.clean_version(kind, asked)
        except ValueError as err:
            raise HTTPException(400, str(err)) from None
    else:
        version = await _look_up_latest(db, deps, name, kind)
    on_disk = (await asyncio.to_thread(_runtimes_on_disk, deps)).get(name)
    current = None if on_disk is None or on_disk.installed is None else on_disk.installed.current
    broken = await asyncio.to_thread(_program_gone, deps, kind, version)
    if current == version and not broken:
        raise HTTPException(409, IN_USE_ALREADY.format(version=version))
    # The job would fail on its first step; saying so now saves the wait.
    if kind.needs_uv and await asyncio.to_thread(handlers._which, deps.uv) is None:
        raise HTTPException(409, handlers.NO_UV)
    if broken:
        # Left marked whole, the install would find the version "already
        # installed" and switch to it as it is. Unmarked, it is half an
        # install, which the install removes and makes again.
        await asyncio.to_thread(_unmark, deps, kind, version)
    try:
        job = await jobs.create(
            db,
            "runtime_install",
            subject=f"{kind.label} {version}",
            payload={"name": name, "version": version},
            user_id=user.id,
        )
    except jobs.JobRefused:
        raise HTTPException(409, BEING_INSTALLED.format(label=kind.label)) from None
    job_id = job.id
    await db.commit()
    if not await _wake(deps.settings):
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id == job_id))
        await db.commit()
        raise HTTPException(503, QUEUE_DOWN)
    await auth_service.audit(
        db,
        "local_models.runtime_install_started",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"name": name, "version": version},
    )
    await db.commit()
    return await _one_runtime(db, deps, user, name)


@router.post("/runtimes/{name}/rollback", response_model=RuntimeOut, dependencies=[require_admin])
async def rollback_runtime(
    name: str, request: Request, user: CurrentUser, db: DbDep, deps: Changing
) -> RuntimeOut:
    """Go back to the version kept from before the last update: one small file
    is rewritten, here and now. The model in memory is let go of, so the next
    question loads it under the older version."""
    kind = _kind_or_404(deps, name)
    if await _installing(db, name, deps.now()):
        raise HTTPException(409, WAIT_FOR_INSTALL.format(label=kind.label))
    settings = deps.settings

    def go_back() -> tuple[str | None, str]:
        root = runtimes.root(settings)
        before = runtimes.describe(root, name).current
        return before, runtimes.rollback(root, name)

    try:
        before, after = await asyncio.to_thread(go_back)
    except runtimes.RuntimeProblem as err:
        raise HTTPException(409, str(err)) from None
    await auth_service.audit(
        db,
        "local_models.runtime_rolled_back",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"name": name, "from": before, "to": after},
    )
    await db.commit()
    await _unload(deps.server)
    return await _one_runtime(db, deps, user, name)


# --- 6.13 letting go of the model in memory ---------------------------------------------

_BUSY = object()


@router.post("/server/unload", response_model=Unloaded, dependencies=[require_admin])
async def unload_now(request: Request, user: CurrentUser, db: DbDep, deps: Changing) -> Unloaded:
    """Unload now: the memory is given back at once, and the model loads again
    the next time it is needed."""
    server = deps.server

    found = await _bounded(_UNLOAD_KEY, _unload_work(server), REMOVE_UNLOAD_WAIT_S, _BUSY)
    _STATUS.pop(_STATUS_KEY, None)
    if found is _BUSY:
        # The answer under way finishes first; the unload asked for carries on
        # in its thread and happens then.
        raise HTTPException(409, SERVER_BUSY)
    if found is None:
        raise HTTPException(503, SERVER_NOT_ANSWERING)
    if isinstance(found, ServerRefused):
        raise HTTPException(409, str(found))
    unloaded = found is True
    await auth_service.audit(
        db,
        "local_models.unloaded",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"unloaded": unloaded},
    )
    await db.commit()
    return Unloaded(unloaded=unloaded)
