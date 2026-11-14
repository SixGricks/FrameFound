"""The chat panel's tool for the Looker: `start_folder_review` (design L3 3.11, 8.10).

Claude, in the panel, can start what the "Review with AI" dialog starts: one
folder (or a whole library) looked at by the local model on this machine,
now or tonight. Nothing new happens underneath. `start` is `reviews.start`,
the commit, the lane's wake-up and the audit row, in the order
`POST /looks/reviews` does them, so a review asked for in the chat leaves
the rows one asked for on the page leaves, is refused in the same words,
and then shows on Processing like any other.

The operator is asked first, every time (`confirm=True`): a review is hours
of model time and of making playable copies. The chip's sentence comes from
the arguments alone — C1's rule is that nothing is looked up for it — so it
says what would be reviewed and when; how much there is to look at is in
the tool's answer, after the yes.

Only an administrator can start one, here as on the page (D24).

The tool is the panel's alone. It is registered with C1's registry
(`chat.tools.register`, no file of C1's edited) and never added to the
connector (`mcp.server.build_server()`), whose tool list is pinned. It is
registered once, at import, and only where models can run: the tool list
then stays the same for every conversation on one install, which is what
C1's stored tool lists and Claude's prompt cache rely on, and the container
server does not offer Claude a tool that could only ever answer "not on this
install". `api/v1/looks.py` imports this module, so the tool is there
whenever the API is.

No `anthropic` import, and the optional `mcp` package is never needed: the
work is done by `start`, which takes a session and a user, and `run` reads
who is asking from `framefound.mcp.auth` (which does not import that
package) only when the panel calls it.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.auth import service as auth_service
from framefound.chat import tools
from framefound.config import Settings
from framefound.db.models import Library, LookRequest, User
from framefound.local_models import UNAVAILABLE, availability, comparisons
from framefound.local_models.deps import Deps, deps_for
from framefound.local_models.settings_store import load_config
from framefound.looking import estimate, reviews, scope
from framefound.media import folders

log = structlog.get_logger()

NAME = "start_folder_review"
DESCRIPTION = (
    "Start a review of one folder (or a whole library) by the local AI model on this machine: it "
    "looks at every photograph and shot that has not been looked at, suggests tags for the review "
    "queue, and gives each a caption and a 0-100 use score. Free, slow, and nothing leaves the "
    "building. The operator is asked before it starts, in a sentence that shows `library` as you "
    "wrote it: give the library's name as Browse shows it, and its id only when you do not know "
    "the name. `folder` is the path inside the library as Browse shows it; leave it empty for the "
    'whole library. `when` is "now" or "tonight".'
)
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "library": {
            "type": "string",
            # The name first: the operator is shown this value as written
            # and cannot recognise a library by its id.
            "description": (
                "The library's name as Browse shows it (its id only if the name is not known)"
            ),
        },
        "folder": {"type": "string", "default": ""},
        "when": {"type": "string", "enum": ["now", "tonight"], "default": "now"},
        "redo": {
            "type": "boolean",
            "default": False,
            "description": "Also look again at items a different model already looked at",
        },
    },
    "required": ["library"],
}

# The sentences Claude reads when a call is wrong. Written for Claude, who
# can fix the call; the person sees them only through what Claude says.
NO_LIBRARY = (
    "Say which library: its name as Browse shows it (its id only if the name is not known)."
)
BAD_REDO = "redo is true or false."
# Where the operator watches a review once it has started.
FOLLOW = "The operator can follow it on Processing (/processing)."
PAUSED = "Local AI is paused. This will wait until it is resumed."

Wake = Callable[[], Awaitable[bool]]


def offered(settings: Settings | None = None) -> bool:
    """Whether this install can run models at all (L1's `availability`): the
    tool is offered only there."""
    return availability(settings).ok


# --- the confirm chip ---------------------------------------------------------------


def _when(args: Mapping[str, Any]) -> Any:
    """When the review was asked for: "now" when nothing was said. Anything
    else is kept as it came, for `reviews.start` to accept or refuse; the
    chip reads the same value, so it never promises a time the tool would
    not keep."""
    when = args.get("when")
    return "now" if when is None else when


def _where(library: str, folder: str) -> str:
    """A library and a folder inside it as the job's subject writes them;
    the whole library when there is no folder."""
    return f"{library} / {folder}" if folder else f"all of {library}"


def _named(library: Any) -> str:
    """The library as the chip names it: as Claude wrote it, since nothing
    is looked up. An id is said to be one — "library 3f2a8c1e-…" — because
    the operator cannot recognise a library by it, and without the word it
    reads as a name nobody gave anything. The tool's description asks
    Claude for the name, so this is the rare case."""
    if not isinstance(library, str) or not library.strip():
        return "?"
    written = library.strip()
    try:
        uuid.UUID(written)
    except ValueError:
        return written
    return f"library {written}"


def describe(args: Mapping[str, Any]) -> str:
    """What saying yes would start, for the confirm chip: one sentence from
    the arguments alone. Nothing is looked up (C1's rule), so the library
    reads as Claude wrote it (`_named`) and nothing is counted; the tool's
    answer does that, after the yes.

    The folder is shown as `start` will read it (slashes at either end
    dropped), so the chip and what then happens name the same folder."""
    named = _named(args.get("library"))
    folder = args.get("folder")
    try:
        inside = folders.clean_folder("" if folder is None else folder)
    except folders.FolderError:
        # Not a folder the catalogue could hold: shown as it was asked, and
        # refused with the reason if the operator says yes.
        inside = str(folder)
    when = _when(args)
    if when == "tonight":
        timing = "tonight"
    elif when == "now":
        timing = "starting now"
    else:
        # Neither: said as it was asked, and refused if the operator says yes.
        timing = f'starting "{when}"'
    again = ", looking again at what another model saw" if args.get("redo") is True else ""
    # No full stop, as C1's own chips have none: the panel reads several out
    # as one list.
    return f"Review {_where(named, inside)} with the local model, {timing}{again}"


# --- the arguments ------------------------------------------------------------------


async def _library(db: AsyncSession, given: Any) -> Library:
    """The library Claude named: by id, or by name without regard to case.
    `ToolProblem` with the page's sentence, and the names there are, when
    there is none — the page picks from a list and Claude typed a name."""
    if not isinstance(given, str) or not given.strip():
        raise tools.ToolProblem(NO_LIBRARY)
    wanted = given.strip()
    try:
        by_id = await db.get(Library, uuid.UUID(wanted))
    except ValueError:
        by_id = None
    if by_id is not None:
        return by_id
    matches = list(
        await db.scalars(
            select(Library).where(
                # The name as written is asked for by itself as well: how a
                # database lowers a letter with an accent is its own affair,
                # and a name written exactly must be found whatever it does.
                (Library.name == wanted) | (func.lower(Library.name) == wanted.lower())
            )
        )
    )
    # Two libraries may differ only in their capitals: the one written
    # exactly is the one meant, and neither is guessed at otherwise.
    exact = [library for library in matches if library.name == wanted]
    if len(exact) == 1 or len(matches) == 1:
        return (exact or matches)[0]
    names = sorted(library.name for library in matches)
    if names:
        raise tools.ToolProblem(
            f"More than one library is called that apart from its capitals: {', '.join(names)}. "
            "Give the name exactly, or the id."
        )
    every = ", ".join(sorted(await db.scalars(select(Library.name))))
    there_are = f"The libraries are: {every}." if every else "There are no libraries yet."
    raise tools.ToolProblem(f"{reviews.NO_SUCH_LIBRARY} {there_are}")


def _redo(args: Mapping[str, Any]) -> bool:
    """Only a real true looks again: the word "false" in quotes is not a
    yes to hours of work that were already done."""
    redo = args.get("redo")
    if redo is None:
        return False
    if not isinstance(redo, bool):
        raise tools.ToolProblem(BAD_REDO)
    return redo


# --- the answer ---------------------------------------------------------------------


def _count(number: int, one: str) -> str:
    return f"{number:,} {one}" if number == 1 else f"{number:,} {one}s"


def _todo(counts: scope.Counts) -> str:
    """What the review has to look at, in the dialog's words: "214
    photographs and 37 clips (291 shots)", and the clips that come later."""
    parts: list[str] = []
    if counts.photos:
        parts.append(_count(counts.photos, "photograph"))
    if counts.clips:
        parts.append(f"{_count(counts.clips, 'clip')} ({_count(counts.shots, 'shot')})")
    if counts.stills_only:
        parts.append(f"{_count(counts.stills_only, 'clip')} judged from one still")
    if parts:
        listed = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
        said = f"To look at: {listed}."
    else:
        said = "Nothing is ready to look at yet."
    unready = estimate.to_make_ready(counts)
    if unready:
        clips = "1 more clip has" if unready == 1 else f"{unready:,} more clips have"
        said += (
            f" {clips} to be made ready first; each is looked at when it is, or judged from "
            "one still if it cannot be."
        )
    return said


async def _told(db: AsyncSession, request: LookRequest, asked: reviews.Asked, deps: Deps) -> str:
    """A review that started, as Claude is told it: the folder, the model,
    what there is to look at, about how long (the dialog's own estimate, in
    its own words), and where the operator follows it."""
    shown = await reviews.review(db, request, deps.now())
    tonight = shown["state"] == "tonight"
    speed = estimate.NONE if asked.model is None else await estimate.speed(db, asked.model)
    how_long = estimate.words(
        estimate.seconds_for(asked.counts, speed),
        speed,
        unready=estimate.to_make_ready(asked.counts),
    )
    said = [
        f"{'Set for tonight' if tonight else 'Started'}: "
        f"{_where(asked.library_name, request.folder)}, with {request.model_name}.",
        _todo(asked.counts),
    ]
    if tonight:
        # "Starts tonight at 21:30."
        said.append(str(shown["message"]))
    if (await load_config(db)).paused:
        said.append(PAUSED)
    said.append(how_long)
    if shown["demo"]:
        # The dialog's amber line: Claude must not report made-up captions
        # and scores as a model's judgement.
        said.append(reviews.DEMO_LINE)
    said.append(FOLLOW)
    return " ".join(said)


# --- the tool -----------------------------------------------------------------------


def _lane(settings: Settings) -> Wake:
    """Waking the real lane, off the event loop: reaching the broker blocks
    (it retries when the queue is down)."""

    def wake() -> Awaitable[bool]:
        # Imported here: that module pulls in every local-AI handler, which
        # the chip's sentence and the tool's definition have no use for.
        from framefound.processing import local_ai_tasks

        return asyncio.to_thread(local_ai_tasks.wake, settings)

    return wake


async def start(
    db: AsyncSession,
    user: User,
    args: Mapping[str, Any],
    *,
    deps: Deps | None = None,
    wake: Wake | None = None,
) -> str:
    """Start the review the arguments ask for, as `user`: the text Claude is
    told. Raises `tools.ToolProblem` with the sentence when it does not
    start: not an administrator, models cannot run here, a library there is
    none of, and every `reviews.ReviewRefused` in its own words.

    What `POST /looks/reviews` does, in its order: `reviews.start`, what it
    counted (before the lane is woken), the commit, the wake-up — taken back
    through `reviews.withdraw` when the lane cannot be woken and has none of
    its jobs yet — then the audit row and its commit. `deps` is the outside
    world (the clock, the runtime kinds) and `wake` the lane's wake-up; a
    test hands in both, None is the real ones."""
    deps = deps or deps_for()
    # The route's order: who is asking, then whether this install can at all.
    if user.disabled or user.role != "admin":
        raise tools.ToolProblem(reviews.ADMIN_ONLY)
    found = availability(deps.settings)
    if not found.ok:
        raise tools.ToolProblem(found.reason or UNAVAILABLE)
    # The programs not installed here, as the route asks them: a model on one
    # is refused in the route's words, not started to fail at once (the
    # chat was the last way left to start such a review). Asks the disk, so
    # off the event loop.
    missing = await asyncio.to_thread(comparisons.missing_programs, deps.kinds, deps.settings)
    redo = _redo(args)
    library = await _library(db, args.get("library"))
    folder = args.get("folder")
    try:
        started = await reviews.start(
            db,
            user,
            library_id=library.id,
            # A folder or a `when` that is not one is refused in there, with
            # the sentence the page shows.
            folder="" if folder is None else folder,
            when=_when(args),
            # The Looker: choosing another model is the dialog's.
            model_id=None,
            redo=redo,
            # As the dialog's box is by default: clips are made ready.
            prepare=True,
            now=deps.now(),
            known_kinds=tuple(deps.kinds),
            missing=missing,
        )
    except reviews.ReviewRefused as err:
        raise tools.ToolProblem(str(err)) from None
    asked = await reviews.asked(db, started)
    request_id = started.id
    await db.commit()
    woken = await (wake or _lane(deps.settings))()
    # Exactly as the route: taken back, unless the lane has it already.
    if not woken and await reviews.withdraw(db, request_id):
        await db.commit()
        raise tools.ToolProblem(reviews.QUEUE_DOWN)
    again = await db.get(LookRequest, request_id, populate_existing=True)
    if again is None:
        # Removed with its library in the moment since: nothing was started.
        raise tools.ToolProblem(reviews.NO_SUCH_LIBRARY)
    await auth_service.audit(db, reviews.AUDIT_STARTED, actor_user_id=user.id, detail=asked.detail)
    await db.commit()
    log.info("looks.chat_started", request_id=str(request_id), when=again.run_when)
    return await _told(db, again, asked, deps)


async def run(args: dict[str, Any]) -> str:
    """C1's entry point: the panel calls it, after the operator said yes,
    with the signed-in user bound. A refusal is `tools.ToolProblem`, which
    the panel hands to Claude as an error result with the sentence."""
    # The connector's caller and session, read only here: that module does
    # not import the optional `mcp` package, and a test calls `start` with
    # neither bound.
    from framefound.mcp.auth import open_session, principal

    who = principal()
    async with open_session() as db:
        # The account as it is now, not as it was when the turn began.
        user = await db.get(User, who.user_id)
        if user is None:
            raise tools.ToolProblem(reviews.ADMIN_ONLY)
        return await start(db, user, args)


def register(settings: Settings | None = None) -> bool:
    """Give the panel the tool, where models can run. Returns whether it did.
    Run once, at import; never per user or per request."""
    if not offered(settings):
        return False
    tools.register(
        tools.ChatTool(
            name=NAME,
            description=DESCRIPTION,
            input_schema=INPUT_SCHEMA,
            run=run,
            confirm=True,
            describe=describe,
        )
    )
    return True


register()
