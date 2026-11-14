"""The chat panel's HTTP surface: conversations, and turns as event streams.

The work is in `framefound/chat`; this module is the door to it. Three
things about the door are deliberate.

**A turn is an event stream, and the stream is only a window on it.**
Sending a message or answering a confirmation starts a turn on the server
(`chat.engine`) and answers with `text/event-stream`, relaying what the
turn says as it says it. Closing the connection does not stop the turn: it
finishes and is stored, and a page that comes back reads the conversation
(`GET /chat/conversations/{id}`) and, if a turn is still going, follows it
again from any event (`GET .../events`). Stopping one is its own request
(`POST .../stop`), so it is something the operator did, never something a
dropped connection did for them. Following only works because the
API is one process (the Dockerfile, the compose file and the Mac services
all start a single uvicorn): the events live in that process's memory.

**The request's database session is given back before a stream starts.**
A stream can stay open for minutes; a session held that long is a
connection the pool cannot lend to anyone else. The turn opens its own.

**Conversations are private to the person who started them.** Every route
answers 404 for a conversation that is someone else's, exactly as for one
that does not exist, an administrator's request included. A read-only
account may look at its conversations and cannot start, continue or delete
one: that is the existing rule for every non-GET route (`auth/deps.py`),
and it is why nothing here checks the role.

Errors before a stream begins use the usual envelope and status (404, 409
while a turn is running, 400 with a sentence, 503 when the panel's
packages are not installed). Once the stream has begun the status is 200
whatever happens, and a failure is an `error` event followed by `done`.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai import spend
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, client_ip
from framefound.chat import engine, store, tools
from framefound.db.engine import get_session
from framefound.db.models import ChatConversation, ChatMessage, ChatToolCall, User
from framefound.mcp.auth import base_url
from framefound.media.maps_store import load_ai_edit_config

log = structlog.get_logger()
router = APIRouter(prefix="/chat", tags=["chat"])

# Claude can think for a minute without a word, and a proxy that sees a
# connection silent for that long may close it. A comment line this often
# keeps it open; the browser's stream reader ignores comments.
KEEPALIVE_S = 15.0

# How long the Stop route waits for the turn to end before answering. A
# turn that was waiting on Claude ends in milliseconds, and the answer
# then carries its settled state. One with a tool running may take that
# tool's whole time limit, and the button must not hang for that.
STOP_WAIT_S = 2.0

# no-transform: a compressing proxy (Caddy's `encode`, the Next dev server)
# may hold a stream back until it has enough to compress, and this is the
# header they agree to leave a response alone for. X-Accel-Buffering is
# the same request in nginx's dialect, for whoever puts one in front.
STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}

# Nothing served from a conversation's folder is a page to run.
INERT = {"Content-Security-Policy": "default-src 'none'; sandbox"}

State = Literal["idle", "running", "awaiting_confirmation"]


# --- shapes -------------------------------------------------------------------


class ChatStatus(BaseModel):
    # The panel's packages are installed on this server.
    available: bool
    # A key has been entered / Claude has not been switched off.
    configured: bool
    enabled: bool
    # Why a message would not be answered right now, as a sentence to show
    # in place of the text box; null when nothing is in the way.
    problem: str | None
    # False for a read-only account: it may read its conversations only.
    can_chat: bool
    # The model a new conversation starts on, and the ones on offer.
    model: str
    models: list[str]
    budget_usd: float
    spent_this_month_usd: float


class PictureRef(BaseModel):
    # GET /chat/conversations/{id}/blobs/{blob}
    blob: str
    media_type: str


class ArtifactRef(BaseModel):
    # GET /chat/conversations/{id}/artifacts/{the tool call's id}
    name: str


class ToolChip(BaseModel):
    """A tool Claude asked for, and what became of it."""

    id: str
    name: str
    input: dict[str, Any]
    # running | ran | failed | pending | approved | declined
    state: str
    needs_confirm: bool
    ok: bool
    # One line of the tool's answer; empty until it has one.
    summary: str
    # What saying yes will do, for a tool that asks first; else null.
    ask: str | None
    images: list[PictureRef]
    artifact: ArtifactRef | None


class MessageOut(BaseModel):
    id: uuid.UUID
    seq: int
    role: Literal["user", "assistant"]
    text: str
    # The page the operator was on when they wrote it (their turns only).
    context: dict[str, Any] | None = None
    tools: list[ToolChip] = []
    stop_reason: str | None = None
    cost_usd: float | None = None
    created_at: datetime


class PendingOut(BaseModel):
    """A call waiting for a yes or no: the same four fields as the
    `confirm` event."""

    id: str
    name: str
    input: dict[str, Any]
    summary: str


class LastTurn(BaseModel):
    # end | awaiting_confirmation | budget | steps | refusal | max_tokens |
    # error | stopped (the operator pressed Stop)
    reason: str
    message: str | None


class RunOut(BaseModel):
    """The turn this server has in memory for the conversation."""

    # Still going. False = finished, kept a few minutes for replay.
    active: bool
    # How many events it has produced so far.
    events: int
    # The `seq` of the stored message it began from (its `start` event's
    # `seq`); null until it has said `start`.
    seq: int | None


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str
    model: str
    pictures: bool
    state: State
    spent_usd: float
    created_at: datetime
    updated_at: datetime


class ConversationOut(ConversationSummary):
    input_tokens: int
    output_tokens: int
    last_turn: LastTurn | None
    run: RunOut | None
    messages: list[MessageOut]
    pending: list[PendingOut]


class ConversationIn(BaseModel):
    pictures: bool = True


class ConversationPatch(BaseModel):
    pictures: bool


class MessageIn(BaseModel):
    # The engine has the real limit and a sentence for it; this one only
    # keeps a runaway body out.
    text: str = Field(max_length=200_000)
    context: dict[str, Any] | None = None
    pictures: bool | None = None


class DecisionIn(BaseModel):
    tool_use_id: str = Field(min_length=1, max_length=64)
    approve: bool


class ConfirmIn(BaseModel):
    decisions: list[DecisionIn] = Field(min_length=1, max_length=100)


# --- helpers ------------------------------------------------------------------


def _utc(moment: datetime) -> datetime:
    """SQLite hands timestamps back without a zone; they were written in
    UTC, and a browser reads a zoneless time as local."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _factory(request: Request) -> engine.SessionFactory:
    """Where a turn gets its database sessions: the same line the
    connector's gate uses, so a test's database reaches the tools too."""
    found: engine.SessionFactory = request.app.dependency_overrides.get(get_session, get_session)
    return found


