"""The tools Claude has in the chat panel: the connector's, run in process.

Nothing is reimplemented. `framefound/mcp` already defines what Claude may
do with the catalogue, with its own permission checks, and the panel calls
those same functions through the `MCPServer` object rather than over HTTP:
`list_tools()` for the definitions, `call_tool()` to run one, with the
signed-in user bound as the caller (`principal_for`). So a tool behaves in
the panel exactly as it does for a panel token with the read and tag
scopes: a read-only account cannot write, and `start_api_review` is still
admin only.

Three things differ from the connector, all about the answer rather than
the tool:

- `CONFIRM_FIRST` names the tools that change the catalogue or spend
  money. The engine does not run them until the operator says yes.
- A timeline (`ARTIFACT_FIELDS`) is taken out of the answer and handed to
  the operator as a download. Claude is told its file name.
- With pictures switched off, a preview is shown to the operator and not
  sent to Claude.

**Adding a tool only the panel has** (a later phase: start a folder
review, ask the local Editor for a draft) is one function and one entry,
in the module that owns the feature:

    async def start_folder_review(args: dict[str, Any]) -> str:
        who = principal()                       # framefound.mcp.auth
        async with open_session() as db:        # framefound.mcp.auth
            ...
        return "Queued 120 clips for review."   # what Claude is told

    tools.register(tools.ChatTool(
        name="start_folder_review",
        description="Start a local review of every clip in a folder. Call it when ...",
        input_schema={"type": "object", "properties": {...}, "required": [...]},
        run=start_folder_review,
        confirm=True,                           # ask the operator first
        describe=lambda args: f"Review every clip in {args.get('folder')}",
    ))

Raise `ToolProblem("plain sentence")` for a failure Claude should read.
Such tools are deliberately not added to `mcp.server.build_server()`: the
external connector's tool list is pinned by a test and by docs/mcp.md, and
must not change because the panel gained something.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger()

# Asked first, every time: these change what the catalogue holds as fact
# (a decided tag teaches the learner) or spend from the monthly budget.
# `propose_tags` is not here: a proposal is a suggestion in the review
# queue, which is where a yes or no is asked for anyway. Nor are the
# timeline builders: they write cut history, not the catalogue.
CONFIRM_FIRST = frozenset({"decide_tag", "decide_tags", "start_api_review"})

# tool -> the field of its answer that is a whole file for the operator.
ARTIFACT_FIELDS = {"build_rough_cut": "xml", "build_selects": "xml"}

# A tool answer longer than this is cut, and says so. About 20,000 tokens:
# a full transcript or a hundred verbose items would otherwise be re-read,
# and re-billed, on every later request of the conversation.
MAX_RESULT_CHARS = 60_000

PICTURES_OFF = (
    "Pictures are switched off in this conversation, so this picture was shown to the "
    "operator and not sent to you. Work from its caption, or ask them to switch pictures on."
)
TOOL_FAILED = (
    "The tool failed inside FrameFound. This is a fault in FrameFound, not in how you "
    "called it; tell the operator and do not retry it more than once."
)


class ToolProblem(Exception):
    """A failure a chat-only tool saw coming. Its message is what Claude
    reads, so it is a plain sentence."""


@dataclass(frozen=True)
class Artifact:
    """A file a tool made for the operator rather than for Claude."""

    filename: str
    text: str


@dataclass(frozen=True)
class ToolOutcome:
    """What running a tool produced.

    `blocks` is what Claude is told: text and image blocks in the Claude
    API's own shape. `shown` is pictures for the operator's eyes only (the
    pictures switch is off). `artifact` is a file for them to download.
    """

    blocks: list[dict[str, Any]]
    is_error: bool = False
    artifact: Artifact | None = None
    shown: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ChatTool:
    """A tool only the panel has. See the module docstring."""

    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[str | ToolOutcome]]
    confirm: bool = False
    # The sentence on the confirm chip, from the arguments.
    describe: Callable[[dict[str, Any]], str] | None = None


CHAT_ONLY: dict[str, ChatTool] = {}


def register(tool: ChatTool) -> None:
    """Add a panel-only tool. Conversations started afterwards have it;
    one already under way keeps the tool list it began with."""
    CHAT_ONLY[tool.name] = tool


def needs_confirm(name: str) -> bool:
    own = CHAT_ONLY.get(name)
    return name in CONFIRM_FIRST or (own is not None and own.confirm)


# --- availability -----------------------------------------------------------


def problem() -> str | None:
    """Why the tools cannot be used on this install, or None."""
    try:
        import framefound.mcp.server  # noqa: F401 - the import is the test
    except ImportError:
        return (
            "The chat panel needs the connector's tools, which this server was installed "
            "without (the `mcp` extra)."
        )
    return None


def available() -> bool:
    return problem() is None


_SERVER: Any = None


def _server() -> Any:
    """The connector's server object, built once. Building it does no I/O;
    it is kept so every conversation sees the tools in the same order."""
    global _SERVER
    if _SERVER is None:
        from framefound.mcp.server import build_server

        _SERVER = build_server()
    return _SERVER


def principal_for(*, user_id: uuid.UUID, email: str, role: str, base_url: str) -> Any:
    """The signed-in user as the connector knows a caller: what a panel
    token with the read and tag scopes would be. The tools' own checks then
    apply unchanged, the read-only role and the admin-only review included.
    `base_url` is how their browser reached us, so links in answers open."""
    from framefound.auth import panel_tokens
    from framefound.mcp.auth import Principal

    return Principal(
        user_id=user_id,
        email=email,
        role=role,
        token_name="chat panel",  # noqa: S106 - a label for logs, not a credential
        scopes=frozenset({panel_tokens.READ, panel_tokens.TAG}),
        base_url=base_url,
    )


# --- definitions ------------------------------------------------------------


async def specs() -> list[dict[str, Any]]:
    """Every tool as the Claude API takes it, in a fixed order: the
    connector's in the order it registers them, then the panel's own by
    name. The order is part of what is cached, so it must not wander."""
    listed = await _server().list_tools()
    found = [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.input_schema,
        }
        for tool in listed
    ]
    taken = {spec["name"] for spec in found}
    for name in sorted(CHAT_ONLY):
        if name in taken:
            raise RuntimeError(f"Chat tool {name!r} has the name of a connector tool")
        own = CHAT_ONLY[name]
        found.append(
            {"name": own.name, "description": own.description, "input_schema": own.input_schema}
        )
    return found


def describe(name: str, args: dict[str, Any]) -> str:
    """What saying yes will do, for the confirm chip: one plain sentence
    from the arguments, without looking anything up.

    Never raises. Claude wrote the arguments and nothing has checked them
    yet (the tool does, when it is run), so a list that arrived as a number
    or a missing folder name must cost the operator neither the chip nor
    the turn: the sentence falls back to the tool's name, and the tool
    itself tells Claude what was wrong with the call if it is approved.
    """
    try:
        return str(_describe(name, args))
    except Exception:
        log.warning("chat.describe_failed", tool=name, exc_info=True)
        return f"Run {name}"


def _describe(name: str, args: dict[str, Any]) -> str:
    own = CHAT_ONLY.get(name)
    if own is not None and own.describe is not None:
        return own.describe(args)
    if name == "decide_tag":
        verb = "Confirm" if args.get("accept") else "Reject"
        return f"{verb} the tag “{args.get('tag', '?')}” on item {args.get('id', '?')}"
    if name == "decide_tags":
        yes = len(args.get("accept") or [])
        no = len(args.get("reject") or [])
        parts = [f"confirm it on {yes}" if yes else "", f"reject it on {no}" if no else ""]
        doing = " and ".join(part for part in parts if part) or "no items given"
        return f"Tag “{args.get('tag', '?')}”: {doing}"
    if name == "start_api_review":
        chosen = args.get("asset_ids") or []
        limit = args.get("limit") or 0
        what = f"{len(chosen)} chosen items" if chosen else f"up to {limit or 'a batch of'} items"
        return f"Start a paid Claude tag review of {what} (spends from the monthly budget)"
    return f"Run {name}"


# --- running one ------------------------------------------------------------


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _capped(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return (
        text[:MAX_RESULT_CHARS]
        + f"\n[Cut here: the answer was {len(text):,} characters and only the first "
        f"{MAX_RESULT_CHARS:,} are shown. Ask for less at a time: a smaller limit, "
        "a time range, fewer fields.]"
    )


def _plain(name: str, message: str) -> str:
    """The connector's error without its own preamble."""
    prefix = f"Error executing tool {name}: "
    return message[len(prefix) :] if message.startswith(prefix) else message


