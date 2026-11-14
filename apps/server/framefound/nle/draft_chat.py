"""The chat panel's tool for the local Editor: `local_cut_draft` (design L5 3.5, 6.12).

Claude, in the panel, can ask the Editor (an open-weight model on this
machine, at no cost) for a rough cut and read how the request went. Nothing
new happens underneath: `start` is `drafts.request_draft` with origin "chat",
exactly what the Cuts page's form does, and the draft then shows on that
page like any other. The tool waits up to half a minute for a quick draft
and otherwise says where it is; a draft that waits for the overnight window
answers at once with its sentence (P20), never after the whole draft.

The tool is the panel's alone. It is registered with C1's registry
(`chat.tools.register`, no file of C1's edited) and never added to the
connector (`mcp.server.build_server()`), whose tool list is pinned (P19).
It is registered once, at import, and only where models can run (D16): the
tool list then stays the same for every conversation on one install, which
is what C1's stored tool lists and Claude's prompt cache rely on (P18).
`api/v1/cuts.py` imports this module, so the tool is there whenever the API is.

No `anthropic` import, and the optional `mcp` package is never needed: the
work is done by `local_cut_draft`, which takes a session and a user id (P3),
and `run` reads who is asking from `framefound.mcp.auth` (which does not
import that package) only when the panel calls it.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.chat import tools
from framefound.config import Settings
from framefound.db.models import Cut, CutDraft, CutPiece, Library, User
from framefound.local_models import availability
from framefound.nle import drafts, roughcut

log = structlog.get_logger()

NAME = "local_cut_draft"
# How long `start` waits for a quick draft inside Claude's turn, and how
# often it looks. The panel gives a tool three minutes (chat.engine's
# TOOL_TIMEOUT_S); a draft can take far longer, so this is a glance, not a wait.
WAIT_S = 30.0
POLL_S = 2.0

TOOL: dict[str, Any] = {
    "name": NAME,
    "description": (
        "Ask the local Editor - an open-weight model running on FrameFound's own machine, at "
        "no cost - to draft a rough cut from a brief, or read a draft it was asked for "
        "earlier. Use it when the person asks for a local draft, a free first pass, or says "
        'to save the Claude budget; do not use it to check your own work. action "start" '
        "takes the brief and the footage (a folder name as find_shots takes it, with an "
        "optional library, or a list of clip ids) and a length in seconds; the Editor chooses "
        "from the analysed shots, FrameFound checks every in and out point, and the result is "
        "saved as a local draft on the Cuts page. It can be slow: the answer says so and gives "
        "a draft id when it is not ready within half a minute, and a large model may only run "
        'overnight. action "read" takes that draft id and returns where it stands, or the '
        "finished cut as numbered lines with the clip ids, in and out points and the Editor's "
        "reason for each. A local draft is a suggestion: to improve one, call build_rough_cut "
        "with parent set to the cut id this tool returns."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "read"],
                "description": "start a new draft, or read one",
            },
            "brief": {
                "type": "string",
                "description": "start: what the video should be, in the person's words",
            },
            "folder": {
                "type": "string",
                "description": "start: a course or folder name, matched anywhere in the path",
            },
            "library": {
                "type": "string",
                "description": "start: a library name, to narrow the folder",
            },
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "start: clip ids (short or full) instead of a folder",
            },
            "length_s": {
                "type": "number",
                "description": "start: the length wanted, in seconds (5 to 600)",
            },
            "title": {"type": "string"},
            "draft": {"type": "string", "description": "read: the draft id that start returned"},
        },
        "required": ["action"],
    },
}

# The sentences Claude reads when a call is wrong. Written for Claude, who
# can fix the call; the person sees them only through what Claude says.
NO_ACTION = 'Say what to do: action "start" (a new draft) or "read" (one asked for earlier).'
NO_BRIEF = "Give the brief: what the video should be, in the person's words."
NO_FOOTAGE = "Name the footage: a folder (with a library if the name is not unique) or clip ids."
BOTH_FOOTAGE = "Give a folder or clip ids, not both."
NO_DRAFT_ID = 'Give the draft id that action "start" returned.'
NO_SUCH_DRAFT = "No such draft."
READ_ONLY = "This account is read-only, so it cannot ask for a draft."
CUT_GONE = (
    "This draft became a cut that has since been deleted. Ask for another draft, or call "
    "recent_cuts for the cuts there are."
)
NOT_A_LIST = "ids is a list of clip ids, as find_shots prints them."
TOO_MANY_IDS = (
    f"ids: at most {drafts.MOST_CLIPS} clips in one draft. Name fewer, or give a folder instead."
)


def offered(settings: Settings | None = None) -> bool:
    """Whether this install can run models at all (L1's `availability`): the
    tool is offered only there (D16)."""
    return availability(settings).ok


# --- the arguments -------------------------------------------------------------


def _text(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise drafts.DraftError(f"{key} is text.")
    return value.strip()


async def _library_id(db: AsyncSession, name: str) -> uuid.UUID | None:
    """A library by name (any case) or id, as find_shots takes it."""
    if not name:
        return None
    try:
        key: uuid.UUID | None = uuid.UUID(name)
    except ValueError:
        key = None
    found = await db.scalar(
        select(Library.id).where(
            (Library.id == key) if key is not None else (func.lower(Library.name) == name.lower())
        )
    )
    if found is None:
        names = ", ".join(sorted(await db.scalars(select(Library.name))))
        raise drafts.DraftError(f"No library called {name!r}. Libraries: {names or 'none'}.", 404)
    return uuid.UUID(str(found))


async def _clip_ids(db: AsyncSession, given: Any) -> list[uuid.UUID]:
    """The clips named, each by the short id the tools print or in full.
    `roughcut.CutError` names the one that is not a clip."""
    if not isinstance(given, list) or not all(isinstance(ref, str) for ref in given):
        raise drafts.DraftError(NOT_A_LIST)
    refs = list(dict.fromkeys(ref.strip() for ref in given if ref.strip()))
    if len(refs) > drafts.MOST_CLIPS:
        # Said before the clips are looked up one by one.
        raise drafts.DraftError(TOO_MANY_IDS)
    found: list[uuid.UUID] = []
    for ref in refs:
        asset = await roughcut.find_asset(db, ref)
        found.append(asset.id)
    return list(dict.fromkeys(found))


async def _request(db: AsyncSession, args: Mapping[str, Any]) -> drafts.DraftRequest:
    """The draft the arguments ask for, or `DraftError` / `CutError` saying
    what is wrong with them. Checked by the same model the page's form is."""
    brief = _text(args, "brief")
    if not brief:
        raise drafts.DraftError(NO_BRIEF)
    folder = _text(args, "folder")
    ids = args.get("ids")
    if ids and folder:
        raise drafts.DraftError(BOTH_FOOTAGE)
    # Built inside the `try`: a limit the Scope sets is Claude's sentence too,
    # never a crash the panel reports as "the tool failed".
    try:
        if ids:
            clips = await _clip_ids(db, ids)
            if not clips:
                raise drafts.DraftError(NO_FOOTAGE)
            scope = drafts.Scope(kind="selection", asset_ids=clips)
        elif folder:
            library_id = await _library_id(db, _text(args, "library"))
            # find_shots' meaning of a folder: these letters anywhere in the
            # path (P21), never the whole library.
            scope = drafts.Scope(kind="folder", library_id=library_id, contains=folder[:200])
        else:
            raise drafts.DraftError(NO_FOOTAGE)
        return drafts.DraftRequest(
            brief=brief,
            # Claude's title is a label, not a contract: shortened, not refused.
            title=drafts.short_title(_text(args, "title")),
            scope=scope,
            target_s=args.get("length_s"),
        )
    except ValidationError as err:
        raise drafts.DraftError(_said(err)) from None


def _said(err: ValidationError) -> str:
    """The first thing wrong with the arguments, as one sentence."""
    first = err.errors()[0]
    where = {"target_s": "length_s", "asset_ids": "ids"}
    field = ".".join(str(part) for part in first.get("loc", ()) if part != "scope")
    field = where.get(field, field) or "the arguments"
    return f"{field}: {first.get('msg', 'not accepted')}."


# --- the answers ---------------------------------------------------------------


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    return text if text.endswith((".", "!", "?", "…")) else f"{text}."


def _seconds(value: float) -> str:
    return f"{round(value, 1):g}"


async def _finished(db: AsyncSession, draft: CutDraft) -> str:
    """A done draft as Claude reads it: the cut, a line a shot, what the
    check changed, and how to keep a better version beside it."""
    cut = None if draft.cut_id is None else await db.get(Cut, draft.cut_id)
    if cut is None:
        raise drafts.DraftError(CUT_GONE, 404)
    pieces = list(
        await db.scalars(
            select(CutPiece).where(CutPiece.cut_id == cut.id).order_by(CutPiece.position)
        )
    )
    shots = "1 shot" if len(pieces) == 1 else f"{len(pieces)} shots"
    title = " ".join(cut.title.split()) or roughcut.DEFAULT_TITLE
    lines = [
        f'Local draft ready: "{title}", {shots}, {_seconds(cut.duration_s)} s. '
        f"Cut id {cut.id} (version {cut.version})."
    ]
    if drafts.demonstration(cut, draft):
        lines.append("This install is a demonstration: its Editor judged nothing.")
    # The same line the review request gives for each piece (number, clip
    # id, clip, source in-out, the Editor's reason on one line).
    lines += [drafts._piece_line(n, piece) for n, piece in enumerate(pieces, start=1)]
    problems = " ".join(" ".join(problem.split()) for problem in draft.problems or [])
    if problems:
        lines.append(f"Changed when it was checked: {problems}")
    lines.append(
        f"The person can open it at /cuts/{cut.id}. To improve it: "
        f'build_rough_cut(parent="{cut.id}", …).'
    )
    return "\n".join(lines)


async def _answer(db: AsyncSession, draft: CutDraft) -> str:
    """Where a draft stands, in one of three answers: the finished cut, "not
    yet" with its sentence and id, or `DraftError` with the sentence it ended on."""
    state, message, stopping = drafts.shown(draft, await drafts.job_of(db, draft))
    if state == "done":
        return await _finished(db, draft)
    if state in drafts.FINISHED or state not in drafts.ACTIVE:
        said = _sentence(message or drafts.STOPPED)
        problems = " ".join(" ".join(problem.split()) for problem in draft.problems or [])
        if problems:
            said += f" {problems}"
        raise drafts.DraftError(said, 409)
    now = _sentence(message)
    if stopping:
        now += " It has been asked to stop."
    return (
        f"The Editor has the brief (draft {draft.id}). {now} Read it later with action "
        '"read", or the person can watch it on the Cuts page.'
    )


async def _draft(db: AsyncSession, ref: str) -> CutDraft:
    """A draft by its id, in full or the short form the tools print."""
    rows = await roughcut._by_id(db, CutDraft, ref)
    if not rows:
        raise drafts.DraftError(NO_SUCH_DRAFT, 404)
    if len(rows) > 1:
        raise drafts.DraftError(f"{ref!r} matches more than one draft; give more of the id.")
    found: CutDraft = rows[0]
    return found


async def _fresh(db: AsyncSession, draft_id: uuid.UUID) -> CutDraft:
    found = await db.get(CutDraft, draft_id, populate_existing=True)
    if found is None:  # cleared from the Cuts page while Claude waited
        raise drafts.DraftError(NO_SUCH_DRAFT, 404)
    return found


# --- the tool ------------------------------------------------------------------


async def local_cut_draft(
    db: AsyncSession,
    user_id: uuid.UUID,
    args: Mapping[str, Any],
    *,
    wait_s: float = WAIT_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wake: Callable[[], bool] | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> str:
    """Run the tool for `user_id`: the text of the tool result. Raises
    `drafts.DraftError` or `roughcut.CutError` (both `ValueError`) with the
    sentence for Claude: a call that cannot be served, or a draft that failed.

    `start` asks for the draft as the Cuts page would (origin "chat"), then
    looks every `POLL_S` for up to `wait_s`: a draft that finishes in that
    time is answered whole, one still under way says where it is and gives
    its id. A draft that has to wait (the overnight window, a pause) answers
    at once. `wake`, `now` and `settings` are `request_draft`'s.
    """
    action = args.get("action")
    if action == "read":
        ref = _text(args, "draft")
        if not ref:
            raise drafts.DraftError(NO_DRAFT_ID)
        return await _answer(db, await _draft(db, ref))
    if action != "start":
        raise drafts.DraftError(NO_ACTION)

    user = await db.get(User, user_id)
    if user is None or user.role == "readonly":
        # The page's form is refused to this account by the API; so is this.
        raise drafts.DraftError(READ_ONLY, 403)
    request = await _request(db, args)
    draft = await drafts.request_draft(
        db, request, user_id=user_id, origin="chat", wake=wake, now=now, settings=settings
    )
    log.info("cut_drafts.chat_started", draft_id=str(draft.id))
    waited = 0.0
    while True:
        draft = await _fresh(db, draft.id)
        state, _message, _stopping = drafts.shown(draft, await drafts.job_of(db, draft))
        # Nothing is held open while Claude's turn waits: the lane writes
        # this draft from another session meanwhile.
        await db.commit()
        if state not in ("queued", "running") or waited >= wait_s:
            return await _answer(db, draft)
        await sleep(POLL_S)
        waited += POLL_S


async def run(args: dict[str, Any]) -> str:
    """C1's entry point: the panel calls it with the signed-in user bound
    (0.2, C-1). A refusal becomes `tools.ToolProblem`, which the panel hands
    to Claude as an error result with the sentence."""
    # The connector's caller and session, read only here: that module does
    # not import the optional `mcp` package, and a test calls
    # `local_cut_draft` with neither bound (P3).
    from framefound.mcp.auth import open_session, principal

    who = principal()
    async with open_session() as db:
        try:
            return await local_cut_draft(db, who.user_id, args)
        except (drafts.DraftError, roughcut.CutError) as err:
            raise tools.ToolProblem(str(err)) from None


def register(settings: Settings | None = None) -> bool:
    """Give the panel the tool, where models can run. Returns whether it did.
    Run once, at import (D16, P18); never per user or per request. No confirm
    chip (D15): starting a draft changes nothing in the catalogue and costs
    nothing, like build_rough_cut."""
    if not offered(settings):
        return False
    tools.register(
        tools.ChatTool(
            name=TOOL["name"],
            description=TOOL["description"],
            input_schema=TOOL["input_schema"],
            run=run,
        )
    )
    return True


register()