def _caller(request: Request, user: User) -> engine.Caller:
    return engine.Caller(
        user_id=user.id,
        email=user.email,
        role=user.role,
        base_url=base_url(request),
        ip=client_ip(request),
    )


async def _mine(db: AsyncSession, conversation_id: uuid.UUID, user: User) -> ChatConversation:
    found = await store.owned(db, conversation_id, user.id)
    if found is None:
        raise HTTPException(status_code=404, detail="No such conversation")
    return found


def _live(conversation_id: uuid.UUID) -> engine.Run | None:
    run = engine.current(conversation_id)
    return run if run is not None and not run.finished else None


def _state(conversation: ChatConversation) -> State:
    if _live(conversation.id) is not None:
        return "running"
    state = store.state_of(conversation)
    if state == store.RUNNING:
        return "running"
    return "awaiting_confirmation" if state == store.AWAITING else "idle"


def _summary(conversation: ChatConversation) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        model=conversation.model,
        pictures=conversation.pictures,
        state=_state(conversation),
        spent_usd=round(conversation.spent_usd or 0.0, 6),
        created_at=_utc(conversation.created_at),
        updated_at=_utc(conversation.updated_at),
    )


def _chip(call: ChatToolCall) -> ToolChip:
    result = call.result or {}
    return ToolChip(
        id=call.tool_use_id,
        name=call.name,
        input=call.input or {},
        state=call.state,
        needs_confirm=call.needs_confirm,
        ok=call.state in ("ran", "approved"),
        summary=str(result.get("summary") or ""),
        # The same sentence the live `confirm` event carried (it never
        # raises, whatever arguments Claude wrote).
        ask=tools.describe(call.name, call.input or {}) if call.needs_confirm else None,
        images=[PictureRef(**picture) for picture in result.get("images") or []],
        artifact=ArtifactRef(name=call.artifact_name) if call.artifact_name else None,
    )


def _said(content: list[dict[str, Any]]) -> str:
    """What Claude wrote in one answer: its text blocks. Its thinking is
    kept for the API and is not for showing."""
    parts = [str(block.get("text") or "") for block in content if block.get("type") == "text"]
    return "\n\n".join(part for part in parts if part.strip())


