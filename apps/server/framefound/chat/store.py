"""Where a conversation is kept: rows for the turns, files for the rest.

The rows (`chat_conversations`, `chat_messages`, `chat_tool_calls`) hold
each turn as the Claude API has it. Three things are files instead, under
`<data dir>/chat`, on the fast local disk with the thumbnails:

- **Pictures** Claude was shown: `<conversation>/blobs/<sha256>.jpg`, the
  exact bytes that were sent. A conversation is replayed to the API on
  every request, and a preview re-encoded later can come out as different
  bytes, which the API treats as a different conversation. So what was sent
  is kept, and a turn's row carries only `{"type": "image", "blob": sha}`.
- **Timelines** a tool built: `<conversation>/artifacts/<tool call>.xml`.
  They go to the operator as a download; Claude is told the file's name,
  not sent tens of thousands of tokens of XML it has no use for.
- **Prefixes**: `prefixes/<sha256>.json`, the instructions and tool list a
  conversation was started with. Kept so an update to FrameFound, which may
  reword a tool, cannot change the opening of a conversation already under
  way; many conversations share one file.

Deleting a conversation deletes its folder. Prefix files are small and
shared, and are left.
"""

import base64
import hashlib
import json
import re
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.config import Settings, get_settings
from framefound.db.models import ChatConversation, ChatMessage, ChatToolCall

IDLE = "idle"
RUNNING = "running"
AWAITING = "awaiting_confirmation"

# A turn in progress touches its conversation every few seconds
# (engine.BEAT_S). One that has not been touched for this long has stopped
# without saying so (its task was cancelled, or it could not write how it
# ended), and must not hold the conversation for ever: it reads as idle
# and the next message takes it over. A turn that belonged to an earlier
# process does not wait this long: see PROCESS_STARTED.
STALE_AFTER_S = 300

TITLE_CHARS = 80
SUMMARY_CHARS = 240

_SHA = re.compile(r"^[0-9a-f]{64}$")
_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}
GONE = "[A picture that was shown here is no longer stored on this server.]"


def now() -> datetime:
    return datetime.now(UTC)


# When this process began, as near as this module can tell: it is loaded
# with the API, before any turn can start. Turns run inside the one API
# process and nowhere else, so a conversation marked `running` before this
# moment was being worked on by a process that is gone, and nothing will
# ever finish it. That is known at once; waiting out STALE_AFTER_S for it
# only told the operator "Claude is still working" for five minutes after
# every restart or update. Read where it is used, never copied, so a test
# can move it.
PROCESS_STARTED = now()


def lost_before(moment: datetime) -> datetime:
    """A `running` conversation last touched before this is not running:
    its turn went quiet for longer than a live one ever does, or belonged
    to a process that ended before this one began."""
    return max(moment - timedelta(seconds=STALE_AFTER_S), PROCESS_STARTED)


def _aware(moment: datetime) -> datetime:
    """SQLite hands timestamps back without a zone; they were written in UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# --- files ------------------------------------------------------------------


def root(settings: Settings | None = None) -> Path:
    return Path((settings or get_settings()).data_dir) / "chat"


def home(conversation_id: uuid.UUID, settings: Settings | None = None) -> Path:
    """The folder that holds one conversation's pictures and timelines."""
    return root(settings) / str(conversation_id)


def _write(path: Path, data: bytes) -> None:
    """Whole or not at all: a half-written picture would be replayed to the
    API as if it were the one that was sent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_bytes(data)
    partial.replace(path)


def save_blob(folder: Path, data: bytes, media_type: str) -> str:
    sha = hashlib.sha256(data).hexdigest()
    path = folder / "blobs" / f"{sha}.{_EXTENSIONS.get(media_type, 'bin')}"
    if not path.is_file():
        _write(path, data)
    return sha


def find_blob(folder: Path, sha: str) -> tuple[Path, str] | None:
    """The file for a picture and its media type, or None. `sha` comes from
    a URL, so it is checked before it goes near a path."""
    if not _SHA.match(sha):
        return None
    for media_type, extension in _EXTENSIONS.items():
        path = folder / "blobs" / f"{sha}.{extension}"
        if path.is_file():
            return path, media_type
    return None


def seal(blocks: list[dict[str, Any]], folder: Path) -> list[dict[str, Any]]:
    """Content blocks as they are stored: every picture's bytes moved to a
    file and replaced by a reference to it. Looks inside tool results."""
    sealed: list[dict[str, Any]] = []
    for block in blocks:
        source = block.get("source")
        if (
            block.get("type") == "image"
            and isinstance(source, dict)
            and source.get("type") == "base64"
        ):
            media_type = str(source.get("media_type") or "image/jpeg")
            data = base64.b64decode(str(source.get("data") or ""))
            sealed.append(
                {
                    "type": "image",
                    "blob": save_blob(folder, data, media_type),
                    "media_type": media_type,
                }
            )
        elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            sealed.append({**block, "content": seal(block["content"], folder)})
        else:
            sealed.append(block)
    return sealed


def unseal(blocks: list[dict[str, Any]], folder: Path) -> list[dict[str, Any]]:
    """Content blocks as the API takes them: every reference turned back
    into the bytes that were first sent. The same input gives the same
    output on every request, which is the point."""
    opened: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") == "image" and "blob" in block:
            found = find_blob(folder, str(block["blob"]))
            if found is None:
                opened.append({"type": "text", "text": GONE})
                continue
            path, media_type = found
            opened.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
                    },
                }
            )
        elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            opened.append({**block, "content": unseal(block["content"], folder)})
        else:
            opened.append(block)
    return opened


def pictures_in(blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The stored pictures among some sealed blocks, for showing them."""
    return [
        {"blob": str(block["blob"]), "media_type": str(block.get("media_type") or "image/jpeg")}
        for block in blocks
        if block.get("type") == "image" and "blob" in block
    ]