def _artifact_name(name: str, data: dict[str, Any]) -> str:
    """The name the download is offered under: the tool's own (it is made
    from the cut's title), held to letters, digits, spaces, dashes and
    underscores. The file on disk is not named by it."""
    given = str(data.get("filename") or "")
    stem = given[:-4] if given.lower().endswith(".xml") else given
    kept = "".join(ch if ch.isalnum() or ch in " -_" else "-" for ch in stem).strip()[:120]
    return f"{kept or name}.xml"


def held_back(outcome: ToolOutcome) -> ToolOutcome:
    """The same outcome with its pictures kept from Claude: moved to
    `shown`, for the operator, with a sentence in their place saying so.
    An outcome with no picture in it is returned as it is."""
    images = [block for block in outcome.blocks if block.get("type") == "image"]
    if not images:
        return outcome
    rest = [block for block in outcome.blocks if block.get("type") != "image"]
    return ToolOutcome(
        blocks=[_text(PICTURES_OFF), *rest],
        is_error=outcome.is_error,
        artifact=outcome.artifact,
        shown=[*outcome.shown, *images],
    )


def _said_first(text: str) -> str:
    """What a connector answer says before its JSON begins.

    `find_shots` puts "20 of 143 shots" there, and the 143 is in no field
    of the JSON: an answer reduced to its structure would tell Claude about
    twenty shots and hide that more matched.
    """
    lines = text.splitlines()
    for at, line in enumerate(lines):
        if line.lstrip()[:1] in ("{", "["):
            return "\n".join(lines[:at]).strip()
    return text.strip()