def _display(
    conversation: ChatConversation, rows: list[ChatMessage], calls: list[ChatToolCall]
) -> ConversationOut:
    """The conversation as the panel shows it, from the stored rows alone."""
    by_turn: dict[uuid.UUID, list[ToolChip]] = {}
    for call in calls:
        by_turn.setdefault(call.message_id, []).append(_chip(call))
    shown: list[MessageOut] = []
    for row in rows:
        if row.role == "user":
            if row.text is None:
                continue  # tool results going back to Claude: not something anyone said
            shown.append(
                MessageOut(
                    id=row.id,
                    seq=row.seq,
                    role="user",
                    text=row.text,
                    context=row.context,
                    created_at=_utc(row.created_at),
                )
            )
        else:
            said = _said(row.content or [])
            if row.stop_reason == engine.STOP and said == engine.STOP_MARK:
                # Stopped before a word arrived. The row is there for
                # Claude's sake (see engine.STOP_MARK); the operator never
                # saw an answer, and is not shown one now.
                continue
            shown.append(
                MessageOut(
                    id=row.id,
                    seq=row.seq,
                    role="assistant",
                    text=said,
                    tools=by_turn.get(row.id, []),
                    stop_reason=row.stop_reason,
                    cost_usd=row.cost_usd,
                    created_at=_utc(row.created_at),
                )
            )
    run = engine.current(conversation.id)
    following: RunOut | None = None
    if run is not None:
        first = run.events[0] if run.events else None
        began = first.data.get("seq") if first is not None and first.name == "start" else None
        following = RunOut(
            active=not run.finished,
            events=len(run.events),
            seq=began if isinstance(began, int) else None,
        )
    summary = _summary(conversation)
    last_turn = (
        LastTurn(reason=conversation.turn_reason, message=conversation.turn_message)
        if conversation.turn_reason
        else None
    )
    if summary.state != "running" and store.turn_was_lost(conversation):
        # The row still says a turn is running and none can be: the server
        # was restarted under it. How it ended was never written, so the
        # row's reason is the turn before's, and showing that would put
        # "finished normally" under a question with no answer.
        last_turn = LastTurn(reason="error", message=engine.STOPPED)
    return ConversationOut(
        **summary.model_dump(),
        input_tokens=conversation.input_tokens or 0,
        output_tokens=conversation.output_tokens or 0,
        last_turn=last_turn,
        run=following,
        messages=shown,
        pending=[
            PendingOut(
                id=call.tool_use_id,
                name=call.name,
                input=call.input or {},
                summary=tools.describe(call.name, call.input or {}),
            )
            for call in calls
            if call.state == "pending"
        ],
    )


# --- the stream ---------------------------------------------------------------


def _frame(event: engine.Event) -> bytes:
    """One event as the browser's stream reader takes it. The JSON is one
    line (a line break inside `data:` would end the field), and `id:` is
    the event's index, which is what a reader sends back to resume."""
    data = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.index}\nevent: {event.name}\ndata: {data}\n\n".encode()


async def _relay(run: engine.Run, after: int) -> AsyncIterator[bytes]:
    # Something at once, so the headers leave and whatever sits in front
    # sees a response that has started rather than a request still pending.
    yield b": open\n\n"
    async for event in run.relay(after, quiet_s=KEEPALIVE_S):
        yield _frame(event) if event is not None else b": keep-alive\n\n"


def _stream(run: engine.Run, after: int = 0) -> StreamingResponse:
    return StreamingResponse(
        _relay(run, after), media_type="text/event-stream", headers=STREAM_HEADERS
    )


async def _started(starting: Awaitable[engine.Run]) -> engine.Run:
    """The engine's reasons for not starting a turn, as this API's errors."""
    try:
        return await starting
    except engine.Busy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except engine.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except engine.ChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# --- routes -------------------------------------------------------------------


@router.get("/status", response_model=ChatStatus)
async def chat_status(user: CurrentUser, db: DbDep) -> ChatStatus:
    """Whether the panel can be used here, by this account, right now."""
    config = await load_ai_edit_config(db)
    missing = await engine.unavailable_now()
    configured = bool(config.api_key_sealed)
    problem = missing
    if problem is None and not configured:
        problem = engine.NO_KEY
    if problem is None and not config.enabled:
        problem = engine.SWITCHED_OFF
    return ChatStatus(
        available=missing is None,
        configured=configured,
        enabled=config.enabled,
        problem=problem,
        can_chat=user.role != "readonly",
        model=engine.model_for(config),
        models=engine.models(),
        budget_usd=config.monthly_budget_usd,
        spent_this_month_usd=round(await spend.spent_this_month(db), 4),
    )


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(user: CurrentUser, db: DbDep) -> list[ConversationSummary]:
    """The caller's own conversations, most recently used first."""
    return [_summary(found) for found in await store.listing(db, user.id)]