def summary_of(blocks: list[dict[str, Any]]) -> str:
    """One line of a tool's answer, for the chip in the panel."""
    for block in blocks:
        if block.get("type") == "text":
            text = " ".join(str(block.get("text") or "").split())
            if text:
                return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1] + "…"
    return ""


def safe_name(value: str, fallback: str = "file") -> str:
    """A tool-call id or a file name as one harmless path component. Both
    come from outside (the API, a tool's title), so neither is trusted with
    a slash or a dot-dot."""
    cleaned = _SAFE.sub("_", value)[:120].strip("_")
    return cleaned or fallback


def save_artifact(
    folder: Path, tool_use_id: str, text: str, settings: Settings | None = None
) -> str:
    """Keep a timeline a tool built. Returns its path relative to the data
    folder, which is what the row records."""
    path = folder / "artifacts" / f"{safe_name(tool_use_id, 'call')}.xml"
    _write(path, text.encode("utf-8"))
    data_dir = Path((settings or get_settings()).data_dir)
    return path.relative_to(data_dir).as_posix()


def artifact_file(relative_path: str, settings: Settings | None = None) -> Path | None:
    """The file behind a recorded artifact path, if it is still there and
    still inside the chat folder."""
    base = root(settings).resolve()
    path = (Path((settings or get_settings()).data_dir) / relative_path).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return None
    return path


