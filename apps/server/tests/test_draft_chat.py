"""The chat panel's tool for the local Editor (framefound/nle/draft_chat.py; design L5 S8).

`local_cut_draft` is called with a session and a user id, on the real lane:
the Mac is test_cut_drafts.py's `Site` (the real `ask()` in front of
`fake.FakeModelServer`, which answers the Editor with `cut_editor.demo_answer`),
and the half minute the tool waits is an injected `sleep` that, where a test
says so, lets the lane run one job (`jobs.run_next`) while Claude's turn
waits. The panel's side goes through C1's own dispatcher (`chat.tools.run`)
with the caller bound as the chat engine binds it. No conversation is
started and Claude is never called; nothing starts a process or reaches a
network.
"""

import ast
import json
import re
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import test_cut_drafts
from sqlalchemy import func, select

# The Mac, its clips and the rule that nothing runs: shared with the job's tests.
from test_cut_drafts import (  # noqa: F401 - `_nothing_runs` is an autouse fixture
    BRIEF,
    LARGE,
    NIGHT,
    SECRET,
    Site,
    _answer,
    _drain,
    _nothing_runs,
)

from framefound.ai import cut_editor
from framefound.api.v1 import cuts as api
from framefound.auth import service
from framefound.chat import tools
from framefound.config import Settings, get_settings
from framefound.db.models import Cut, CutDraft, CutPiece, LocalAiJob
from framefound.local_models import jobs
from framefound.mcp import auth
from framefound.nle import draft_chat, drafts, roughcut, shotlist

PASSWORD = "a-strong-password"
READ_LATER = 'Read it later with action "read", or the person can watch it on the Cuts page.'

# The fixture: the Mac and a library of analysed clips, as the job's tests build them.
env = test_cut_drafts.env


