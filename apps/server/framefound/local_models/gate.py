"""The Compare question (design L6 D11, 3.4): asked once, when a role is given.

A model that runs on a program marked `needs_compare` (llama.cpp) arrives
with nothing to show for itself: nobody here has seen how it answers. So
when it is given a role without having been through Compare on the
operator's footage, the API asks first (428), and one click goes ahead.
It is a question, never a lock: nothing here refuses anything, and nothing
is stored — "not compared yet" is worked out again each time it is shown and
stops being true by itself once a comparison finishes.

What counts as having been seen:

- for the Looker, a finished Compare column of this installed model
  (`comparisons.last_finished_for`, L2's one answer to "has it been
  through Compare, and when?");
- for the Editor, that, or else a finished text Test whose answer came back
  in the form asked: Compare measures looking, and there is no Compare of
  drafting a rough cut. Only a model that does not read pictures is ever
  given a text Test (the Test shows a picture model a picture), so for a
  model that reads pictures only Compare counts, and the Editor's question
  says so rather than pointing at a Test it cannot have.

A model on any other program (MLX-VLM) is never asked: its kind does not
ask, so `needed` is False and the evidence counts as seen.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import LocalAiJob, LocalModel
from framefound.local_models import ROLES
from framefound.local_models.kinds import RuntimeKind

LOOKER_QUESTION = (
    "{name} runs on {program} and has not been through Compare on your footage, so nobody "
    "has seen how well it looks at your pictures. Make it the Looker anyway?"
)
EDITOR_QUESTION = (
    "{name} runs on {program} and has not been through Compare or a text Test, so nobody "
    "has seen an answer from it. Make it the Editor anyway?"
)
# For a model that reads pictures: its Test is always a picture Test, which
# says nothing about drafting, so only Compare is named.
EDITOR_QUESTION_PICTURES = (
    "{name} runs on {program} and has not been through Compare on your footage, so nobody "
    "has seen an answer from it. Make it the Editor anyway?"
)
QUESTIONS = {"looker": LOOKER_QUESTION, "editor": EDITOR_QUESTION}

log = structlog.get_logger()

By = Literal["compare", "test", ""]
# What the audit row says about the question (6.4).
Said = Literal["seen", "skipped", "not_needed"]


@dataclass(frozen=True)
class Evidence:
    """What is known of how a model answers, for one role."""

    needed: bool  # the model's program asks for it
    seen: bool  # there is evidence (always True when it is not needed)
    by: By  # what the evidence is; "" when there is none or none is asked for
    at: datetime | None  # when, in UTC
    question: str  # the 428's message when needed and not seen; else ""

    def said(self) -> Said:
        """The audit row's word for how the role was given (once it was)."""
        if not self.needed:
            return "not_needed"
        return "seen" if self.seen else "skipped"

    def asks(self, skipped: bool) -> bool:
        """Whether giving the role must stop and ask first."""
        return self.needed and not self.seen and not skipped


NOT_NEEDED = Evidence(needed=False, seen=True, by="", at=None, question="")


def _utc(moment: datetime | None) -> datetime | None:
    # SQLite hands back what was written without its zone; local AI writes UTC.
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


_Finder = Callable[[AsyncSession, uuid.UUID], Awaitable[datetime | None]]
_warned: set[str] = set()


def _gone(why: str) -> None:
    # Once per process: the Models page asks this for every model it shows.
    if why not in _warned:
        _warned.add(why)
        log.warning("local_models.gate_compare_unknown", why=why)