@router.post("/conversations", response_model=ConversationOut, status_code=201)
async def create_conversation(
    user: CurrentUser, db: DbDep, body: ConversationIn | None = None
) -> ConversationOut:
    """Start an empty conversation on the model the Security page names.
    The model is fixed here: changing the setting later moves new
    conversations, never this one."""
    missing = await engine.unavailable_now()
    if missing:
        raise HTTPException(status_code=503, detail=missing)
    config = await load_ai_edit_config(db)
    conversation = await store.create(
        db,
        user_id=user.id,
        model=engine.model_for(config),
        pictures=body.pictures if body is not None else True,
    )
    await db.commit()
    await db.refresh(conversation)
    log.info("chat.started", conversation=str(conversation.id)[:8], by=user.email)
    return _display(conversation, [], [])


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def read_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, db: DbDep
) -> ConversationOut:
    """Everything the panel needs to draw a conversation again after a
    reload: what was said, each tool and what became of it, the calls still
    waiting for a yes or no, how the last turn ended, and whether a turn is
    running that can be followed."""
    conversation = await _mine(db, conversation_id, user)
    return _display(
        conversation,
        await store.messages(db, conversation_id),
        await store.tool_calls(db, conversation_id),
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationSummary)
async def update_conversation(
    conversation_id: uuid.UUID, body: ConversationPatch, user: CurrentUser, db: DbDep
) -> ConversationSummary:
    """The pictures switch. It decides what tool answers carry from now
    on, a turn already running included (the turn asks again for each tool,
    and once more as it stores the tool's answer); what Claude has already
    been sent stays sent."""
    conversation = await _mine(db, conversation_id, user)
    conversation.pictures = body.pictures
    await db.commit()
    return _summary(conversation)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, user: CurrentUser, db: DbDep) -> None:
    conversation = await _mine(db, conversation_id, user)
    if _live(conversation_id) is not None:
        # The turn would go on writing rows and pictures for a conversation
        # that no longer exists.
        raise HTTPException(
            status_code=409,
            detail="Claude is still working in this conversation. Delete it when it has finished.",
        )
    await store.remove(db, conversation)
    # Its last turn's events hold what was said in it; they go too.
    engine.forget(conversation_id)
    log.info("chat.deleted", conversation=str(conversation_id)[:8], by=user.email)


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageIn,
    request: Request,
    user: CurrentUser,
    db: DbDep,
) -> StreamingResponse:
    """Say something to Claude. The answer is an event stream (see the
    module docstring and docs/chat.md); calls that were waiting for a yes
    are answered "no" by writing instead."""
    await _mine(db, conversation_id, user)
    await db.close()
    run = await _started(
        engine.send(
            _factory(request),
            conversation_id,
            _caller(request, user),
            text=body.text,
            context=body.context,
            pictures=body.pictures,
        )
    )
    return _stream(run)


@router.post("/conversations/{conversation_id}/confirm")
async def confirm_calls(
    conversation_id: uuid.UUID,
    body: ConfirmIn,
    request: Request,
    user: CurrentUser,
    db: DbDep,
) -> StreamingResponse:
    """Say yes or no to calls that are waiting, and carry the same turn on
    as an event stream. Answering only some of them is allowed: the stream
    then ends `awaiting_confirmation` again, and Claude hears nothing until
    every one has its answer.

    Each yes and no goes on the audit log, written by the turn itself in
    the transaction that records the decision (a yes before its tool is
    run). Not here: a row written from this handler after the turn had
    started could fail, and the change it was about go ahead unrecorded."""
    await _mine(db, conversation_id, user)
    decisions: dict[str, bool] = {}
    for decision in body.decisions:
        if decisions.setdefault(decision.tool_use_id, decision.approve) != decision.approve:
            raise HTTPException(
                status_code=400, detail="The same request was answered both yes and no."
            )
    await db.close()
    run = await _started(
        engine.confirm(_factory(request), conversation_id, _caller(request, user), decisions)
    )
    return _stream(run)