@pytest.fixture()
async def chat(env: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """The Mac and its clips, an editor who may draft, and a read-only account."""
    async with env["factory"]() as db:
        editor = await service.create_user(db, "editor@example.com", PASSWORD, "admin")
        reader = await service.create_user(db, "reader@example.com", PASSWORD, "readonly")
        await db.commit()
    env.update(user=editor.id, reader=reader.id)
    yield env


Sleep = Callable[[float], Awaitable[None]]


def _waiting(slept: list[float]) -> Sleep:
    """Claude's turn waits and the lane does nothing meanwhile."""

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return sleep


def _working(env: dict[str, Any], slept: list[float]) -> Sleep:
    """Claude's turn waits while the lane takes one job."""

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await jobs.run_next(env["factory"], env["site"].deps())

    return sleep


async def _tool(
    env: dict[str, Any], sleep: Sleep | None = None, user: uuid.UUID | None = None, **args: Any
) -> str:
    site: Site = env["site"]
    async with env["factory"]() as db:
        return await draft_chat.local_cut_draft(
            db,
            user or env["user"],
            args,
            sleep=sleep or _waiting([]),
            wake=site.wake,
            now=site.clock(),
            settings=site.settings,
        )


async def _refused(
    env: dict[str, Any], sleep: Sleep | None = None, user: uuid.UUID | None = None, **args: Any
) -> ValueError:
    with pytest.raises((drafts.DraftError, roughcut.CutError)) as raised:
        await _tool(env, sleep, user, **args)
    return raised.value


def _start(**more: Any) -> dict[str, Any]:
    return {"action": "start", "brief": BRIEF, "folder": "Pete Dye", **more}


async def _only_draft(env: dict[str, Any]) -> CutDraft:
    async with env["factory"]() as db:
        [draft] = list(await db.scalars(select(CutDraft)))
        return draft


async def _pieces(env: dict[str, Any], cut_id: uuid.UUID | None) -> tuple[Cut, list[CutPiece]]:
    assert cut_id is not None
    async with env["factory"]() as db:
        cut = await db.get(Cut, cut_id)
        assert cut is not None
        pieces = await db.scalars(
            select(CutPiece).where(CutPiece.cut_id == cut_id).order_by(CutPiece.position)
        )
        return cut, list(pieces)


async def _written(env: dict[str, Any]) -> tuple[int, int, int]:
    """Drafts, jobs and cuts in the database."""
    async with env["factory"]() as db:

        async def count(table: Any) -> int:
            return int(await db.scalar(select(func.count()).select_from(table)) or 0)

        return await count(CutDraft), await count(LocalAiJob), await count(Cut)


# --- a draft, asked for in the chat ------------------------------------------------


async def test_a_quick_draft_comes_back_in_the_same_answer(chat: dict[str, Any]) -> None:
    site: Site = chat["site"]
    await site.editor(chat["factory"])
    slept: list[float] = []

    said = await _tool(
        chat, _working(chat, slept), **_start(library="gelco", length_s=30, title="Pete Dye")
    )

    # One look after two seconds found it finished.
    assert slept == [draft_chat.POLL_S] and site.woken == [1]
    draft = await _only_draft(chat)
    assert (draft.state, draft.origin, draft.requested_by) == ("done", "chat", chat["user"])
    # find_shots' meaning of a folder, in the library named (any case).
    assert draft.scope["label"] == 'GELCO › folders matching "Pete Dye"'
    assert draft.scope["contains"] == "Pete Dye" and draft.target_s == 30.0
    cut, pieces = await _pieces(chat, draft.cut_id)
    lines = said.splitlines()
    assert lines[0] == (
        f'Local draft ready: "Pete Dye", 6 shots, 28.5 s. Cut id {cut.id} (version 1).'
    )
    # A line a shot: number, clip id, clip, in-out, and the Editor's reason.
    assert len(lines) == 1 + len(pieces) + 1
    for number, (line, piece) in enumerate(zip(lines[1:-1], pieces, strict=True), start=1):
        assert line.startswith(f"{number} {shotlist.short_id(piece.asset_id)} DJI_")
        assert f" {piece.in_ms / 1000:.1f}-{piece.out_ms / 1000:.1f}s - " in line
        assert line.endswith(f" - {cut_editor.DEMO_REASON}")
    assert lines[-1] == (
        f"The person can open it at /cuts/{cut.id}. To improve it: "
        f'build_rough_cut(parent="{cut.id}", …).'
    )
    assert cut.source == "local" and cut.created_by == chat["user"]


async def test_a_slow_draft_says_where_it_is_and_can_be_read_later(chat: dict[str, Any]) -> None:
    site: Site = chat["site"]
    await site.editor(chat["factory"])
    slept: list[float] = []
    held: list[bool] = []
    async with chat["factory"]() as db:

        async def sleep(seconds: float) -> None:
            # The lane writes this draft from its own session meanwhile; a
            # transaction left open for the half minute would be in its way.
            held.append(db.in_transaction())
            slept.append(seconds)

        said = await draft_chat.local_cut_draft(
            db,
            chat["user"],
            _start(),
            sleep=sleep,
            wake=site.wake,
            now=site.clock(),
            settings=site.settings,
        )

    # Half a minute of looking, then the answer, not the whole draft.
    assert slept == [draft_chat.POLL_S] * 15 and sum(slept) == draft_chat.WAIT_S
    assert not any(held)
    draft = await _only_draft(chat)
    assert said == f"The Editor has the brief (draft {draft.id}). Waiting its turn. {READ_LATER}"
    # Read while still waiting: the same answer.
    assert await _tool(chat, action="read", draft=str(draft.id)) == said

    await _drain(chat)

    # By the short id the tools print, or in full: the finished cut.
    short = await _tool(chat, action="read", draft=shotlist.short_id(draft.id))
    assert short.startswith('Local draft ready: "') and short == await _tool(
        chat, action="read", draft=str(draft.id)
    )
    assert (await _only_draft(chat)).cut_id is not None


async def test_a_draft_that_waits_for_the_night_answers_at_once(chat: dict[str, Any]) -> None:
    site: Site = chat["site"]
    await site.editor(chat["factory"], LARGE, memory_gb=10.1)  # overnight only, asked at 14:00
    slept: list[float] = []

    said = await _tool(chat, _working(chat, slept), **_start())

    # The lane took it once and it went to wait for 21:30: no more looking.
    assert slept == [draft_chat.POLL_S]
    draft = await _only_draft(chat)
    assert said.startswith(
        f"The Editor has the brief (draft {draft.id}). Waiting for the overnight window "
        "(starts 21:30)"
    )
    assert said.endswith(READ_LATER) and ".. " not in said
    assert site.server.requests == []

    site.clock.at = NIGHT
    await _drain(chat)
    assert (await _tool(chat, action="read", draft=str(draft.id))).startswith("Local draft ready")


async def test_short_clip_ids_are_accepted(chat: dict[str, Any]) -> None:
    site: Site = chat["site"]
    await site.editor(chat["factory"])
    clips = chat["clips"]
    ids = [shotlist.short_id(clips[0].id), shotlist.short_id(clips[1].id), str(clips[2].id)]

    said = await _tool(
        chat,
        _working(chat, []),
        action="start",
        brief=BRIEF,
        ids=[*ids, ids[0].upper(), "  "],
    )

    draft = await _only_draft(chat)
    assert draft.scope["kind"] == "selection"
    assert draft.scope["asset_ids"] == [str(clip.id) for clip in clips]
    assert draft.scope["label"] == "3 clips named in the chat"
    assert said.startswith("Local draft ready")

    # An id that is no clip is named, and nothing more is written.
    before = await _written(chat)
    missing = await _refused(chat, action="start", brief=BRIEF, ids=["deadbeef"])
    assert isinstance(missing, roughcut.CutError) and str(missing) == "No asset deadbeef"
    named = await _refused(chat, action="start", brief=BRIEF, ids=["DJI_0631"])
    assert str(named) == "'DJI_0631' is not an asset id"
    assert str(await _refused(chat, action="start", brief=BRIEF, ids="DJI_0631")) == (
        draft_chat.NOT_A_LIST
    )
    assert await _written(chat) == before


# --- what Claude is told when it cannot be done ---------------------------------------


async def test_a_refused_draft_is_a_tool_problem_with_the_sentence(
    chat: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through C1's dispatcher, as the panel runs it: the caller bound, the
    settings this install's own, and the connector's package not installed."""
    site: Site = chat["site"]
    await site.editor(chat["factory"])
    for name, value in (
        ("FRAMEFOUND_DATA_DIR", site.settings.data_dir),
        ("FRAMEFOUND_MODELS_DIR", site.settings.models_dir),
        ("FRAMEFOUND_RUNTIMES_DIR", site.settings.runtimes_dir),
        ("FRAMEFOUND_SECRET_KEY", SECRET),
        ("FRAMEFOUND_MIN_FREE_GB", "0"),
    ):
        monkeypatch.setenv(name, str(value))
    get_settings.cache_clear()
    monkeypatch.setattr(tools, "CHAT_ONLY", {})
    assert draft_chat.register()
    monkeypatch.setitem(sys.modules, "mcp", None)  # `import mcp` now fails

    async def sessions() -> AsyncIterator[Any]:
        async with chat["factory"]() as db:
            yield db

    caller = tools.principal_for(
        user_id=chat["user"], email="editor@example.com", role="admin", base_url="http://t"
    )

    async def ran(**args: Any) -> tuple[bool, str]:
        with auth.bound(caller, sessions):
            outcome = await tools.run(draft_chat.NAME, args, pictures=True)
        [block] = outcome.blocks
        return outcome.is_error, block["text"]

    bare = shotlist.short_id(chat["bare"].id)
    assert await ran(action="start", brief=BRIEF, ids=[bare]) == (
        True,
        drafts.NO_SHOTS_SELECTION,
    )
    assert await ran(action="start", brief=BRIEF, folder="Rev 2") == (True, drafts.NO_SHOTS_FOLDER)
    # A folder name that matches nothing is not footage waiting for analysis.
    assert await ran(action="start", brief=BRIEF, folder="Pete Dyee") == (
        True,
        'No video on the share is in a folder matching "Pete Dyee".',
    )
    # More clips than one draft takes: the limit, not "the tool failed".
    many = [str(uuid.uuid4()) for _ in range(drafts.MOST_CLIPS + 1)]
    assert await ran(action="start", brief=BRIEF, ids=many) == (True, draft_chat.TOO_MANY_IDS)
    assert await ran(action="start", brief=BRIEF, folder="Pete", library="Nowhere") == (
        True,
        "No library called 'Nowhere'. Libraries: GELCO.",
    )
    assert await ran(action="start", brief=BRIEF) == (True, draft_chat.NO_FOOTAGE)
    assert await ran(action="start", brief=BRIEF, folder="Pete", ids=[bare]) == (
        True,
        draft_chat.BOTH_FOOTAGE,
    )
    assert await ran(action="start", folder="Pete") == (True, draft_chat.NO_BRIEF)
    assert await ran(action="start", brief=BRIEF, folder="Pete", length_s=2) == (
        True,
        "length_s: Input should be greater than or equal to 5.",
    )
    assert await ran(action="draw") == (True, draft_chat.NO_ACTION)
    assert await ran(action="read") == (True, draft_chat.NO_DRAFT_ID)
    assert await ran(action="read", draft=str(uuid.uuid4())) == (True, draft_chat.NO_SUCH_DRAFT)
    assert await _written(chat) == (0, 0, 0)

    # A draft that ended without a cut is read as an error, with why.
    _answer([{"shot": 91, "in_s": 1.0, "out_s": 4.0, "reason": "Invented."}])
    failed = await _refused(chat, _working(chat, []), **_start())
    expected = f"{drafts.NOTHING_USABLE} Cut 1 named shot 91, which is not on the list."
    assert str(failed) == expected
    draft = await _only_draft(chat)
    assert await ran(action="read", draft=str(draft.id)) == (True, expected)


async def test_more_clips_than_a_draft_takes_are_refused_before_any_is_looked_up(
    chat: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await chat["site"].editor(chat["factory"])
    looked: list[str] = []
    real = roughcut.find_asset

    async def counted(db: Any, ref: str) -> Any:
        looked.append(ref)
        return await real(db, ref)

    monkeypatch.setattr(roughcut, "find_asset", counted)
    many = [str(uuid.uuid4()) for _ in range(drafts.MOST_CLIPS + 1)]

    refused = await _refused(chat, action="start", brief=BRIEF, ids=many)
    assert (str(refused), looked) == (draft_chat.TOO_MANY_IDS, [])
    # Exactly the limit is accepted, and its first unknown id named.
    refused = await _refused(chat, action="start", brief=BRIEF, ids=many[: drafts.MOST_CLIPS])
    assert (str(refused), looked) == (f"No asset {many[0]}", [many[0]])
    # One clip named many times is one clip.
    looked.clear()
    clip = str(chat["clips"][0].id)
    said = await _tool(chat, action="start", brief=BRIEF, ids=[clip] * (drafts.MOST_CLIPS + 1))
    assert looked == [clip] and said.startswith("The Editor has the brief")
    assert (await _only_draft(chat)).scope["label"] == "1 clip named in the chat"


async def test_a_read_only_account_reads_drafts_but_cannot_ask_for_one(
    chat: dict[str, Any],
) -> None:
    site: Site = chat["site"]
    await site.editor(chat["factory"])
    refused = await _refused(chat, user=chat["reader"], **_start())
    assert (str(refused), getattr(refused, "status", None)) == (draft_chat.READ_ONLY, 403)
    assert await _written(chat) == (0, 0, 0)

    await _tool(chat, **_start())
    draft = await _only_draft(chat)
    read = await _tool(chat, user=chat["reader"], action="read", draft=str(draft.id))
    assert read.startswith(f"The Editor has the brief (draft {draft.id}).")


async def test_a_cut_deleted_since_is_said_and_not_a_crash(chat: dict[str, Any]) -> None:
    site: Site = chat["site"]
    await site.editor(chat["factory"])
    await _tool(chat, _working(chat, []), **_start())
    draft = await _only_draft(chat)
    async with chat["factory"]() as db:
        stored = await db.get(CutDraft, draft.id)
        assert stored is not None
        stored.cut_id = None  # what DELETE /cuts/{id} leaves behind
        await db.commit()
    assert str(await _refused(chat, action="read", draft=str(draft.id))) == draft_chat.CUT_GONE


# --- where the tool is offered, and what it is ------------------------------------------


def test_the_tool_definition_is_valid_for_claude() -> None:
    tool = draft_chat.TOOL
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", tool["name"])
    schema = tool["input_schema"]
    assert schema["type"] == "object" and schema["required"] == ["action"]
    assert set(schema["required"]) <= set(schema["properties"])
    assert schema["properties"]["action"]["enum"] == ["start", "read"]
    assert all(field.get("type") for field in schema["properties"].values())
    assert json.loads(json.dumps(tool)) == tool, "sent to the API as JSON, byte for byte"
    # What tells Claude when to use it, and how to build on what it made.
    assert "local Editor" in tool["description"] and "parent" in tool["description"]
    assert tool["description"].isascii()


def test_the_tool_is_not_offered_where_models_cannot_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tools, "CHAT_ONLY", {})
    nowhere = Settings(secret_key=SECRET)
    assert not draft_chat.offered(nowhere)
    assert draft_chat.register(nowhere) is False and tools.CHAT_ONLY == {}
    # The demo shows it (and its drafts say they are demonstrations)...
    assert draft_chat.offered(Settings(secret_key=SECRET, local_ai_demo=True))
    # ...but never on somebody's real, public install.
    assert not draft_chat.offered(
        Settings(secret_key=SECRET, local_ai_demo=True, domain="media.example.com")
    )
    assert draft_chat.register(Settings(secret_key=SECRET, runtimes_dir=tmp_path)) is True
    own = tools.CHAT_ONLY[draft_chat.NAME]
    assert (own.confirm, tools.needs_confirm(draft_chat.NAME)) == (False, False)
    assert own.run is draft_chat.run


async def test_the_connector_still_has_exactly_its_tools_and_the_panel_has_one_more(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("mcp")
    from framefound.mcp.server import build_server

    connector = [tool.name for tool in await build_server().list_tools()]
    assert len(connector) == 21

    monkeypatch.setattr(tools, "CHAT_ONLY", {})
    draft_chat.register(Settings(secret_key=SECRET))
    assert [spec["name"] for spec in await tools.specs()] == connector

    draft_chat.register(Settings(secret_key=SECRET, runtimes_dir=tmp_path))
    listed = await tools.specs()
    assert [spec["name"] for spec in listed] == [*connector, draft_chat.NAME]
    assert listed[-1] == draft_chat.TOOL
    assert [tool.name for tool in await build_server().list_tools()] == connector, (
        "the external connector does not gain the panel's tool"
    )


def test_the_api_registers_the_tool_and_neither_needs_the_connector() -> None:
    """`api/v1/cuts.py` imports the module (that is what registers the tool);
    the module reads the caller from `framefound.mcp.auth` only inside `run`,
    and never imports the optional `mcp` package or `anthropic` (P1)."""
    cuts_tree = ast.parse(Path(api.__file__).read_text(encoding="utf-8"))
    assert "framefound.nle.draft_chat" in {
        alias.name
        for node in cuts_tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    tree = ast.parse(Path(draft_chat.__file__).read_text(encoding="utf-8"))

    def imports(node: ast.AST) -> list[str]:
        found: list[str] = []
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom):
                found.append(inner.module or "")
            elif isinstance(inner, ast.Import):
                found += [alias.name for alias in inner.names]
        return [
            name
            for name in found
            if name.split(".")[0] in ("mcp", "anthropic") or name.startswith("framefound.mcp")
        ]

    assert imports(tree) == ["framefound.mcp.auth"]
    [run] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
    ]
    assert imports(run) == ["framefound.mcp.auth"], "read only when the panel calls the tool"
