"""The database side of local models and roles: which builds were added, and
which model does which job.

Everything here is about rows. Where a model's files are, whether they are
whole and how they are fetched is the library's business; whether a model
may run at this hour is decided when it is asked. This module answers the
questions that only the database can: is this build already here, who has a
role, may this model be given one, is it still approved.

Three rules are kept here rather than left to whoever calls:

- **One row per build, whatever the capitals of its name.** `add_model` lets
  the database decide (one statement, the unique key), so two requests that
  arrive together cannot both add it.
- **A role changes hands only for the model it was waiting for.**
  `complete_role` is one guarded statement: if the operator chose another
  model, or took the role away, while the copy was being made, nothing
  changes.
- **Writes name their columns.** Two parties write these tables (the page,
  and the lane that finishes a download or a copy), so nothing here writes a
  row back from a copy read earlier. A role's `model_id` and its
  `pending_model_id`, or the Looker's row and the Editor's, can be written
  at the same moment without one undoing the other.

Every function that writes sends its statement at once and leaves the
commit to the caller, so a change and its audit row are one transaction.
"""

import math
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

# `sqlalchemy.orm` stopped re-exporting it in 2.1; `orm.util` has it in 2.0 and 2.1 alike.
from sqlalchemy.orm.util import identity_key

from framefound.db.models import LocalAiJob, LocalAiRole, LocalModel
from framefound.local_models import (
    ROLE_NEEDS,
    ROLE_TITLES,
    ROLES,
    library,
    policy,
    preflight,
    schedule,
)
from framefound.local_models.files import safe_path
from framefound.local_models.hub import RepoFile
from framefound.local_models.kinds import is_demo_runtime

# The three states of a model's row. `installed` is set only after every
# file has been checked and the manifest written; `incomplete` carries the
# reason in `error` and is what Resume starts from.
DOWNLOADING = "downloading"
INSTALLED = "installed"
INCOMPLETE = "incomplete"

STILL_DOWNLOADING = "It is still downloading."
NOT_COMPLETE = "Its download did not finish. Resume the download first."
NO_PROGRAM_HERE = "It needs a program this install does not have."
NOTHING_TO_ADD = "FrameFound found nothing in this model that it could download and run."
TOO_LONG = "This model's name is longer than FrameFound can store."
BAD_RECORD = (
    "FrameFound's record of what this model was built from cannot be read, so it cannot be "
    "checked. Remove the model and add it again."
)
BAD_FILES_RECORD = (
    "FrameFound's record of this model's files cannot be read. Remove the model and add it again."
)

# A memory figure above this is a unit mistake (bytes read as gigabytes),
# not a measurement: no Mac has it. It matters because the measured figure
# only ever goes up, so one such reading would keep the model from running
# for good, with nothing on the page to undo it.
_MOST_MEMORY_GB = 1024.0

# The widths of `local_models.repo_id` / `repo_key` and `.variant`.
_LONGEST_REPO_ID = 200
_LONGEST_VARIANT = 100
_SHA256_LENGTH = 64
_BLOB_ID_LENGTH = 40
# Any hour will do: a model that fits no budget is refused in the same
# words at every hour (schedule.decide), and the sentence is what is wanted.
_ANY_HOUR = datetime(2000, 1, 1, 12, 0)


class Refusal(RuntimeError):
    """The registry would not make the change. The message is a sentence for the operator."""


@dataclass(frozen=True)
class Handover:
    """What `complete_role` did.

    `changed` is False when the role was no longer waiting for that model:
    the operator chose another one, or took the role away, while the copy
    was being made. `previous` is who had the role until now — None when
    nobody did, and always None when nothing changed, which is why the two
    are separate answers. It can be the same model (its copy was made again).
    """

    changed: bool
    previous: uuid.UUID | None = None


# --- reading a row ---------------------------------------------------------------