@router.post("/conversations/{conversation_id}/stop", response_model=ConversationSummary)
async def stop_turn(
    conversation_id: uuid.UUID, user: CurrentUser, db: DbDep
) -> ConversationSummary:
    """Stop the turn that is running: an answer the operator no longer
    wants must stop costing money. The request to Claude is dropped at
    once and no other is made; a tool that is already running finishes
    (a change is never cut in half) and no other starts; calls waiting
    for a yes go on waiting. The turn then ends, on every stream following
    it, with `done {"reason": "stopped"}`.

    With nothing running this changes nothing and answers the same: a
    second press, or one that arrives as the turn ends by itself, is not
    an error. The answer is the conversation as it stands a moment later:
    usually settled (`idle`, or `awaiting_confirmation`), still `running`
    if a tool is taking its time to finish. Whoever needs the end itself
    reads it from the stream.
    """
    await _mine(db, conversation_id, user)
    run = engine.stop(conversation_id)
    if run is not None:
        # Given back before the wait, as before a stream: the turn needs
        # its own sessions to write how it ended.
        await db.close()
        # Out of patience means a tool is still finishing, and the summary
        # will say `running`: true, and not a failure of the stop.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(run.wait(), STOP_WAIT_S)
        log.info("chat.stopped", conversation=str(conversation_id)[:8], by=user.email)
    return _summary(await _mine(db, conversation_id, user))


@router.get("/conversations/{conversation_id}/events")
async def follow_turn(
    conversation_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    after: int = Query(default=0, ge=0),
) -> Response:
    """Follow the turn this server has for the conversation: every event
    from index `after` on (so `after` is how many the page already has),
    then the rest as they happen. A browser's own reconnect header
    (`Last-Event-ID`) is honoured the same way. 204 when there is nothing
    to follow: the turn ended long ago, or the server was restarted, and
    the stored conversation is all there is."""
    await _mine(db, conversation_id, user)
    await db.close()
    run = engine.current(conversation_id)
    if run is None:
        return Response(status_code=204)
    resumed = request.headers.get("last-event-id", "").strip()
    if resumed.isdigit():
        after = int(resumed) + 1
    return _stream(run, after)


@router.get("/conversations/{conversation_id}/blobs/{sha}")
async def picture(
    conversation_id: uuid.UUID, sha: str, user: CurrentUser, db: DbDep, settings: SettingsDep
) -> FileResponse:
    """A picture a tool returned in this conversation: the bytes Claude was
    sent, or, with pictures off, the bytes kept back for the operator."""
    await _mine(db, conversation_id, user)
    found = await asyncio.to_thread(
        store.find_blob, store.home(conversation_id, settings), sha.lower()
    )
    if found is None:
        raise HTTPException(status_code=404, detail="That picture is no longer stored")
    path, media_type = found
    return FileResponse(
        path,
        media_type=media_type,
        # Named by the hash of its own bytes, so it can never change; a
        # day, like the other previews, so one from a conversation since
        # deleted does not sit in a browser's cache for long.
        headers={**INERT, "Cache-Control": "private, max-age=86400, immutable"},
    )


@router.get("/conversations/{conversation_id}/artifacts/{tool_use_id}")
async def artifact(
    conversation_id: uuid.UUID,
    tool_use_id: str,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
) -> FileResponse:
    """A timeline a tool built in this conversation, as a download."""
    await _mine(db, conversation_id, user)
    call = next(
        (
            found
            for found in await store.tool_calls(db, conversation_id)
            if found.tool_use_id == tool_use_id and found.artifact_path
        ),
        None,
    )
    if call is None or call.artifact_path is None:
        raise HTTPException(status_code=404, detail="No such file in this conversation")
    path = await asyncio.to_thread(store.artifact_file, call.artifact_path, settings)
    if path is None:
        raise HTTPException(status_code=404, detail="That file is no longer stored")
    name = call.artifact_name or "timeline.xml"
    # The name comes from the cut's title and may carry an accent: a plain
    # one for browsers that only read `filename`, the exact one beside it.
    plain = "".join(ch if ch.isascii() and ch not in '"\\' else "_" for ch in name)
    return FileResponse(
        path,
        media_type="application/xml",
        headers={
            **INERT,
            "Content-Disposition": (
                f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name)}"
            ),
        },
    )
