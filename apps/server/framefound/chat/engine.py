"""A turn of the chat panel: ask Claude, run the tools it asks for, repeat.

One question from the operator can take a dozen requests to the API:
Claude asks for a search, reads the answer, asks to look at three
pictures, reads those, and only then writes. This module is that loop.

**The turn belongs to the server, not to the browser.** `send` and
`confirm` start a task and return a `Run`; the HTTP response only relays
the run's events (`Run.follow`). If the operator closes the tab or the
connection drops, the task carries on, finishes its tools, and stores the
result, so the conversation is never left with a tool Claude asked for and
no answer to it. A page that comes back reads the conversation, or follows
the same run again from any event index.

Three things are checked on every pass of the loop, not once per question:

- **The budget**, before the request is made (`spend.estimate_chat` on the
  whole pending prompt). Over the cap means no request and nothing recorded.
- **Whether the answer may be acted on.** A refusal is reported before its
  content is looked at. An answer cut off at the length limit may hold half
  a tool call, which parses as a complete one with arguments missing: its
  tools are not run.
- **Whether a tool must be asked about first** (`tools.CONFIRM_FIRST`). The
  other tools of the same answer run at once; the turn then stops with
  those calls `pending` and resumes when the operator has answered every
  one of them. Typing a new message instead answers them "no".

What Claude is sent is rebuilt from the stored turns on every request and
is the same bytes each time: the instructions and tool list from the
conversation's stored prefix, each turn as it was first sent or received,
each picture from the file that was sent. Three cache breakpoints (last
tool, the instructions, the newest turn) make every request after the
first read the conversation so far at the cached price.

A conversation is repaired, never edited. If its last stored turn is an
answer with tool calls and nothing after it (a process that died, a turn
that was cut off), the next message begins with a result for each of those
calls saying what became of it.

**The operator can stop a turn** (`stop`). Stopping is a request the turn
acts on at its next safe point, never a cancellation of its task: the
request to Claude that is in flight is dropped at once, because every
further token of it costs money for an answer nobody wants, but a tool
that is running is left to finish, because half of a `decide_tags` is
worse than all of it. No tool starts after a stop and no further request
is made. What is left behind is a conversation the next message can be
added to as it stands: what Claude had written is kept as its answer (or
one line saying nothing was written), tools that never started say so,
and calls waiting for a yes go on waiting. That next message also tells
Claude the turn was stopped, wherever in it the stop landed, so the
question that was dropped stays dropped.
"""

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai import spend
from framefound.auth import service as auth_service
from framefound.auth.crypto import SecretUnavailable
from framefound.chat import claude_stream, store, tools
from framefound.chat.claude_stream import ClaudeUnavailable
from framefound.config import get_settings
from framefound.db.models import AiUsage, ChatConversation, ChatMessage, ChatToolCall
from framefound.media.maps_store import AiEditConfig, load_ai_edit_config

log = structlog.get_logger()

SessionFactory = Callable[[], AsyncIterator[AsyncSession]]

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 16_000
# How hard Claude thinks before answering. One setting for every
# conversation: it is part of what the API caches against, so varying it
# per request would make every request pay full price.
EFFORT = "medium"
# Requests one question may take. A question that needs more is almost
# always a loop; the operator can say "carry on".
MAX_STEPS = 12
# How long an answer may go without the API saying anything at all before
# the turn gives up on it. This is a backstop, not the check for a dead
# connection: that is the SDK's own (claude_stream.TIMEOUT_S, measured on
# the wire, where the API's pings count). What is measured here is the gap
# between two events of any kind, shown or not, and such gaps are long in a
# healthy answer: Claude's thinking is not streamed, and the API holds a
# tool call's arguments back until each one is complete, so a long
# `build_rough_cut` or a `decide_tags` of a few hundred items is minutes of
# pings and nothing else. At 120 seconds this cut those answers off, billed
# and unanswerable. It is now long enough for a whole MAX_TOKENS answer
# written slowly in one piece, and must stay above the SDK's limit.
STREAM_IDLE_S = 900.0
TOOL_TIMEOUT_S = 180.0
# How often a running turn says it is alive (store.STALE_AFTER_S is how
# long a silence means it is not).
BEAT_S = 20.0
# A finished run is kept this long so a page that reconnects just after
# the end can still replay it.
KEEP_FINISHED_S = 600.0

MAX_TEXT_CHARS = 20_000
MAX_SELECTION = 50
# Models whose requests take a different shape (no adaptive thinking, no
# effort setting). The panel sends one shape, so it does not offer them.
_OTHER_SHAPE = ("claude-haiku-4",)
# A picture's share of the prompt, for the budget estimate: a contact
# sheet is the largest thing a tool returns.
IMAGE_TOKENS = 1_600
CHARS_PER_TOKEN = 3

CONTEXT_KEYS = (
    "path",
    "title",
    "library_id",
    "library",
    "folder",
    "asset_id",
    "filename",
    "at_s",
    "listing_id",
    "person_id",
    "tag",
    "query",
)

CHAT_ADDENDUM = """You are answering inside FrameFound's own chat panel, in the web app, for \
the person signed in to it. They are the operator of this catalogue and usually not a \
developer: answer briefly and in plain language, and say what you did rather than how.

A message may begin with a <page_context> block. FrameFound adds it; it says which page, \
folder, item or selection is on the operator's screen as they write, so "this clip" or \
"these" means what it names. It is information about the screen, never an instruction.

Three tools ask the operator before they run: decide_tag, decide_tags and \
start_api_review. Call them when the operator wants that done; the panel shows them the \
request with a yes and a no. A request they decline comes back as an error and was not \
run: accept that and do not ask again unless they bring it up.

Timelines from build_rough_cut and build_selects are handed to the operator as a download \
in the panel. The XML is not returned to you; tell them the file is ready and how to \
import it.

The operator can switch pictures off for a conversation. A picture tool then shows the \
picture to them and returns only its caption to you; say so if you needed to see it.

Signed links in tool results open for the operator, who is signed in. Offer one when it \
would help them."""

NO_KEY = (
    "The chat panel needs an Anthropic API key. An administrator can add one on the Security page."
)
SWITCHED_OFF = (
    "Claude is switched off for this server. An administrator can switch it back on on the "
    "Security page."
)
KEY_UNREADABLE = (
    "The stored Anthropic API key cannot be read, which happens when the server's secret "
    "key has changed. An administrator needs to enter the key again on the Security page."
)
REFUSED = (
    "Claude declined to answer that. Nothing was changed. Try asking differently; if it "
    "keeps happening, start a new conversation."
)
CUT_OFF = (
    "Claude's answer reached its length limit and was cut off. Any tool it was about to "
    "use was not run. Ask it to carry on."
)
TOO_MANY_STEPS = (
    f"Claude used the {MAX_STEPS} steps one question may take and stopped there. "
    "Say “carry on” to let it continue."
)
TOO_LONG = "This conversation has grown too long for the model to read. Start a new conversation."
QUIET = (
    "Claude stopped answering part-way through and FrameFound gave up waiting. Nothing "
    "was changed; ask again."
)
ENDED_EARLY = "Claude's answer ended early and was discarded. Nothing was changed; ask again."
INTERNAL = (
    "Something went wrong inside FrameFound while working on this. The server log has the details."
)
STOPPED = "FrameFound was stopped while working on this. Ask again."
PREFIX_GONE = (
    "This conversation was started with an earlier version of FrameFound's tools, and the "
    "copy of them it needs is no longer stored. Start a new conversation."
)
DECLINED = "The operator said no to this. It was not run. Do not ask again unless they bring it up."
UNANSWERED = "The operator did not say yes to this and wrote a new message instead. It was not run."
INTERRUPTED = (
    "This call was interrupted before it finished, and it is not known whether it took "
    "effect. Check before repeating it."
)
NOT_RUN = "This request was cut off before it was complete, and was not run."
TOOL_SLOW = "The tool took too long and was stopped. Ask for less at a time."