def files_of(model: LocalModel) -> tuple[RepoFile, ...]:
    """The files this build is made of, as stored when it was added.

    `ValueError` (a sentence) when the stored list cannot be read in full. A
    list read in part would be a model checked in part: a file left out here
    is one nobody fetches and nobody verifies.
    """
    stored: object = model.files
    if not isinstance(stored, list) or not stored:
        raise ValueError(BAD_FILES_RECORD)
    files = [_file(entry) for entry in stored]
    if len({file.path.casefold() for file in files}) != len(files):
        raise ValueError(BAD_FILES_RECORD)
    return tuple(files)


def snapshot(model: LocalModel) -> library.ModelFiles:
    """The row as the library needs it: a plain value a thread may hold
    while the session that read it goes on. `ValueError(BAD_FILES_RECORD)`
    when the stored file list cannot be read in full (`files_of`)."""
    return library.ModelFiles(
        id=model.id,
        repo_id=model.repo_id,
        revision=model.revision,
        variant=model.variant or "",
        runtime=model.runtime,
        files=files_of(model),
        # Only the demo's own kinds write the demo's few-kilobyte files.
        demo=is_demo_runtime(model.runtime),
    )


def nodes(model: LocalModel) -> tuple[policy.Node, ...]:
    """The stored chain: this model first, then what it was built from.

    Entries that cannot be read are left out. That never makes a model more
    acceptable: `judged` refuses a chain that did not read whole.
    """
    return _chain(model)[0]


def judged(model: LocalModel, lists: policy.PublisherLists) -> policy.Verdict:
    """Is this model approved under the lists in force today?

    Judged from the chain stored with the model, by the same rule that let
    it in. So a publisher blocked after a model was added stops that model,
    and taking the block away lets it run again; nothing is deleted either
    way. A stored chain that cannot be read, or that is about some other
    model, approves nothing.
    """
    chain, whole = _chain(model)
    if not whole or not chain or chain[0].repo_id.casefold() != model.repo_key:
        return policy.Verdict(False, None, BAD_RECORD)
    return policy.judge(chain, lists)


def memory_gb(model: LocalModel) -> float:
    """What the model is taken to need: the highest figure a real answer reported, once
    there is one that is larger than the estimate made from its size."""
    return max(model.measured_memory_gb or 0.0, model.memory_gb)


def role_problem(
    model: LocalModel,
    role: str,
    lists: policy.PublisherLists,
    budgets: schedule.Budgets,
    known_kinds: Collection[str],
) -> str | None:
    """None when the model may be given the role; else the sentence saying why not.

    `known_kinds` is the names of the runtime kinds this install has
    (`kinds.kinds()`). Whether that program is installed yet is not asked: a
    role can be given first and the program installed after.
    """
    _known(role)
    if model.state == DOWNLOADING:
        return STILL_DOWNLOADING
    if model.state != INSTALLED:
        return NOT_COMPLETE
    needs = ROLE_NEEDS[role]
    takes: object = model.takes
    if not isinstance(takes, list) or needs not in takes:
        return "It does not read pictures." if needs == "image" else f"It does not take {needs}."
    verdict = judged(model, lists)
    if not verdict.allowed:
        return f"It is no longer approved: {verdict.reason}"
    needed = memory_gb(model)
    if schedule.fits(needed, budgets) == "never":
        return schedule.decide(needed, _ANY_HOUR, budgets).message
    if model.runtime not in known_kinds:
        return NO_PROGRAM_HERE
    return None


# --- models ----------------------------------------------------------------------


async def get_model(db: AsyncSession, model_id: uuid.UUID) -> LocalModel | None:
    """The row as the database has it now, not as this session last saw it."""
    return await db.get(LocalModel, model_id, populate_existing=True)