def save_prefix(prefix: dict[str, Any], settings: Settings | None = None) -> str:
    """Keep the instructions and tool list a conversation starts with, and
    name them by their hash. The same prefix is the same file."""
    data = json.dumps(prefix, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    path = root(settings) / "prefixes" / f"{digest}.json"
    if not path.is_file():
        _write(path, data)
    return digest


def load_prefix(digest: str, settings: Settings | None = None) -> dict[str, Any] | None:
    """The prefix stored under a hash, or None when the file is gone or is
    not what its name says."""
    if not _SHA.match(digest):
        return None
    path = root(settings) / "prefixes" / f"{digest}.json"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != digest:
        return None
    loaded = json.loads(data)
    return loaded if isinstance(loaded, dict) else None


def discard_files(conversation_id: uuid.UUID, settings: Settings | None = None) -> None:
    shutil.rmtree(home(conversation_id, settings), ignore_errors=True)


# --- conversations ----------------------------------------------------------


async def create(
    db: AsyncSession, *, user_id: uuid.UUID, model: str, pictures: bool = True
) -> ChatConversation:
    """A new, empty conversation. Flushed, not committed."""
    conversation = ChatConversation(
        user_id=user_id, model=model, pictures=pictures, state=IDLE, state_at=now()
    )
    db.add(conversation)
    await db.flush()
    return conversation


async def owned(
    db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> ChatConversation | None:
    """The conversation, if it is this user's. Someone else's is None, the
    same as one that does not exist: conversations are private, admins
    included."""
    conversation = await db.get(ChatConversation, conversation_id)
    if conversation is None or conversation.user_id != user_id:
        return None
    return conversation


async def listing(db: AsyncSession, user_id: uuid.UUID, limit: int = 50) -> list[ChatConversation]:
    rows = await db.execute(
        select(ChatConversation)
        .where(ChatConversation.user_id == user_id)
        .order_by(ChatConversation.updated_at.desc(), ChatConversation.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def remove(
    db: AsyncSession, conversation: ChatConversation, settings: Settings | None = None
) -> None:
    """Delete a conversation, its turns, its tool calls and its files.
    Commits. The rows go first: files with no rows are litter, rows with no
    files are a conversation that cannot be replayed."""
    conversation_id = conversation.id
    # Spelled out rather than left to ON DELETE CASCADE: SQLite only
    # enforces foreign keys when asked to, and the tests run on SQLite.
    await db.execute(delete(ChatToolCall).where(ChatToolCall.conversation_id == conversation_id))
    await db.execute(delete(ChatMessage).where(ChatMessage.conversation_id == conversation_id))
    await db.delete(conversation)
    await db.commit()
    discard_files(conversation_id, settings)


def state_of(conversation: ChatConversation, at: datetime | None = None) -> str:
    """The conversation's state as the operator should see it: a `running`
    that nothing has touched for STALE_AFTER_S, or that was last touched
    before this process began, is a turn that died (`lost_before`)."""
    if conversation.state != RUNNING:
        return conversation.state
    touched = conversation.state_at
    if touched is None:
        return IDLE
    if _aware(touched) < lost_before(at or now()):
        return IDLE
    return RUNNING


def turn_was_lost(conversation: ChatConversation, at: datetime | None = None) -> bool:
    """Whether the conversation's last turn died without ending: the row
    still says `running` and nothing can be running it. How that turn
    "ended" was never written, so what the row says about its last turn is
    the turn before."""
    return conversation.state == RUNNING and state_of(conversation, at) != RUNNING


async def claim(db: AsyncSession, conversation_id: uuid.UUID, allowed: tuple[str, ...]) -> bool:
    """Take the conversation for a turn: `running`, in one statement, only
    if it is in one of `allowed` states or its last turn died. False when
    another turn has it. Commits.

    One statement so two messages sent at the same moment cannot both win;
    the database decides, not a read followed by a write.
    """
    moment = now()
    # The same line `state_of` draws, so a conversation that reads as free
    # can be taken, and one that reads as running cannot.
    stale = lost_before(moment)
    result: Any = await db.execute(
        update(ChatConversation)
        .where(
            ChatConversation.id == conversation_id,
            ChatConversation.state.in_(allowed)
            | (
                (ChatConversation.state == RUNNING)
                & (ChatConversation.state_at.is_(None) | (ChatConversation.state_at < stale))
            ),
        )
        .values(state=RUNNING, state_at=moment)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return bool(result.rowcount)


async def beat(db: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Say the turn is still alive. Commits."""
    await db.execute(
        update(ChatConversation)
        .where(ChatConversation.id == conversation_id, ChatConversation.state == RUNNING)
        .values(state_at=now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()


# --- turns ------------------------------------------------------------------


async def messages(db: AsyncSession, conversation_id: uuid.UUID) -> list[ChatMessage]:
    rows = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.seq)
    )
    return list(rows.scalars())


async def last_message(db: AsyncSession, conversation_id: uuid.UUID) -> ChatMessage | None:
    rows = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.seq.desc())
        .limit(1)
    )
    return rows.scalars().first()


async def append(
    db: AsyncSession,
    conversation: ChatConversation,
    role: str,
    content: list[dict[str, Any]],
    *,
    text: str | None = None,
    context: dict[str, Any] | None = None,
    usage: dict[str, int] | None = None,
    cost_usd: float | None = None,
    stop_reason: str | None = None,
) -> ChatMessage:
    """Add a turn at the end. `content` is already sealed. Flushed, not
    committed. Turns are only ever added: nothing here edits one."""
    latest = (
        await db.execute(
            select(func.max(ChatMessage.seq)).where(ChatMessage.conversation_id == conversation.id)
        )
    ).scalar_one()
    message = ChatMessage(
        conversation_id=conversation.id,
        seq=(latest or 0) + 1,
        role=role,
        content=content,
        text=text,
        context=context,
        usage=usage,
        cost_usd=cost_usd,
        stop_reason=str(stop_reason)[:30] if stop_reason else None,
    )
    db.add(message)
    conversation.updated_at = now()
    if role == "user" and text and not conversation.title:
        first_line = " ".join(text.strip().splitlines()[0].split()) if text.strip() else ""
        conversation.title = (
            first_line if len(first_line) <= TITLE_CHARS else first_line[: TITLE_CHARS - 1] + "…"
        )
    await db.flush()
    return message


async def tool_calls(
    db: AsyncSession, conversation_id: uuid.UUID, message_id: uuid.UUID | None = None
) -> list[ChatToolCall]:
    """Tool calls in the order Claude asked for them; for one assistant
    turn when `message_id` is given."""
    stmt = (
        select(ChatToolCall)
        .join(ChatMessage, ChatMessage.id == ChatToolCall.message_id)
        .where(ChatToolCall.conversation_id == conversation_id)
    )
    if message_id is not None:
        stmt = stmt.where(ChatToolCall.message_id == message_id)
    rows = await db.execute(stmt.order_by(ChatMessage.seq, ChatToolCall.position))
    return list(rows.scalars())


async def pending_calls(db: AsyncSession, conversation_id: uuid.UUID) -> list[ChatToolCall]:
    """The calls waiting for the operator's yes or no."""
    return [call for call in await tool_calls(db, conversation_id) if call.state == "pending"]