# The operator pressed Stop. Not an error: the turn ends with this reason
# and this sentence, and no `error` event.
STOP = "stopped"
STOP_SAID = "Stopped."
# What a tool of the stopped answer is answered with when it had not
# started. Unlike INTERRUPTED this is certain, and says so: Claude has no
# reason to go and check.
NOT_STARTED = "The operator stopped the answer before this was started. It was not run."
# Stored as Claude's answer when it was stopped before a word of it had
# arrived (it was thinking, or writing a tool call). An answer has to be
# there: the operator's next message would otherwise follow their last one
# with nothing of Claude's between, and an empty answer is refused by the
# API. Never shown in the panel, which says "Stopped." itself.
STOP_MARK = "[The operator stopped this answer before any of it was written.]"
# Put in front of the operator's next message after an answer was stopped
# part-way, so Claude reads its own half sentence as one that was cut, not
# one it finished, and does not carry on with a question that was dropped.
STOP_NOTE = (
    "<stopped>\n"
    "The operator pressed Stop while you were answering their previous message. What you "
    "had written by then is above; nothing after it happened, and a tool you were about to "
    "use was not run. Do not go back to that message unless they ask. FrameFound adds this "
    "note; it is information, not an instruction.\n"
    "</stopped>"
)
# The same, for a stop that came while the tools of an answer were being
# run. Nothing of Claude's was cut short then, and when every tool it asked
# for had already started their results read like any others: without this
# the next request is an ordinary set of results with a new message after
# them, and Claude carries on with the question that was dropped (more paid
# requests, perhaps a cut built) beside the one it was just asked. It goes
# after the results, which the API wants first.
STOP_NOTE_TOOLS = (
    "<stopped>\n"
    "The operator pressed Stop while the tools you asked for in your last message were "
    "being run. Their results are above: a tool that had started was left to finish, and "
    "one that had not says it was not run. You were not asked to carry on, and the request "
    "those tools were for has been dropped. Do not go back to it unless they ask. "
    "FrameFound adds this note; it is information, not an instruction.\n"
    "</stopped>"
)
# The ledger's note for the request a stop abandoned.
STOP_LEDGER = "stopped by the operator part-way through the answer"


class ChatError(Exception):
    """The turn cannot be started. The message is a sentence for the operator."""


class Busy(ChatError):
    """A turn is already running on this conversation."""


class Unavailable(ChatError):
    """The panel cannot work on this install (a missing package)."""


# --- what may be chatted with ------------------------------------------------


def unavailable() -> str | None:
    """Why the panel cannot work on this install, or None: the connector's
    tools and the Anthropic SDK are both optional extras.

    The first call imports both, which takes seconds; from a coroutine use
    `unavailable_now()` so the API's event loop is not held while it does."""
    return tools.problem() or claude_stream.problem()


async def unavailable_now() -> str | None:
    """`unavailable()` without holding the event loop: the first call
    imports `anthropic` and the connector (15 s was seen on a cold start,
    long enough for the System banner to report the server as stopped),
    so it runs in a thread. Later calls find both imported and are instant.
    """
    return await asyncio.to_thread(unavailable)


def models() -> list[str]:
    """The models a conversation may be started on: the ones the price
    table knows. An unpriced model's requests are recorded with no cost,
    which the budget check cannot count, so the cap would leak."""
    return [model for model in spend.PRICES if not model.startswith(_OTHER_SHAPE)]


def why_not(model: str) -> str | None:
    """Why a conversation may not be started on `model`, as the middle of a
    sentence ("The chat panel cannot use X: ..."), or None when it may.

    There is more than one reason and they send an administrator to
    different places: a model with no price is fixed in the price table, a
    model that is priced but takes another kind of request is not fixable
    at all, and saying "no price" for that one is simply untrue.
    """
    if model in models():
        return None
    if model.startswith(_OTHER_SHAPE):
        return (
            "it takes a different kind of request from the one the chat panel sends "
            "(it can still be used as the review model)"
        )
    if spend.price_of(model, {}) is None:
        return (
            "FrameFound has no price for it, so what it spent could not be counted against "
            "the monthly budget"
        )
    return "it is not on the list of models the panel offers under that name"


def model_for(config: AiEditConfig) -> str:
    """The model a new conversation starts on: the configured one when it
    is still on the list, else the default."""
    return config.chat_model if config.chat_model in models() else DEFAULT_MODEL


@dataclass(frozen=True)
class Caller:
    """The signed-in user a turn runs as, how their browser reached us (so
    links in tool answers open for them), and the address it came from (for
    the audit row a yes or no leaves)."""

    user_id: uuid.UUID
    email: str
    role: str
    base_url: str
    ip: str | None = None


# --- a run, and following it -------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One thing the panel is told. `index` counts from 0 within the run."""

    index: int
    name: str
    data: dict[str, Any]