async def _compared(db: AsyncSession, model_id: uuid.UUID) -> datetime | None:
    """The one adapter to L2: when a Compare column of this model last finished
    with answers. Imported here, not at the top: should L2's module be
    missing, its function renamed or given another shape, the answer is
    "never", so the question is asked — one click still answers it — rather
    than skipped, turned into a refusal or into a 500 on the Models page
    (P17). A failure while the function runs is not hidden: that is a broken
    database, not a renamed function."""
    try:
        from framefound.local_models import comparisons
    except ImportError:
        _gone("comparisons cannot be imported")
        return None
    finder: _Finder | None = getattr(comparisons, "last_finished_for", None)
    if not callable(finder):
        _gone("comparisons.last_finished_for is missing")
        return None
    try:
        # Calling an async function only binds its arguments; a TypeError
        # here is a changed signature, never something the query raised.
        pending = finder(db, model_id)
    except TypeError:
        _gone("comparisons.last_finished_for takes other arguments")
        return None
    return await pending


async def _tested_as_text(db: AsyncSession, model_id: uuid.UUID) -> datetime | None:
    """When this model last finished a text Test whose answer was in the form
    asked (`structured_ok`); None if it never has. The result is JSON the
    Test's handler wrote, so it is read here rather than queried into."""
    rows = await db.scalars(
        select(LocalAiJob)
        .where(
            LocalAiJob.kind == "test",
            LocalAiJob.model_id == model_id,
            LocalAiJob.state == "done",
        )
        .order_by(LocalAiJob.finished_at.desc(), LocalAiJob.created_at.desc())
    )
    for job in rows:
        result = job.result if isinstance(job.result, dict) else {}
        if result.get("kind") == "text" and result.get("structured_ok") is True:
            return _utc(job.finished_at or job.created_at)
    return None


def _needed(model: LocalModel, kinds: Mapping[str, RuntimeKind]) -> RuntimeKind | None:
    kind = kinds.get(model.runtime)
    return kind if kind is not None and kind.needs_compare else None


def _name(model: LocalModel) -> str:
    return model.repo_id.rsplit("/", 1)[-1]


def _reads_pictures(model: LocalModel) -> bool:
    takes = model.takes if isinstance(model.takes, list) else []
    return "image" in takes


def _question(model: LocalModel, role: str, kind: RuntimeKind) -> str:
    words = QUESTIONS[role]
    if role == "editor" and _reads_pictures(model):
        words = EDITOR_QUESTION_PICTURES
    return words.format(name=_name(model), program=kind.label)


async def evidence(
    db: AsyncSession, model: LocalModel, role: str, kinds: Mapping[str, RuntimeKind]
) -> Evidence:
    """What is known of how `model` answers, judged for `role`."""
    return (await for_roles(db, model, kinds, (role,)))[role]


async def for_roles(
    db: AsyncSession,
    model: LocalModel,
    kinds: Mapping[str, RuntimeKind],
    roles: tuple[str, ...] = ROLES,
) -> dict[str, Evidence]:
    """`evidence` for several roles of one model, asking the database once each
    for Compare and the Test. Nothing is asked of a model whose program does
    not ask, and no Test is looked for on a model that reads pictures: it is
    only ever given a picture Test, which does not count."""
    for role in roles:
        if role not in QUESTIONS:
            raise ValueError(f"no such role: {role}")
    kind = _needed(model, kinds)
    if kind is None:
        return dict.fromkeys(roles, NOT_NEEDED)
    compared = await _compared(db, model.id)
    tested: datetime | None = None
    if compared is None and "editor" in roles and not _reads_pictures(model):
        tested = await _tested_as_text(db, model.id)
    found: dict[str, Evidence] = {}
    for role in roles:
        by: By = ""
        at: datetime | None = None
        if compared is not None:
            by, at = "compare", compared
        elif role == "editor" and tested is not None:
            by, at = "test", tested
        found[role] = Evidence(
            needed=True,
            seen=by != "",
            by=by,
            at=at,
            question="" if by else _question(model, role, kind),
        )
    return found


def shown_role(model: LocalModel) -> str:
    """The role a model row's "compared" is judged for: the Looker for a model
    that reads pictures, the Editor for one that does not (6.3)."""
    return "looker" if _reads_pictures(model) else "editor"