def _convert(name: str, result: Any, *, pictures: bool) -> ToolOutcome:
    """A connector answer as Claude API blocks.

    A structured answer is sent as compact JSON rather than the connector's
    indented text: the same facts in about two thirds of the tokens, and
    this is re-read on every later request. Anything the connector wrote in
    front of the JSON is kept in front of it.
    """
    images: list[dict[str, Any]] = []
    texts: list[str] = []
    for block in result.content:
        kind = getattr(block, "type", "")
        if kind == "text":
            texts.append(block.text)
        elif kind == "image":
            images.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.mime_type or "image/jpeg",
                        "data": block.data,
                    },
                }
            )

    artifact: Artifact | None = None
    structured = result.structured_content
    if isinstance(structured, dict) and not images:
        data = dict(structured)
        held = ARTIFACT_FIELDS.get(name)
        if held and isinstance(data.get(held), str) and data[held]:
            artifact = Artifact(filename=_artifact_name(name, data), text=data[held])
            data[held] = (
                f"Delivered to the operator as a download named {artifact.filename}, "
                "in the chat panel. Not repeated here."
            )
        texts = [
            _said_first(texts[0]) if texts else "",
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        ]

    blocks: list[dict[str, Any]] = list(images)
    body = _capped("\n".join(text for text in texts if text))
    if body:
        blocks.append(_text(body))
    if not blocks:
        blocks.append(_text("(The tool returned nothing.)"))
    outcome = ToolOutcome(blocks=blocks, is_error=bool(result.is_error), artifact=artifact)
    return outcome if pictures else held_back(outcome)


async def run(name: str, args: Any, *, pictures: bool) -> ToolOutcome:
    """Run one tool as the bound caller and say what came of it.

    Never raises for anything the tool did: a failure is an outcome with
    `is_error`, because Claude must be told about every call it made. The
    caller binds the principal (`framefound.mcp.auth.bound`) first.
    """
    if not isinstance(args, dict):
        return ToolOutcome([_text("The arguments must be a JSON object.")], is_error=True)

    own = CHAT_ONLY.get(name)
    if own is not None:
        try:
            answer = await own.run(args)
        except ToolProblem as exc:
            return ToolOutcome([_text(str(exc))], is_error=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("chat.tool_crashed", tool=name)
            return ToolOutcome([_text(TOOL_FAILED)], is_error=True)
        if isinstance(answer, ToolOutcome):
            return answer if pictures else held_back(answer)
        return ToolOutcome([_text(_capped(str(answer)) or "(The tool returned nothing.)")])

    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    try:
        result = await _server().call_tool(name, args)
    except UnexpectedToolError as exc:
        # A crash inside the tool. The connector withholds the detail from
        # its caller; so does this, and the log keeps it.
        log.error("chat.tool_crashed", tool=name, exc_info=exc.__cause__ or exc)
        return ToolOutcome([_text(TOOL_FAILED)], is_error=True)
    except ToolError as exc:
        # Anticipated: an unknown id, arguments that do not fit, a scope the
        # caller lacks. The sentence is written for Claude to read.
        return ToolOutcome([_text(_plain(name, str(exc)))], is_error=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("chat.tool_crashed", tool=name)
        return ToolOutcome([_text(TOOL_FAILED)], is_error=True)
    return _convert(name, result, pictures=pictures)