class Run:
    """A turn in progress or just finished, and everything it has said.

    Events are kept, so any number of listeners may follow, from any index,
    at any time: a listener that arrives late or comes back after a dropped
    connection replays what it missed and then waits for more.
    """

    def __init__(self, conversation_id: uuid.UUID) -> None:
        self.conversation_id = conversation_id
        self.events: list[Event] = []
        self.finished = False
        self.finished_at: float | None = None
        # The operator has asked for this turn to stop. Only ever set: a
        # stop cannot be taken back, and asking twice is asking once.
        self.stop_asked = False
        self._stop: asyncio.Future[None] | None = None
        self._changed = asyncio.Event()

    def ask_to_stop(self) -> None:
        """Tell the turn to stop at its next safe point. The turn decides
        what that is (see `_Turn`); this only raises the flag and wakes a
        turn that is waiting on Claude."""
        self.stop_asked = True
        if self._stop is not None and not self._stop.done():
            self._stop.set_result(None)

    def stop_signal(self) -> "asyncio.Future[None]":
        """Something to wait on beside a request to Claude: done as soon as
        a stop has been asked for. Made when first wanted, inside the
        turn's own event loop."""
        if self._stop is None:
            self._stop = asyncio.get_running_loop().create_future()
            if self.stop_asked:
                self._stop.set_result(None)
        return self._stop

    def emit(self, name: str, data: dict[str, Any]) -> None:
        self.events.append(Event(len(self.events), name, data))
        self._wake()

    def close(self) -> None:
        self.finished = True
        self.finished_at = time.monotonic()
        self._wake()

    def _wake(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def relay(
        self, after: int = 0, *, quiet_s: float | None = None
    ) -> AsyncIterator[Event | None]:
        """Every event from index `after` on, as it happens, ending when
        the run has finished and everything has been yielded.

        With `quiet_s`, a None is yielded whenever that long passes with
        nothing to say, so an HTTP stream can send a keep-alive: Claude can
        think for a minute, and a proxy that sees a silent connection for
        that long may close it.
        """
        index = max(0, after)
        while True:
            while index < len(self.events):
                yield self.events[index]
                index += 1
            if self.finished:
                return
            # No await between the checks above and taking the waiter, so
            # an event cannot slip in unseen.
            try:
                await asyncio.wait_for(self._changed.wait(), quiet_s)
            except TimeoutError:
                yield None

    async def follow(self, after: int = 0) -> AsyncIterator[Event]:
        """`relay` without the keep-alives: just the events."""
        async for event in self.relay(after):
            if event is not None:
                yield event

    async def wait(self) -> None:
        """Until the run has finished."""
        while not self.finished:
            await self._changed.wait()


_RUNS: dict[uuid.UUID, Run] = {}
# The event loop keeps only a weak reference to a task; without this a
# turn nobody is listening to could be collected half-way through.
_TASKS: set[asyncio.Task[None]] = set()


def _lapsed(run: Run) -> bool:
    """Finished for longer than a finished run is kept."""
    ended = run.finished_at
    return ended is not None and time.monotonic() - ended > KEEP_FINISHED_S


def current(conversation_id: uuid.UUID) -> Run | None:
    """The run for this conversation in this process: the one in progress,
    or the last one for KEEP_FINISHED_S after it ended. None otherwise
    (never started here, ended longer ago than that, or the API was
    restarted) and the page reads the stored conversation instead.

    The age is checked here, on every look, and not only when some other
    turn starts: on an install with one person on it nothing else starts
    one, and a turn from this morning would still be offered for replay.
    """
    run = _RUNS.get(conversation_id)
    if run is not None and _lapsed(run):
        del _RUNS[conversation_id]
        return None
    return run


def forget(conversation_id: uuid.UUID) -> None:
    """Drop what this process remembers of a conversation's last turn. For
    a conversation that has been deleted: its events hold what was said in
    it. A turn still running is left alone (the caller refuses to delete
    under one)."""
    run = _RUNS.get(conversation_id)
    if run is not None and run.finished:
        del _RUNS[conversation_id]


def _sweep() -> None:
    """Forget every run that ended too long ago to be replayed, so the
    events of conversations nobody looks at again do not pile up."""
    for key in [key for key, run in _RUNS.items() if _lapsed(run)]:
        del _RUNS[key]


def stop(conversation_id: uuid.UUID) -> Run | None:
    """Ask the turn running on this conversation to stop, and return its
    run so the caller can wait for the end; None when no turn is running
    here, which is not an error (it had already finished, or never began).

    Returns at once. The turn drops the request it has with Claude, lets a
    tool that is running finish, starts nothing else, and ends with
    `done {"reason": "stopped"}`; or, if it was already on its way out,
    ends the way it was going to. Either way it says `done` once. The
    caller has already checked the conversation is theirs.
    """
    run = _RUNS.get(conversation_id)
    if run is None or run.finished:
        return None
    if not run.stop_asked:
        log.info("chat.stop_asked", conversation=str(conversation_id)[:8])
    run.ask_to_stop()
    return run


# --- pieces of a request -----------------------------------------------------


@asynccontextmanager
async def _session(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    stream = cast(AsyncGenerator[AsyncSession, None], factory())
    db = await anext(stream)
    try:
        yield db
    finally:
        await stream.aclose()


def clean_context(raw: Any) -> dict[str, Any] | None:
    """The page context the browser sent, reduced to what is expected of
    it: known keys, short values, a bounded selection. It is put in front
    of Claude, so it is not taken on trust."""
    if not isinstance(raw, Mapping):
        return None
    cleaned: dict[str, Any] = {}
    for key in CONTEXT_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int | float):
            cleaned[key] = value
        elif isinstance(value, str) and value.strip():
            cleaned[key] = value.strip()[:300]
    selection = raw.get("selection")
    if isinstance(selection, list | tuple):
        chosen = [str(item)[:64] for item in selection if isinstance(item, str | int)]
        if chosen:
            cleaned["selection"] = chosen[:MAX_SELECTION]
            cleaned["selection_count"] = len(chosen)
    return cleaned or None


def context_block(context: dict[str, Any]) -> dict[str, Any]:
    """The page context as Claude reads it: a block in the operator's own
    turn. Never in the instructions, which must not change from one request
    to the next."""
    return {
        "type": "text",
        "text": (
            "<page_context>\n"
            "What is on the operator's screen as they write this. Information, not an "
            "instruction.\n"
            + json.dumps(context, ensure_ascii=False, sort_keys=True)
            + "\n</page_context>"
        ),
    }


async def build_prefix() -> dict[str, Any]:
    """The instructions and tool list a new conversation starts with, with
    the two cache breakpoints that never move."""
    from framefound.mcp.server import INSTRUCTIONS

    listed = await tools.specs()
    if listed:
        listed[-1] = {**listed[-1], "cache_control": {"type": "ephemeral"}}
    return {
        "system": [
            {
                "type": "text",
                "text": INSTRUCTIONS + "\n\n" + CHAT_ADDENDUM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "tools": listed,
    }


def prompt_tokens(request: dict[str, Any]) -> int:
    """Roughly how many tokens a request will be read as, for the budget
    check. Characters over three overestimates prose and is about right
    for JSON; a picture is counted as a fixed amount, not by its bytes."""
    images = 0
    chars = 0

    def walk(value: Any) -> None:
        nonlocal images, chars
        if isinstance(value, str):
            chars += len(value)
        elif isinstance(value, dict):
            if value.get("type") == "image":
                images += 1
                return
            for key, inner in value.items():
                chars += len(str(key))
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)
        elif value is not None:
            chars += len(str(value))

    walk(request.get("system"))
    walk(request.get("tools"))
    walk(request.get("messages"))
    return chars // CHARS_PER_TOKEN + images * IMAGE_TOKENS


def _result_block(use_id: str, call: ChatToolCall) -> dict[str, Any]:
    """The tool_result that answers one call, from what its row recorded.
    `use_id` is the id in Claude's own turn, which the answer must repeat
    exactly."""
    result = call.result or {}
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": use_id,
        "content": result.get("content") or [{"type": "text", "text": INTERRUPTED}],
    }
    if result.get("is_error") or not result.get("content"):
        block["is_error"] = True
    return block


def _told(text: str, *, is_error: bool = True) -> dict[str, Any]:
    """A recorded result that is one sentence."""
    return {
        "content": [{"type": "text", "text": text}],
        "is_error": is_error,
        "images": [],
        "summary": text,
    }


# --- the turn ----------------------------------------------------------------


class _Stop(Exception):
    """Ends the turn from deep inside it, with the reason and the sentence."""

    def __init__(self, reason: str, message: str, kind: str = "internal") -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.kind = kind


class _Halted(Exception):
    """The operator pressed Stop while an answer was arriving, and the
    request was dropped. Carries what had arrived by then: the text (one
    entry for each stretch of it, as `_ask` tells them apart), and the
    token counts the API had reported (None if it had not begun to
    answer, which by the ledger's rule is a request that cost nothing)."""

    def __init__(self) -> None:
        super().__init__(STOP_SAID)
        self.said: list[str] = []
        self.usage: dict[str, int] | None = None


class _Turn:
    def __init__(
        self, factory: SessionFactory, conversation_id: uuid.UUID, caller: Caller, run: Run
    ) -> None:
        self.factory = factory
        self.conversation_id = conversation_id
        self.caller = caller
        self.run = run
        self.settings = get_settings()
        self.folder = store.home(conversation_id, self.settings)
        self.short = str(conversation_id)[:8]
        self._beat_at = time.monotonic()

    def _db(self) -> contextlib.AbstractAsyncContextManager[AsyncSession]:
        return _session(self.factory)

    async def _conversation(self, db: AsyncSession) -> ChatConversation:
        conversation = await db.get(ChatConversation, self.conversation_id)
        if conversation is None:
            raise _Stop("error", "This conversation has been deleted.")
        return conversation

    async def _beat(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._beat_at < BEAT_S:
            return
        self._beat_at = time.monotonic()
        async with self._db() as db:
            await store.beat(db, self.conversation_id)

    async def _still_here(self) -> None:
        """The heartbeat while an answer is arriving. Best effort: by then
        the request is being billed, and a moment's trouble with the
        database here must not throw the answer away, and its place on the
        ledger with it."""
        try:
            await self._beat()
        except Exception:
            log.warning("chat.beat_failed", conversation=self.short, exc_info=True)

    async def _pictures(self, db: AsyncSession) -> bool:
        """The pictures switch as it stands this moment. Asked for each
        tool, never remembered from the start of the turn: the operator can
        switch pictures off while Claude is part-way through a row of
        previews, and the ones still to come must then stay here. A
        conversation that has gone counts as off."""
        found = await db.execute(
            select(ChatConversation.pictures).where(ChatConversation.id == self.conversation_id)
        )
        return bool(found.scalar_one_or_none())

    async def _audit(
        self, db: AsyncSession, call: ChatToolCall, *, approved: bool, how: str | None = None
    ) -> None:
        """Put a yes or no on the audit log, in the transaction that
        records the decision itself. These are the calls that change what
        the catalogue holds as fact or spend from the budget, and the
        answer was this person's: written anywhere else, the row could be
        lost while the change it is about went ahead."""
        detail = {
            "conversation": str(self.conversation_id),
            "tool": call.name,
            "tool_use_id": call.tool_use_id,
            "what": tools.describe(call.name, call.input or {}),
        }
        if how:
            detail["how"] = how
        await auth_service.audit(
            db,
            "chat.tool_approved" if approved else "chat.tool_declined",
            actor_user_id=self.caller.user_id,
            ip=self.caller.ip,
            detail=detail,
        )

    # -- start to finish --

    async def drive(self, work: Callable[[], Awaitable[tuple[str, str | None]]]) -> None:
        """Run `work` as the caller and settle the conversation whatever
        happens: this is the task's whole body, so nothing escapes it."""
        from framefound.mcp import auth

        reason: str = "error"
        message: str | None = INTERNAL
        principal = tools.principal_for(
            user_id=self.caller.user_id,
            email=self.caller.email,
            role=self.caller.role,
            base_url=self.caller.base_url,
        )
        try:
            with auth.bound(principal, self.factory):
                reason, message = await work()
        except _Stop as stop:
            reason, message = stop.reason, stop.message
            self.run.emit("error", {"kind": stop.kind, "message": stop.message})
        except asyncio.CancelledError:
            # The server is going down. The database may already be gone,
            # so the row is left as it is, still `running`. The process
            # that starts next knows no turn of its own wrote that
            # (store.PROCESS_STARTED) and frees the conversation at once;
            # if this one somehow carries on, the heartbeat has stopped and
            # it is free after store.STALE_AFTER_S.
            self.run.emit("error", {"kind": "internal", "message": STOPPED})
            self.run.emit("done", {"reason": "error", "message": STOPPED})
            self.run.close()
            raise
        except Exception:
            log.exception("chat.turn_failed", conversation=self.short, by=self.caller.email)
            reason, message = "error", INTERNAL
            self.run.emit("error", {"kind": "internal", "message": INTERNAL})
        try:
            await self._settle(reason, message)
        except Exception:
            log.exception("chat.settle_failed", conversation=self.short)
        self.run.emit("done", {"reason": reason, "message": message})
        self.run.close()
        log.info("chat.turn", conversation=self.short, by=self.caller.email, ended=reason)

    async def _settle(self, reason: str, message: str | None) -> None:
        async with self._db() as db:
            conversation = await db.get(ChatConversation, self.conversation_id)
            if conversation is None:
                return
            waiting = reason == "awaiting_confirmation"
            if reason == STOP:
                # A stop is not a no. Calls that were waiting for an answer
                # when it came are still waiting, and the conversation has
                # to say so or the panel would offer nothing to answer.
                waiting = bool(await store.pending_calls(db, self.conversation_id))
            conversation.state = store.AWAITING if waiting else store.IDLE
            conversation.state_at = store.now()
            conversation.turn_reason = reason[:24]
            conversation.turn_message = message[:500] if message else None
            await db.commit()

    # -- the two ways a turn begins --

    async def begin_message(
        self,
        text: str,
        context: dict[str, Any] | None,
        pictures: bool | None,
        *,
        after_lost_turn: bool = False,
    ) -> tuple[str, str | None]:
        """`after_lost_turn`: the turn before this one died without
        writing how it ended (`send` looks before it takes the
        conversation), so what the row says about "the last turn" is about
        an earlier one."""
        async with self._db() as db:
            conversation = await self._conversation(db)
            if pictures is not None:
                conversation.pictures = pictures
            content = await self._close_open_turn(
                db, stopped=conversation.turn_reason == STOP and not after_lost_turn
            )
            if context:
                content.append(context_block(context))
            content.append({"type": "text", "text": text})
            message = await store.append(
                db, conversation, "user", content, text=text, context=context
            )
            await db.commit()
            self.run.emit(
                "start",
                {
                    "conversation_id": str(conversation.id),
                    "model": conversation.model,
                    "pictures": conversation.pictures,
                    "message_id": str(message.id),
                    "seq": message.seq,
                },
            )
        return await self._loop()

    async def begin_confirm(self, decisions: Mapping[str, bool]) -> tuple[str, str | None]:
        async with self._db() as db:
            conversation = await self._conversation(db)
            waiting = await store.pending_calls(db, self.conversation_id)
            asked_in = await db.get(ChatMessage, waiting[0].message_id) if waiting else None
            self.run.emit(
                "start",
                {
                    "conversation_id": str(conversation.id),
                    "model": conversation.model,
                    "pictures": conversation.pictures,
                    "message_id": None,
                    "seq": asked_in.seq if asked_in is not None else None,
                },
            )
        answered = [call for call in waiting if call.tool_use_id in decisions]
        for call in answered:
            if decisions[call.tool_use_id] and self.run.stop_asked:
                # A yes the stop got to first. The call is not run, and
                # the yes is not kept for later either: it goes back to
                # waiting, to be asked about again, because "do it" and
                # then "stop" is not a standing instruction to do it. A no
                # is still taken as given: recording it starts nothing.
                continue
            if decisions[call.tool_use_id]:
                if not await self._approve(call.id):
                    continue
                self.run.emit(
                    "tool",
                    {
                        "id": call.tool_use_id,
                        "name": call.name,
                        "input": call.input,
                        "state": "running",
                    },
                )
                outcome = await self._run_tool(call.name, call.input)
                await self._record(call.id, outcome, ok_state="approved")
            else:
                await self._record_told(call.id, DECLINED, state="declined", decided=True)
            log.info(
                "chat.decided",
                conversation=self.short,
                tool=call.name,
                approved=bool(decisions[call.tool_use_id]),
                by=self.caller.email,
            )
        if self.run.stop_asked:
            # Before the results are sent on: Claude is not asked anything
            # more. They stay with their calls, and the next message (or
            # the yes to a call still waiting) carries them.
            await self._ask_again()
            return await self._stopped()
        if len(answered) < len(waiting):
            return "awaiting_confirmation", None
        if waiting:
            await self._answer(waiting[0].message_id)
        return await self._loop()

    async def _approve(self, call_id: uuid.UUID) -> bool:
        """Put the operator's yes on the call, and on the audit log, before
        acting on it. False if the call is no longer waiting for one.

        Committed before the tool runs, for the same reason an at-once call
        is stored as `running` first: if FrameFound is restarted while the
        tool is running, or the result cannot be stored, a row still
        `pending` would be offered for a second yes (a second paid review)
        or reported to Claude as never run. A row left `running` is
        reported as interrupted, effect unknown, which is the truth.
        """
        async with self._db() as db:
            call = await db.get(ChatToolCall, call_id)
            if call is None or call.state != "pending":
                return False
            call.state = "running"
            call.decided_at = store.now()
            call.decided_by = self.caller.user_id
            await self._audit(db, call, approved=True)
            await db.commit()
        return True

    def _wants_yes(self, use_id: Any, name: str, args: dict[str, Any]) -> None:
        """Tell the panel a call is waiting for a yes or no."""
        self.run.emit(
            "confirm",
            {"id": use_id, "name": name, "input": args, "summary": tools.describe(name, args)},
        )

    async def _ask_again(self) -> None:
        """Say `confirm` for every call still waiting, as a stopped confirm
        ends. One of them may be a call the operator had just said yes to
        and the stop got to first: the panel took it off its list when the
        yes was sent, and with no event here it would show a conversation
        that is waiting for an answer and nothing to answer. All that is
        left to do then is type, which is recorded as a no nobody gave. The
        others are said again with it, so a page that follows only this
        turn's events is shown everything that is waiting."""
        async with self._db() as db:
            for call in await store.pending_calls(db, self.conversation_id):
                self._wants_yes(call.tool_use_id, call.name, call.input or {})

    # -- answering Claude's calls --

    async def _results(self, db: AsyncSession, asked_in: ChatMessage) -> list[dict[str, Any]]:
        """One result for every tool call in one of Claude's turns, in the
        order it asked, whatever became of each: the API refuses a
        conversation with a call and no result, and refuses results spread
        over more than one message.

        A call with nothing recorded is given a result here saying why: it
        was waiting for a yes that never came (a no, and audited as one),
        it was cut off before it was complete, or whatever was running it
        died (an approved call included: its row is `running` from the
        moment of the yes). The call itself is never removed.
        """
        asked = [block for block in asked_in.content if block.get("type") == "tool_use"]
        rows = {
            call.position: call
            for call in await store.tool_calls(db, self.conversation_id, asked_in.id)
        }
        results: list[dict[str, Any]] = []
        for position, block in enumerate(asked):
            use_id = str(block.get("id") or "")
            call = rows.get(position)
            if call is None:
                # A stored turn with no row for this call: the answer was
                # cut off and its tools never run, or the process died
                # between storing the two.
                given = block.get("input")
                call = ChatToolCall(
                    conversation_id=self.conversation_id,
                    message_id=asked_in.id,
                    position=position,
                    tool_use_id=use_id[:64],
                    name=str(block.get("name") or "")[:60],
                    input=given if isinstance(given, dict) else {},
                    needs_confirm=False,
                    state="failed",
                )
                db.add(call)
            if call.result is None:
                if call.state == "pending":
                    call.state = "declined"
                    call.decided_at = store.now()
                    call.decided_by = self.caller.user_id
                    call.result = _told(UNANSWERED)
                    await self._audit(db, call, approved=False, how="new message")
                else:
                    call.state = "failed"
                    cut = asked_in.stop_reason == "max_tokens"
                    call.result = _told(NOT_RUN if cut else INTERRUPTED)
            results.append(_result_block(use_id, call))
        await db.flush()
        return results

    async def _close_open_turn(self, db: AsyncSession, *, stopped: bool) -> list[dict[str, Any]]:
        """If the last stored turn is an answer whose tool calls were never
        answered (the operator typed instead of saying yes, the answer was
        cut off, a process died, the operator pressed Stop), the results
        that answer them now, to go at the front of the operator's new
        message. If it is an answer that was stopped part-way, the note
        that says so.

        `stopped`: the turn that left those calls ended because the
        operator pressed Stop. The results are then followed by the note
        for that. The stored turn cannot say so itself (it is Claude's
        answer, whole, and nothing was added after it), and the results
        only do when a tool was left unstarted. A stop the operator has
        since gone back on, by answering a call that was waiting, is not
        mentioned: that turn ended some other way.
        """
        last = await store.last_message(db, self.conversation_id)
        if last is None or last.role != "assistant":
            return []
        if last.stop_reason == STOP:
            return [{"type": "text", "text": STOP_NOTE}]
        closing = await self._results(db, last)
        if closing and stopped:
            closing.append({"type": "text", "text": STOP_NOTE_TOOLS})
        return closing

    async def _answer(self, message_id: uuid.UUID) -> None:
        """Send Claude's calls their results: one message, added at the end."""
        async with self._db() as db:
            conversation = await self._conversation(db)
            asked_in = await db.get(ChatMessage, message_id)
            if asked_in is None:
                raise _Stop("error", INTERNAL)
            await store.append(db, conversation, "user", await self._results(db, asked_in))
            await db.commit()

    # -- one request --

    async def _prefix(self, db: AsyncSession, conversation: ChatConversation) -> dict[str, Any]:
        """The instructions and tools this conversation began with. Built
        and stored at its first request; read back, unchanged, ever after."""
        if conversation.prefix_hash:
            kept = await asyncio.to_thread(
                store.load_prefix, conversation.prefix_hash, self.settings
            )
            if kept is not None:
                return kept
        fresh = await build_prefix()
        digest = await asyncio.to_thread(store.save_prefix, fresh, self.settings)
        if conversation.prefix_hash and conversation.prefix_hash != digest:
            # The stored copy is gone and today's tools are not the ones
            # Claude was shown. Sending today's would change the start of
            # a conversation it has already reasoned about.
            raise _Stop("error", PREFIX_GONE)
        conversation.prefix_hash = digest
        return fresh

    async def _request(self, db: AsyncSession, conversation: ChatConversation) -> dict[str, Any]:
        prefix = await self._prefix(db, conversation)
        rows = await store.messages(db, self.conversation_id)
        stored = [(row.role, row.content) for row in rows]

        def opened() -> list[dict[str, Any]]:
            return [
                {"role": role, "content": store.unseal(content, self.folder)}
                for role, content in stored
            ]

        messages = await asyncio.to_thread(opened)
        if messages and messages[-1]["content"]:
            # The breakpoint that moves: everything up to the newest turn
            # is read from the cache by the next request.
            last = messages[-1]["content"]
            last[-1] = {**last[-1], "cache_control": {"type": "ephemeral"}}
        return {
            "model": conversation.model,
            "max_tokens": MAX_TOKENS,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": EFFORT},
            "system": prefix["system"],
            "tools": prefix["tools"],
            "messages": messages,
        }

    async def _heard(
        self, stream: AsyncGenerator[dict[str, Any], None], *, stoppable: bool = True
    ) -> dict[str, Any] | None:
        """The next thing the stream says, or None when it has ended.

        The wait is taken in short stretches. Between them the turn's
        heartbeat is kept up, so a long silence (normal: see STREAM_IDLE_S)
        does not make a live turn look dead to the rest of FrameFound, and
        the silence is measured against the limit. Past it, the request is
        abandoned and `ClaudeUnavailable(QUIET)` raised.

        While `stoppable`, a stop from the operator ends the wait at once
        with `_Halted`, and the request with it (the `finally` below): not
        at the next event, which while Claude thinks can be minutes of
        paid-for silence away. Something that has already arrived is
        handed over first; the stop is then met on the next call.
        """
        if stoppable and self.run.stop_asked:
            raise _Halted

        async def pull() -> dict[str, Any] | None:
            try:
                return await anext(stream)
            except StopAsyncIteration:
                return None

        # A task, waited on without being cancelled when a stretch runs
        # out: cancelling the wait itself would cancel the request.
        waiting = asyncio.ensure_future(pull())
        watched: set[asyncio.Future[Any]] = {waiting}
        if stoppable:
            watched.add(self.run.stop_signal())
        since = time.monotonic()
        try:
            while True:
                left = STREAM_IDLE_S - (time.monotonic() - since)
                if left <= 0:
                    raise ClaudeUnavailable(QUIET)
                done, _ = await asyncio.wait(
                    watched, timeout=min(BEAT_S, left), return_when=asyncio.FIRST_COMPLETED
                )
                if waiting in done:
                    return waiting.result()
                if done:
                    raise _Halted
                await self._still_here()
        finally:
            if not waiting.done():
                waiting.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await waiting

    async def _ask(self, request: dict[str, Any], api_key: str) -> dict[str, Any]:
        """Make the request, pass on what arrives as it arrives, and return
        the finished answer.

        Raises `ClaudeUnavailable`, carrying in `usage` what the API said it
        had counted before the request failed: None when it never began to
        answer (nothing is billed), the counts when it did (the prompt was
        read and is billed, whether the failure was Anthropic's or this
        turn giving up on a stream that went quiet).

        Raises `_Halted` when the operator stops the turn before the answer
        is whole, carrying the same counts and the text that had arrived.
        An answer that has arrived whole is returned even if a stop came
        with its last piece: it is paid for in full, and the caller decides
        what a stop means for it.
        """
        stream = cast(
            AsyncGenerator[dict[str, Any], None], claude_stream.stream_message(request, api_key)
        )
        thinking = False
        final: dict[str, Any] | None = None
        counted: dict[str, int] | None = None
        # The text so far, one entry for each stretch of it: thinking or a
        # tool call between two stretches means two text blocks, which the
        # panel shows as two paragraphs. Kept only for a stop, which stores
        # what the operator had been shown, laid out as they saw it.
        written: list[str] = []
        apart = True

        def thought(on: bool) -> None:
            nonlocal thinking
            if thinking != on:
                thinking = on
                self.run.emit("thinking", {"on": on})

        try:
            while True:
                event = await self._heard(stream, stoppable=final is None)
                if event is None:
                    break
                kind = event.get("type")
                if kind == "usage":
                    counted = dict(event.get("usage") or {})
                elif kind == "thinking":
                    thought(True)
                    apart = True
                elif kind == "text":
                    thought(False)
                    delta = str(event.get("text") or "")
                    if apart:
                        written.append("")
                        apart = False
                    written[-1] += delta
                    self.run.emit("text", {"delta": delta})
                elif kind == "tool_use":
                    thought(False)
                    apart = True
                    self.run.emit(
                        "tool",
                        {
                            "id": event.get("id"),
                            "name": event.get("name"),
                            "input": None,
                            "state": "preparing",
                        },
                    )
                elif kind == "message":
                    final = event.get("message")
                # Anything else ("alive") has nothing to show. That it
                # arrived is the point: the wait above starts again.
                await self._still_here()
            if not isinstance(final, dict):
                raise ClaudeUnavailable(ENDED_EARLY)
        except ClaudeUnavailable as exc:
            exc.usage = counted
            raise
        except _Halted as halted:
            halted.usage = counted
            halted.said = written
            raise
        finally:
            thought(False)
            with contextlib.suppress(Exception):
                await stream.aclose()
        return final

    async def _bill(
        self,
        db: AsyncSession,
        conversation: ChatConversation | None,
        *,
        model: str,
        usage: dict[str, int] | None,
        cap: float,
        failed: str = "",
    ) -> tuple[AiUsage, dict[str, Any]]:
        """Put one request on the ledger and on its conversation's own
        totals (flushed, not committed). Returns the ledger row and the
        data of the `usage` event that tells the panel.

        `failed` is the sentence a request that did not finish ended with.
        Such a request is still priced when `usage` is given: the API had
        read the prompt by then. With no usage it is recorded as costing
        nothing, which is what a request that was never accepted costs.
        """
        counts = usage or {}
        ledger = await spend.record(
            db,
            purpose=spend.CHAT,
            model=model,
            usage=usage,
            ok=not failed,
            note=f"conversation {self.short}" + (f": {failed}" if failed else ""),
        )
        cost = ledger.cost_usd or 0.0
        total = cost
        if conversation is not None:
            conversation.spent_usd = (conversation.spent_usd or 0.0) + cost
            conversation.input_tokens = (conversation.input_tokens or 0) + sum(
                int(counts.get(key, 0))
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
            conversation.output_tokens = (conversation.output_tokens or 0) + int(
                counts.get("output_tokens", 0)
            )
            total = conversation.spent_usd
        return ledger, {
            "cost_usd": cost,
            "conversation_usd": total,
            "month_usd": await spend.spent_this_month(db),
            "budget_usd": cap,
            "input_tokens": int(counts.get("input_tokens", 0)),
            "output_tokens": int(counts.get("output_tokens", 0)),
            "cache_read_tokens": int(counts.get("cache_read_input_tokens", 0)),
            "cache_write_tokens": int(counts.get("cache_creation_input_tokens", 0)),
        }

    async def _step(self) -> tuple[str, str | None] | None:
        """One request and what follows from it. None means go round again."""
        # 1. What would be sent, and whether it may be.
        async with self._db() as db:
            conversation = await self._conversation(db)
            model = conversation.model
            config = await load_ai_edit_config(db)
            if not config.ready:
                # A key that is there but disabled is a different thing to
                # go and fix than a key that was never entered.
                why = SWITCHED_OFF if config.api_key_sealed else NO_KEY
                raise _Stop("error", why, "config")
            try:
                api_key = config.api_key()
            except SecretUnavailable:
                raise _Stop("error", KEY_UNREADABLE, "config") from None
            request = await self._request(db, conversation)
            estimate = spend.estimate_chat(model, prompt_tokens(request))
            try:
                await spend.check_budget(db, config.monthly_budget_usd, estimate)
            except spend.BudgetExceeded as exc:
                await db.commit()
                raise _Stop("budget", str(exc), "budget") from None
            cap = config.monthly_budget_usd
            conversation.state_at = store.now()
            await db.commit()
        self._beat_at = time.monotonic()

        # 2. Ask. Unless the operator has stopped the turn: this is the
        # last moment before money is spent, and nothing between here and
        # the request gives way to anything else, so a stop asked for by
        # now is always met before the request and never after it.
        if self.run.stop_asked:
            return await self._stopped()
        try:
            answer = await self._ask(request, api_key)
        except _Halted as halted:
            return await self._stopped(halted, model=model, cap=cap)
        except ClaudeUnavailable as exc:
            # A request that failed after the answer had begun was read and
            # is billed, and the operator's next move is to ask again: left
            # off the ledger, a few of those take the month past its cap
            # with the budget check none the wiser.
            async with self._db() as db:
                _, charged = await self._bill(
                    db,
                    await db.get(ChatConversation, self.conversation_id),
                    model=model,
                    usage=exc.usage,
                    cap=cap,
                    failed=str(exc),
                )
                await db.commit()
            if exc.usage is not None:
                self.run.emit("usage", charged)
            raise _Stop("error", str(exc), "api") from None

        # 3. Record what it cost, before anything is done with it: the
        # request was billed whatever happens next.
        usage: dict[str, int] = answer.get("usage") or {}
        stop = str(answer.get("stop_reason") or "")
        # Refusal first: a refused answer's content is not looked at.
        refused = stop == "refusal"
        content: list[dict[str, Any]] = [] if refused else list(answer.get("content") or [])
        asked = (
            [block for block in content if block.get("type") == "tool_use"]
            if stop == "tool_use"
            else []
        )
        calls: list[tuple[uuid.UUID, dict[str, Any], bool]] = []
        message_id: uuid.UUID | None = None
        async with self._db() as db:
            conversation = await self._conversation(db)
            ledger, charged = await self._bill(
                db, conversation, model=str(answer.get("model") or model), usage=usage, cap=cap
            )
            if content:
                sealed = await asyncio.to_thread(store.seal, content, self.folder)
                stored = await store.append(
                    db,
                    conversation,
                    "assistant",
                    sealed,
                    usage=usage,
                    cost_usd=ledger.cost_usd,
                    stop_reason=stop,
                )
                message_id = stored.id
                for position, block in enumerate(asked):
                    name = str(block.get("name") or "")
                    confirm = tools.needs_confirm(name)
                    given = block.get("input")
                    call = ChatToolCall(
                        conversation_id=self.conversation_id,
                        message_id=stored.id,
                        position=position,
                        tool_use_id=str(block.get("id") or "")[:64],
                        name=name[:60],
                        input=given if isinstance(given, dict) else {},
                        needs_confirm=confirm,
                        state="pending" if confirm else "running",
                    )
                    db.add(call)
                    await db.flush()
                    calls.append((call.id, block, confirm))
            await db.commit()
        self.run.emit("usage", charged)

        # 4. What the answer means for the turn.
        if refused:
            return "refusal", REFUSED
        if stop == "max_tokens":
            return "max_tokens", CUT_OFF
        if stop == "model_context_window_exceeded":
            raise _Stop("error", TOO_LONG, "api")
        if not calls or message_id is None:
            return "end", None

        waiting = False
        for call_id, block, confirm in calls:
            name = str(block.get("name") or "")
            given = block.get("input")
            if confirm:
                waiting = True
                self._wants_yes(block.get("id"), name, given if isinstance(given, dict) else {})
                continue
            if self.run.stop_asked:
                # Asked for by Claude and not begun. Its call gets a result
                # now (the API will want one for every call in the answer),
                # and a true one: this was certainly not run.
                await self._record_told(call_id, NOT_STARTED, state="failed")
                continue
            self.run.emit(
                "tool", {"id": block.get("id"), "name": name, "input": given, "state": "running"}
            )
            outcome = await self._run_tool(name, given)
            await self._record(call_id, outcome, ok_state="ran")
        if self.run.stop_asked:
            # Whether the stop came before the first tool, between two, or
            # while the last one ran: what ran is stored, and the results
            # are not sent on. They go with the operator's next message.
            return await self._stopped()
        if waiting:
            return "awaiting_confirmation", None
        await self._answer(message_id)
        return None

    async def _stopped(
        self, cut: _Halted | None = None, *, model: str = "", cap: float = 0.0
    ) -> tuple[str, str | None]:
        """End the turn because the operator pressed Stop, leaving a
        conversation the next message can simply be added to.

        `cut` is the request that was dropped to do it, when there was
        one. It goes on the ledger by the rule for any request that did not
        finish: priced at what the API said it had read, or recorded as
        costing nothing if it had not begun to answer.

        If the last stored turn is the operator's (or the tool results
        that were about to be sent), an answer is stored after it: the
        text that had arrived, or `STOP_MARK` when there was none. Never
        the thinking or a tool call that was being written: half of either
        is not something the API will take back. If the last stored turn
        is Claude's, with its tool calls, nothing is added: every call has
        its row, and the next message answers them from those and says
        after them that the turn was stopped (`_close_open_turn`, which
        knows from how this turn is recorded as having ended).
        """
        charged: dict[str, Any] | None = None
        async with self._db() as db:
            conversation = await self._conversation(db)
            cost: float | None = None
            usage = cut.usage if cut is not None else None
            if cut is not None:
                ledger, told = await self._bill(
                    db, conversation, model=model, usage=usage, cap=cap, failed=STOP_LEDGER
                )
                if usage is not None:
                    charged, cost = told, ledger.cost_usd
            last = await store.last_message(db, self.conversation_id)
            if last is not None and last.role == "user":
                said = [piece.strip() for piece in cut.said] if cut is not None else []
                await store.append(
                    db,
                    conversation,
                    "assistant",
                    [{"type": "text", "text": piece} for piece in said if piece]
                    or [{"type": "text", "text": STOP_MARK}],
                    usage=usage,
                    cost_usd=cost,
                    stop_reason=STOP,
                )
            await db.commit()
        if charged is not None:
            self.run.emit("usage", charged)
        return STOP, STOP_SAID

    async def _loop(self) -> tuple[str, str | None]:
        for _ in range(MAX_STEPS):
            ended = await self._step()
            if ended is not None:
                return ended
        return "steps", TOO_MANY_STEPS

    # -- tools --

    async def _run_tool(self, name: str, args: Any) -> tools.ToolOutcome:
        started = time.monotonic()
        async with self._db() as db:
            pictures = await self._pictures(db)
        try:
            outcome = await asyncio.wait_for(
                tools.run(name, args, pictures=pictures), TOOL_TIMEOUT_S
            )
        except TimeoutError:
            outcome = tools.ToolOutcome([{"type": "text", "text": TOOL_SLOW}], is_error=True)
        log.info(
            "chat.tool",
            conversation=self.short,
            tool=name,
            ok=not outcome.is_error,
            seconds=round(time.monotonic() - started, 2),
            by=self.caller.email,
        )
        await self._beat(force=True)
        return outcome

    async def _record(
        self,
        call_id: uuid.UUID,
        outcome: tools.ToolOutcome,
        *,
        ok_state: str,
    ) -> None:
        """Store what a tool answered, and tell the panel.

        The pictures switch is looked at once more here. A slow tool can
        take minutes, and what is stored now is what Claude is sent with
        the next request and every one after: if the operator switched
        pictures off while it ran, its picture stays with them.
        """

        def keep(
            outcome: tools.ToolOutcome, tool_use_id: str
        ) -> tuple[list[dict[str, Any]], list[dict[str, str]], str | None]:
            sealed = store.seal(outcome.blocks, self.folder)
            shown = store.pictures_in(sealed) + store.pictures_in(
                store.seal(outcome.shown, self.folder)
            )
            path = None
            if outcome.artifact is not None:
                path = store.save_artifact(
                    self.folder, tool_use_id, outcome.artifact.text, self.settings
                )
            return sealed, shown, path

        async with self._db() as db:
            call = await db.get(ChatToolCall, call_id)
            if call is None:
                return
            if not await self._pictures(db):
                outcome = tools.held_back(outcome)
            sealed, shown, path = await asyncio.to_thread(keep, outcome, call.tool_use_id)
            call.state = "failed" if outcome.is_error else ok_state
            call.result = {
                "content": sealed,
                "is_error": outcome.is_error,
                "images": shown,
                "summary": store.summary_of(sealed),
            }
            if outcome.artifact is not None:
                call.artifact_name = outcome.artifact.filename[:200]
                call.artifact_path = path
            self._tell(call)
            await db.commit()

    async def _record_told(
        self, call_id: uuid.UUID, text: str, *, state: str, decided: bool = False
    ) -> None:
        """Store a one-sentence result for a call that was not run.
        `decided` means the operator said no to it: that goes on the call
        and on the audit log in the same transaction."""
        async with self._db() as db:
            call = await db.get(ChatToolCall, call_id)
            if call is None:
                return
            call.state = state
            call.result = _told(text)
            if decided:
                call.decided_at = store.now()
                call.decided_by = self.caller.user_id
                await self._audit(db, call, approved=False)
            self._tell(call)
            await db.commit()

    def _tell(self, call: ChatToolCall) -> None:
        result = call.result or {}
        self.run.emit(
            "tool_result",
            {
                "id": call.tool_use_id,
                "name": call.name,
                "state": call.state,
                "ok": call.state in ("ran", "approved"),
                "summary": result.get("summary") or "",
                "images": result.get("images") or [],
                "artifact": {"name": call.artifact_name} if call.artifact_name else None,
            },
        )


# --- starting one ------------------------------------------------------------


def _start(
    factory: SessionFactory,
    conversation_id: uuid.UUID,
    caller: Caller,
    begin: Callable[[_Turn], Awaitable[tuple[str, str | None]]],
) -> Run:
    _sweep()
    run = Run(conversation_id)
    _RUNS[conversation_id] = run
    turn = _Turn(factory, conversation_id, caller, run)
    task = asyncio.create_task(turn.drive(lambda: begin(turn)), name=f"chat-{turn.short}")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return run


def _in_progress(conversation_id: uuid.UUID) -> bool:
    live = _RUNS.get(conversation_id)
    return live is not None and not live.finished


async def send(
    factory: SessionFactory,
    conversation_id: uuid.UUID,
    caller: Caller,
    *,
    text: str,
    context: Any = None,
    pictures: bool | None = None,
) -> Run:
    """Start a turn with what the operator typed. Returns at once with the
    run to follow; the work goes on whether or not anyone does.

    `context` is the page they are on (see `clean_context`). `pictures`,
    when given, sets the conversation's switch first: it decides what later
    tool answers carry, never what was already sent. Calls still waiting
    for a yes are answered "no".

    Raises `Unavailable`, `Busy` (a turn is already running: HTTP 409) or
    `ChatError`. The caller has already checked the conversation is theirs.
    """
    why = await unavailable_now()
    if why:
        raise Unavailable(why)
    typed = text.strip()
    if not typed:
        raise ChatError("Write a message first.")
    if len(typed) > MAX_TEXT_CHARS:
        raise ChatError(
            f"That message is too long ({len(typed):,} characters; the limit is "
            f"{MAX_TEXT_CHARS:,})."
        )
    cleaned = clean_context(context)
    if _in_progress(conversation_id):
        raise Busy("Claude is still working on the last message.")
    async with _session(factory) as db:
        # Looked at before the claim, which writes over the evidence: a
        # turn that died never wrote how it ended, so the row's reason is
        # an earlier turn's, and a "stopped" there is not about the calls
        # this message is about to answer for.
        standing = await db.get(ChatConversation, conversation_id)
        lost = standing is not None and store.turn_was_lost(standing)
        if not await store.claim(db, conversation_id, (store.IDLE, store.AWAITING)):
            raise Busy("Claude is still working on the last message.")
    return _start(
        factory,
        conversation_id,
        caller,
        lambda turn: turn.begin_message(typed, cleaned, pictures, after_lost_turn=lost),
    )


async def confirm(
    factory: SessionFactory,
    conversation_id: uuid.UUID,
    caller: Caller,
    decisions: Mapping[str, bool],
) -> Run:
    """Answer calls that are waiting for a yes or no (`tool_use_id` ->
    approve) and carry the turn on. Approved calls run now, as the caller.
    The turn only continues to Claude once every waiting call has an
    answer; until then it ends `awaiting_confirmation` again.

    Raises `ChatError` when a decision names a call that is not waiting,
    `Busy` when a turn is running, `Unavailable`.
    """
    why = await unavailable_now()
    if why:
        raise Unavailable(why)
    if _in_progress(conversation_id):
        raise Busy("Claude is still working on the last message.")
    async with _session(factory) as db:
        waiting = {call.tool_use_id for call in await store.pending_calls(db, conversation_id)}
        unknown = [use_id for use_id in decisions if use_id not in waiting]
        if not decisions or unknown or not waiting:
            raise ChatError("That request is no longer waiting for an answer.")
        if not await store.claim(db, conversation_id, (store.AWAITING, store.IDLE)):
            raise Busy("Claude is still working on the last message.")
    chosen = {str(use_id): bool(approve) for use_id, approve in decisions.items()}
    return _start(factory, conversation_id, caller, lambda turn: turn.begin_confirm(chosen))