async def find_build(
    db: AsyncSession, repo_id: str, revision: str, variant: str = ""
) -> LocalModel | None:
    """The row for exactly this build, whatever the capitals of the name it is asked by."""
    return (
        await db.execute(
            select(LocalModel)
            .where(
                LocalModel.repo_key == repo_id.strip().casefold(),
                LocalModel.revision == revision.strip().lower(),
                LocalModel.variant == variant,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def builds_of(db: AsyncSession, repo_id: str) -> tuple[preflight.Installed, ...]:
    """Every build of a repository that has a row, the newest first.

    What the check's `existing` answers with. Read it before the check's
    thread starts (a thread never touches a session) and hand the check a
    function that returns it.
    """
    rows = await db.execute(
        select(LocalModel.id, LocalModel.revision, LocalModel.variant)
        .where(LocalModel.repo_key == repo_id.strip().casefold())
        .order_by(LocalModel.created_at.desc(), LocalModel.id)
    )
    return tuple(preflight.Installed(row.id, row.revision, row.variant) for row in rows)


async def add_model(
    db: AsyncSession, found: preflight.Preflight, user_id: uuid.UUID | None
) -> LocalModel:
    """Make the row for a model the check has just passed. `Refusal` otherwise.

    `found` must be the check run by this request, at the build that will be
    downloaded: what is stored is what it read, and nothing the browser
    sent. A check that found a problem adds nothing. A build that already
    has a row is refused by the database itself, so two requests that arrive
    together cannot both add it. The row starts as `downloading`.
    """
    if found.problems:
        raise Refusal(found.summary)
    variant = found.variant
    repo_id = found.info.repo_id
    if variant is None or found.runtime is None or not variant.files:
        raise Refusal(NOTHING_TO_ADD)
    if len(repo_id) > _LONGEST_REPO_ID or len(variant.id) > _LONGEST_VARIANT:
        # Too long to store, and these are keys, so they are never cut: on
        # PostgreSQL the insert would fail and take the caller's transaction
        # (and its audit row) with it.
        raise Refusal(TOO_LONG)

    model_id = uuid.uuid4()
    await db.execute(
        _insert_once(db, LocalModel).values(
            id=model_id,
            repo_id=repo_id,
            repo_key=repo_id.casefold(),
            revision=found.info.revision,
            variant=variant.id,
            publisher=_publisher(found),
            chain=[
                {
                    "repo_id": node.repo_id,
                    "publisher": node.publisher,
                    "bases": list(node.bases),
                    "found": node.found,
                }
                for node in found.chain
            ],
            # What follows is only ever shown. Hugging Face sets no length
            # on these, and a column that is too short for one would turn a
            # long licence name into a failed download.
            licence=found.info.licence[:100],
            licence_name=found.info.licence_name[:200],
            licence_url=_fitting(found.info.licence_url, 500),
            gated=found.info.gated[:10],
            runtime=found.runtime,
            takes=list(found.takes),
            files=[
                {
                    "path": file.path,
                    "size": file.size,
                    "sha256": file.sha256,
                    "blob_id": file.blob_id,
                }
                for file in variant.files
            ],
            size_bytes=found.size_bytes,
            memory_gb=found.memory_gb,
            state=DOWNLOADING,
            hub_modified_at=found.info.modified_at,
            added_by=user_id,
        )
    )
    model = await db.get(LocalModel, model_id)
    if model is None:
        # The unique key turned it away: somebody added this build first.
        raise Refusal(preflight.ALREADY_INSTALLED)
    return model


async def remove_problem(db: AsyncSession, model_id: uuid.UUID) -> str | None:
    """None when the model's row may go; else the sentence saying which role is in the way."""
    held: list[str] = []
    waiting: list[str] = []
    for role, row in (await roles(db)).items():
        if row.model_id == model_id:
            held.append(ROLE_TITLES[role])
        elif row.pending_model_id == model_id:
            waiting.append(ROLE_TITLES[role])
    if held:
        return (
            f"It is the {held[0]}. Give that role to another model, or take it away, first."
            if len(held) == 1
            else f"It is the {' and the '.join(held)}. Give those roles to other models, or "
            "take them away, first."
        )
    if waiting:
        return (
            f"It is being made the {' and the '.join(waiting)}: its files are being copied to "
            "this machine. Cancel that first."
        )
    return None


async def delete_model(db: AsyncSession, model_id: uuid.UUID) -> None:
    """Delete a model's row. `Refusal` while it holds, or is waiting for, a role.

    The rule is in the statement, not in the foreign keys: they would
    quietly empty the role instead (and SQLite, which the tests and a
    developer's copy run on, does not enforce them at all). Jobs that were
    about the model stay as history, no longer pointing at it. Files are
    the library's to remove, before this is called. A row already gone is
    not an error.
    """
    in_the_way = await remove_problem(db, model_id)
    if in_the_way:
        raise Refusal(in_the_way)
    result: Any = await db.execute(
        delete(LocalModel)
        .where(
            LocalModel.id == model_id,
            ~select(LocalAiRole.role)
            .where((LocalAiRole.model_id == model_id) | (LocalAiRole.pending_model_id == model_id))
            .exists(),
        )
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        # Nothing went: either the row was already gone, or a role was given
        # between the look above and the statement.
        in_the_way = await remove_problem(db, model_id)
        if in_the_way:
            raise Refusal(in_the_way)
    await db.execute(
        update(LocalAiJob)
        .where(LocalAiJob.model_id == model_id)
        .values(model_id=None)
        .execution_options(synchronize_session=False)
    )
    known = db.identity_map.get(identity_key(LocalModel, model_id))
    if known is not None:
        db.expunge(known)


async def note_loaded(db: AsyncSession, model_id: uuid.UUID, runtime: str, version: str) -> None:
    """Remember which program, at which version, last loaded the model."""
    await db.execute(
        update(LocalModel)
        .where(LocalModel.id == model_id)
        .values(last_loaded_with=f"{runtime}/{version}"[:60])
        .execution_options(synchronize_session=False)
    )
    await _reread(db, LocalModel, model_id)


async def note_memory(db: AsyncSession, model_id: uuid.UUID, seen_gb: float) -> None:
    """Record how much memory an answer was seen to use: the highest figure ever, never
    the last. A short test uses less than a long prompt, and the budgets
    must be judged against the most the model has been seen to need."""
    if not math.isfinite(seen_gb) or seen_gb <= 0 or seen_gb > _MOST_MEMORY_GB:
        return
    await db.execute(
        update(LocalModel)
        .where(
            LocalModel.id == model_id,
            LocalModel.measured_memory_gb.is_(None) | (LocalModel.measured_memory_gb < seen_gb),
        )
        .values(measured_memory_gb=seen_gb)
        .execution_options(synchronize_session=False)
    )
    await _reread(db, LocalModel, model_id)


# --- roles -----------------------------------------------------------------------


async def roles(db: AsyncSession) -> dict[str, LocalAiRole]:
    """Both roles, as the database has them now. Reading writes nothing.

    A role nobody has ever been given has no row yet; it is answered with an
    empty one that belongs to no session. Change a role only through
    `set_role`, `complete_role` and `drop_pending`: an attribute set on what
    this returns is at best not saved.
    """
    rows = await db.execute(select(LocalAiRole).execution_options(populate_existing=True))
    stored = {row.role: row for row in rows.scalars()}
    return {role: stored.get(role) or LocalAiRole(role=role) for role in ROLES}


async def holders(db: AsyncSession) -> dict[uuid.UUID, list[str]]:
    """For each model that has a role or is waiting for one: which roles."""
    held: dict[uuid.UUID, list[str]] = {}
    for role, row in (await roles(db)).items():
        for model_id in dict.fromkeys((row.model_id, row.pending_model_id)):
            if model_id is not None:
                held.setdefault(model_id, []).append(role)
    return held


async def models_per_runtime(db: AsyncSession) -> dict[str, int]:
    """How many models added run on each runtime, by kind name, whatever their state.

    Downloading, incomplete and dropped ones count: a runtime is needed from
    the moment a model that runs on it is added, not once it has arrived. The
    page's runtime rows and `framefound doctor` both read this, so the two
    never give different figures.
    """
    rows = await db.execute(select(LocalModel.runtime, func.count()).group_by(LocalModel.runtime))
    return {str(runtime): int(count) for runtime, count in rows.all()}


async def role_models(db: AsyncSession) -> list[library.ModelFiles]:
    """Every model that has a role or is waiting for one, as the library
    needs it (`snapshot`): what must keep a copy on this machine when the
    library leaves it (`library.keep_on_this_machine`). A row whose file
    list cannot be read is left out: it could not be loaded either."""
    found: list[library.ModelFiles] = []
    for model_id in await holders(db):
        model = await get_model(db, model_id)
        if model is None:
            continue
        try:
            found.append(snapshot(model))
        except ValueError:
            continue
    return found


async def set_role(
    db: AsyncSession,
    role: str,
    model: LocalModel | None,
    *,
    pending: bool,
    user_id: uuid.UUID | None,
) -> None:
    """Give a role, line a model up for it, or take it away. Checks nothing:
    ask `role_problem` first.

    - `model=None`: the role is empty, and nothing is waiting for it.
    - `pending=False`: the model has the role from now, given by `user_id`,
      and nothing is waiting for it.
    - `pending=True`: the model gets the role once `complete_role` says its
      copy is on this machine. Until then whoever has the role keeps it, so
      a copy that fails half-way leaves the role where it was. Only the
      waiting model is written; who asked is on the job that makes the copy.
    """
    _known(role)
    values: dict[str, Any]
    if model is None:
        values = {
            "model_id": None,
            "pending_model_id": None,
            "assigned_at": None,
            "assigned_by": None,
        }
    elif pending:
        values = {"pending_model_id": model.id}
    else:
        values = {
            "model_id": model.id,
            "pending_model_id": None,
            "assigned_at": datetime.now(UTC),
            "assigned_by": user_id,
        }
    await db.execute(_insert_once(db, LocalAiRole).values(role=role))
    await db.execute(
        update(LocalAiRole)
        .where(LocalAiRole.role == role)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await _reread(db, LocalAiRole, role)


async def complete_role(
    db: AsyncSession, role: str, model_id: uuid.UUID, *, user_id: uuid.UUID | None = None
) -> Handover:
    """The model's copy is on this machine: give it the role it was waiting for.

    One statement, which changes the row only if the role is still waiting
    for this very model. If the operator chose another model or took the
    role away while the copy was being made, nothing changes and the answer
    says so. `user_id` is who asked for the change (the job's
    `requested_by`), recorded as who gave the role.
    """
    _known(role)
    # Read first, for the answer only. Everything that changes `model_id`
    # also clears `pending_model_id`, so if this figure goes stale before
    # the statement runs, the statement matches nothing and it is not used.
    previous = (
        await db.execute(select(LocalAiRole.model_id).where(LocalAiRole.role == role))
    ).scalar_one_or_none()
    result: Any = await db.execute(
        update(LocalAiRole)
        .where(LocalAiRole.role == role, LocalAiRole.pending_model_id == model_id)
        .values(
            model_id=model_id,
            pending_model_id=None,
            assigned_at=datetime.now(UTC),
            assigned_by=user_id,
        )
        .execution_options(synchronize_session=False)
    )
    await _reread(db, LocalAiRole, role)
    if not result.rowcount:
        return Handover(False)
    return Handover(True, previous)


async def drop_pending(db: AsyncSession, role: str, model_id: uuid.UUID) -> bool:
    """Stop the role waiting for this model (its copy was cancelled or failed).

    Guarded like `complete_role`: if the role is by now waiting for another
    model, that one is left waiting. True when something changed. Whoever
    has the role keeps it.
    """
    _known(role)
    result: Any = await db.execute(
        update(LocalAiRole)
        .where(LocalAiRole.role == role, LocalAiRole.pending_model_id == model_id)
        .values(pending_model_id=None)
        .execution_options(synchronize_session=False)
    )
    await _reread(db, LocalAiRole, role)
    return bool(result.rowcount)


# --- the parts ---------------------------------------------------------------------


def _known(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"There is no role called {role!r}.")


def _insert_once(db: AsyncSession, table: type[LocalModel] | type[LocalAiRole]) -> Any:
    """An INSERT that does nothing when the row is already there.

    The database decides, in the one statement, so nothing has to be read
    first and no error has to be caught: a refused INSERT caught afterwards
    would have ended the caller's whole transaction with it.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql.insert(table).on_conflict_do_nothing()
    if dialect == "sqlite":
        return sqlite.insert(table).on_conflict_do_nothing()
    raise RuntimeError(f"FrameFound runs on PostgreSQL or SQLite, not {dialect}.")


async def _reread(db: AsyncSession, table: type[LocalModel] | type[LocalAiRole], key: Any) -> None:
    """Bring this session's copy of a row up to date after a statement changed it.

    The statements above go straight to the database, so an object this
    session loaded earlier would otherwise keep the old values and the
    caller would act on them.
    """
    known = db.identity_map.get(identity_key(table, key))
    if known is not None:
        await db.refresh(known)


def _chain(model: LocalModel) -> tuple[tuple[policy.Node, ...], bool]:
    """The stored chain, and whether all of it could be read."""
    stored: object = model.chain
    if not isinstance(stored, list):
        return (), False
    chain: list[policy.Node] = []
    whole = True
    for entry in stored:
        node = _node(entry)
        if node is None:
            whole = False
        else:
            chain.append(node)
    return tuple(chain), whole


def _node(entry: object) -> policy.Node | None:
    if not isinstance(entry, dict):
        return None
    repo_id = entry.get("repo_id")
    publisher = entry.get("publisher")
    bases = entry.get("bases")
    found = entry.get("found")
    if not isinstance(repo_id, str) or not repo_id.strip():
        return None
    if publisher is not None and not isinstance(publisher, str):
        return None
    if not isinstance(bases, list) or not all(isinstance(base, str) for base in bases):
        return None
    if not isinstance(found, bool):
        return None
    return policy.Node(repo_id, publisher, tuple(bases), found)


def _file(entry: object) -> RepoFile:
    if not isinstance(entry, dict):
        raise ValueError(BAD_FILES_RECORD)
    path = entry.get("path")
    size = entry.get("size")
    sha256 = entry.get("sha256")
    blob_id = entry.get("blob_id")
    if not isinstance(path, str) or not safe_path(path):
        raise ValueError(BAD_FILES_RECORD)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError(BAD_FILES_RECORD)
    if not _hex_or_none(sha256, _SHA256_LENGTH) or not _hex_or_none(blob_id, _BLOB_ID_LENGTH):
        raise ValueError(BAD_FILES_RECORD)
    return RepoFile(path=path, size=size, sha256=sha256, blob_id=blob_id)


def _hex_or_none(value: object, length: int) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, str)
        and len(value) == length
        and all(letter in "0123456789abcdef" for letter in value)
    )


def _publisher(found: preflight.Preflight) -> str:
    """The publisher to show: as Hugging Face reports it, else the first part of the name.

    The two are the same on every page seen. Approval is never judged from
    this column — it is judged from the chain — so a reported name too long
    to store is replaced by the one in the model's own name.
    """
    reported = (found.info.publisher or "").strip()
    named = policy.publisher_of(found.info.repo_id) or ""
    return reported if reported and len(reported) <= 100 else named[:100]


def _fitting(text: str | None, length: int) -> str | None:
    """A link is kept whole or not at all: half an address leads somewhere else."""
    return text if text is not None and len(text) <= length else None
