"""Asking the local Editor for a rough cut (framefound/nle/drafts.py; design L5 S5).

The job runs on the real lane: `jobs.run_next` is awaited directly (no queue,
no worker), the real `ask()` stands in front of `fake.FakeModelServer` (as a
real Mac's server: its answers are not marked made up), which answers the
Editor with `cut_editor.demo_answer` (registered by drafts.py at import)
unless a test says otherwise, and the Editor is one of L1's fixture
models shrunk to a few kilobytes in a library under `tmp_path`. Waking the
lane is a list's `append` (False for "the queue is down"). The footage is
rows: clips and their shot windows. Nothing starts a process or reaches a
network — trying to fails the test.
"""

import ast
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import cut_editor
from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.models import (
    Asset,
    Cut,
    CutDraft,
    CutPiece,
    Library,
    LocalAiJob,
    LocalModel,
    ShotWindow,
)
from framefound.local_models import UNAVAILABLE, fake, jobs, library, preflight, registry, runtimes
from framefound.local_models.ask import NotNow
from framefound.local_models.deps import Deps
from framefound.local_models.kinds import MlxVlm
from framefound.local_models.settings_store import LocalModelsConfig, load_config, save_config
from framefound.nle import drafts, roughcut
from framefound.nle.drafts import DraftError, DraftRequest, Scope
from framefound.storage import stores

Factory = async_sessionmaker[AsyncSession]

SECRET = "cut-drafts-secret"
EDITOR = "mlx-community/gemma-4-e4b-it-4bit"
# The plan's Editor: about 10 GB, so it only runs overnight under the defaults.
LARGE = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
VERSION = "0.7.12"
AFTERNOON = datetime(2026, 11, 14, 14, 0)
NIGHT = datetime(2026, 11, 14, 22, 0)
BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
FOLDER = "2026/Pete Dye"
UNANALYSED = "2026/Rev 2"
# Three shots a clip: 6 s, 8 s and 6 s.
WINDOWS = ((0, 6000), (6000, 14000), (14000, 20000))
BRIEF = "Thirty seconds of smooth aerials at golden hour, opening on the clubhouse."

_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_MODELS_DIR",
    "FRAMEFOUND_DATA_DIR",
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
)


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may start a program or reach a real address; trying to is a failure."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a test tried to start a process or reach a real address")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    for name in _OURS:
        monkeypatch.delenv(name, raising=False)
    # Progress written, and Cancel looked for, at every step.
    monkeypatch.setattr(jobs, "PROGRESS_EVERY_S", 0.0)
    get_settings.cache_clear()
    stores._CACHE.clear()
    answers = dict(fake.FakeModelServer._answers)
    yield
    # `answer_for` is class-wide: what a test registered must not outlive it.
    fake.FakeModelServer._answers.clear()
    fake.FakeModelServer._answers.update(answers)
    get_settings.cache_clear()
    stores._CACHE.clear()


class Clock:
    """The Mac's wall clock, naive, moved by the test."""

    def __init__(self, at: datetime = AFTERNOON) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class MacServer(fake.FakeModelServer):
    """The model server of a Mac that runs models: what it answers a model
    thought (`demo` false), where the plain fake marks every answer made up.
    Set `demo` to answer as the demo install's server does: made up, by the
    demo's runtime."""

    demo = False

    def chat(self, body: dict[str, Any], *, idle_s: float, timeout_s: float) -> dict[str, Any]:
        answer = super().chat(body, idle_s=idle_s, timeout_s=timeout_s)
        facts = answer["framefound"]
        facts["demo"] = self.demo
        if self.demo:
            facts["runtime"] = "demo"
        return answer


class Site:
    """A Mac with MLX-VLM installed, a model library and a data folder."""

    def __init__(self, tmp_path: Path) -> None:
        self.data = tmp_path / "data"
        self.models = tmp_path / "models"
        self.runtimes = tmp_path / "runtimes"
        for folder in (self.data, self.models, self.runtimes):
            folder.mkdir()
        self.hub = fake.FakeHub.from_fixtures(sleep=lambda _seconds: None)
        self.hub.shrink(EDITOR)
        self.hub.shrink(LARGE)
        self.settings = Settings(
            data_dir=self.data,
            models_dir=self.models,
            runtimes_dir=self.runtimes,
            secret_key=SECRET,
            min_free_gb=0,
        )
        self.clock = Clock()
        self.server = MacServer(sleep=lambda _seconds: None)
        runner = fake.FakeRunner(
            {
                "uv venv": (0, ""),
                "uv pip install": (0, ""),
                "uv pip freeze": (0, ""),
                "python": lambda argv: (0, f"{Path(argv[0]).parents[2].name}\n"),
            },
            make_venvs=True,
        )
        runtimes.install(MlxVlm(), VERSION, self.runtimes, run=runner)
        runtimes.activate(self.runtimes, "mlx-vlm", VERSION)
        # Every wake-up of the lane; `queue_up = False` is "the queue is down".
        self.woken: list[int] = []
        self.queue_up = True

    def wake(self) -> bool:
        self.woken.append(1)
        return self.queue_up

    def deps(self) -> Deps:
        return Deps(
            settings=self.settings,
            hub_for=self.hub.with_token,
            fetch_json=lambda _url, **_more: {},
            server=self.server,
            kinds={"mlx-vlm": MlxVlm()},
            sleep=lambda _seconds: None,
            now=self.clock,
            system="Darwin",
            machine="arm64",
        )

    async def editor(
        self,
        factory: Factory,
        repo: str = EDITOR,
        *,
        memory_gb: float | None = None,
        variant: str | None = None,
        pending: bool = False,
    ) -> uuid.UUID:
        """A model checked and added as the page would, its files in the
        library, given the Editor role (`pending`: lined up for it while its
        copy is still being made)."""
        found = preflight.preflight(
            self.hub,
            repo,
            config=LocalModelsConfig(),
            kinds={"mlx-vlm": MlxVlm()},
            installed={"mlx-vlm": VERSION},
            free_bytes=None,
            min_free_bytes=0,
        )
        async with factory() as db:
            model = await registry.add_model(db, found, None)
            model.state = registry.INSTALLED
            if memory_gb is not None:
                # What the real build needs; the shrunk fixture's files say less.
                model.memory_gb = memory_gb
            if variant is not None:
                model.variant = variant
            await registry.set_role(db, "editor", model, pending=pending, user_id=None)
            await db.commit()
            files = registry.snapshot(model)
        library.download(self.hub, files, settings=self.settings)
        return model.id


def _clip(library_id: uuid.UUID, relative: str, hour: int) -> Asset:
    return Asset(
        library_id=library_id,
        relative_path=relative,
        filename=relative.rsplit("/", 1)[-1],
        extension="mp4",
        media_type="video",
        size_bytes=1,
        mtime=BASE,
        captured_at=BASE + timedelta(hours=hour),
        duration_s=20.0,
        fps=29.97,
        width=3840,
        height=2160,
        availability="online",
    )


@pytest.fixture()
async def env(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """A Mac, and a library with a folder of three analysed drone clips and a
    folder whose one clip was never analysed into shots."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'drafts.db').as_posix()}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    site = Site(tmp_path)
    async with factory() as db:
        lib = Library(name="GELCO", root_path=str(tmp_path / "GELCO"))
        db.add(lib)
        await db.flush()
        clips = [_clip(lib.id, f"{FOLDER}/DJI_20261103051509_063{n}_D.MP4", n) for n in range(1, 4)]
        bare = _clip(lib.id, f"{UNANALYSED}/DJI_20261103051509_0700_D.MP4", 5)
        db.add_all([*clips, bare])
        await db.flush()
        for clip in clips:
            for idx, (start, end) in enumerate(WINDOWS):
                db.add(
                    ShotWindow(
                        asset_id=clip.id,
                        idx=idx,
                        start_ms=start,
                        end_ms=end,
                        move="orbit",
                        direction="right",
                        smooth=0.9,
                        energy="low",
                        light="golden",
                        subjects={"green": 0.3, "clubhouse": 0.2},
                    )
                )
        await db.commit()
    yield {"factory": factory, "site": site, "library": lib, "clips": clips, "bare": bare}
    await engine.dispose()


def _request(env: dict[str, Any], **more: Any) -> DraftRequest:
    values: dict[str, Any] = {
        "brief": BRIEF,
        "scope": Scope(kind="folder", library_id=env["library"].id, folder=FOLDER),
    }
    values.update(more)
    return DraftRequest(**values)


async def _ask_for(env: dict[str, Any], **more: Any) -> CutDraft:
    site: Site = env["site"]
    async with env["factory"]() as db:
        return await drafts.request_draft(
            db,
            _request(env, **more),
            user_id=None,
            wake=site.wake,
            now=site.clock(),
            settings=site.settings,
        )


async def _refused(env: dict[str, Any], **more: Any) -> DraftError:
    with pytest.raises(DraftError) as raised:
        await _ask_for(env, **more)
    return raised.value


async def _drain(env: dict[str, Any], limit: int = 10) -> None:
    """What the lane does when woken: run jobs until none is queued."""
    for _ in range(limit):
        if not await jobs.run_next(env["factory"], env["site"].deps()):
            return
    raise AssertionError("the lane never ran out of work")


async def _read(env: dict[str, Any], draft_id: uuid.UUID) -> tuple[CutDraft, LocalAiJob | None]:
    async with env["factory"]() as db:
        draft = await db.get(CutDraft, draft_id, populate_existing=True)
        assert draft is not None
        return draft, await drafts.job_of(db, draft)


async def _shown(env: dict[str, Any], draft_id: uuid.UUID) -> tuple[str, str, bool]:
    draft, job = await _read(env, draft_id)
    return drafts.shown(draft, job, env["site"].clock())


async def _count(env: dict[str, Any], model: type[Any]) -> int:
    async with env["factory"]() as db:
        return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def _cut(env: dict[str, Any], cut_id: uuid.UUID | None) -> tuple[Cut, list[CutPiece]]:
    assert cut_id is not None
    async with env["factory"]() as db:
        cut = await db.get(Cut, cut_id)
        assert cut is not None
        pieces = list(
            await db.scalars(
                select(CutPiece).where(CutPiece.cut_id == cut_id).order_by(CutPiece.position)
            )
        )
        return cut, pieces


def _answer(cuts: list[dict[str, Any]], title: str = "") -> None:
    """What the Editor writes from now on, for every question."""
    fake.FakeModelServer.answer_for(
        "EditorAnswer",
        lambda _body: cut_editor.EditorAnswer.model_validate(
            {"title": title, "cuts": cuts}
        ).model_dump_json(),
    )


# --- a draft, start to finish -------------------------------------------------------


async def test_a_draft_becomes_a_cut_with_its_reasons_and_its_provenance(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    # A named build, so the cut is seen to say which one drafted it.
    model_id = await site.editor(env["factory"], variant="4bit")

    draft = await _ask_for(env)
    assert site.woken == [1]
    assert await _shown(env, draft.id) == ("queued", drafts.QUEUED_SAYS, False)
    # Nine shots in three clips, all offered, kept by clip and span.
    assert (draft.shots_in_scope, draft.clips, len(draft.candidates)) == (9, 3, 9)
    assert draft.scope["label"] == f"GELCO › {FOLDER}"
    assert draft.title == drafts.short_title(BRIEF) and draft.target_s == 30.0

    await _drain(env)

    draft, job = await _read(env, draft.id)
    assert (draft.state, draft.message, draft.problems) == ("done", "", [])
    assert job is not None and job.state == "done" and job.kind == drafts.KIND
    async with env["factory"]() as db:
        model = await db.get(LocalModel, model_id)
        assert model is not None
    cut, pieces = await _cut(env, draft.cut_id)
    # A local draft, recorded as any cut is, by the model that made it.
    assert (cut.source, cut.version, cut.root_id, cut.sent_at) == ("local", 1, None, None)
    provenance = (
        cut.model,
        cut.model_revision,
        cut.variant,
        cut.runtime,
        cut.runtime_version,
        cut.prompt_version,
    )
    assert provenance == (
        EDITOR,
        model.revision,
        "4bit",
        "mlx-vlm",
        VERSION,
        cut_editor.PROMPT_VERSION,
    )
    # A model on a real install judged it: not a demonstration.
    assert (draft.model, draft.runtime_version, draft.demo) == (EDITOR, VERSION, False)
    assert not drafts.demonstration(cut, draft)
    # Either is enough: the demo's runtime, or a server that said it made the answer up.
    assert drafts.demonstration(Cut(runtime="demo"), None)
    assert drafts.demonstration(cut, CutDraft(demo=True))
    # The Editor's reason for each shot is kept, and each piece lies inside
    # the shot it was offered as.
    assert len(pieces) == 6 and {p.note for p in pieces} == {cut_editor.DEMO_REASON}
    offered = [cut_editor.Candidate.from_json(row) for row in draft.candidates]
    for piece in pieces:
        assert any(
            c.asset_id == piece.asset_id
            and c.start_s * 1000 <= piece.in_ms < piece.out_ms <= c.end_s * 1000
            for c in offered
        )
    assert cut.duration_s == pytest.approx(28.5)
    # What the Editor was asked: one question, the list, the form, the room.
    [asked] = site.server.requests
    assert asked["response_format"]["json_schema"]["name"] == "EditorAnswer"
    text = asked["messages"][0]["content"][0]["text"]
    assert text.startswith(cut_editor.INSTRUCTIONS) and f"Brief: {BRIEF}" in text
    assert asked["max_tokens"] == cut_editor.max_tokens(cut_editor.Constraints(30.0))
    assert job.result == {"cut_id": str(cut.id), "cuts": 6, "seconds": 28.5}


async def test_the_draft_is_written_by_the_function_the_connector_uses(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await env["site"].editor(env["factory"])
    calls: list[dict[str, Any]] = []
    real = roughcut.build_rough_cut

    async def recorder(db: AsyncSession, title: str, cuts: Any, **kwargs: Any) -> Any:
        calls.append({"title": title, "cuts": cuts, **kwargs})
        return await real(db, title, cuts, **kwargs)

    monkeypatch.setattr(roughcut, "build_rough_cut", recorder)
    draft = await _ask_for(env, title="Pete Dye teaser")
    await _drain(env)

    [call] = calls
    assert call["title"] == "Pete Dye teaser"
    assert (call["source"], call["record"], call["must_record"], call["by"]) == (
        "local",
        True,
        True,
        "local editor",
    )
    assert call["profile"] is None and call["parent"] is None
    # The timeline runs at the rate the footage was shot at.
    assert call["sequence_fps"] == pytest.approx(29.97)
    assert set(call["provenance"]) == {
        "model",
        "model_revision",
        "variant",
        "runtime",
        "runtime_version",
        "prompt_version",
    }
    assert all(isinstance(c, roughcut.CutIn) and c.note for c in call["cuts"])
    draft, _job = await _read(env, draft.id)
    assert draft.state == "done"


async def test_the_editors_own_title_is_used_only_when_none_was_given(
    env: dict[str, Any],
) -> None:
    await env["site"].editor(env["factory"])
    _answer([{"shot": 2, "in_s": 6.5, "out_s": 11.0, "reason": "Opens wide."}], "Course at dusk")

    untitled = await _ask_for(env)
    titled = await _ask_for(env, title="Pete Dye teaser")
    await _drain(env)

    first, _ = await _read(env, untitled.id)
    second, _ = await _read(env, titled.id)
    assert (await _cut(env, first.cut_id))[0].title == "Course at dusk"
    assert (await _cut(env, second.cut_id))[0].title == "Pete Dye teaser"


# --- refused before anything is queued ---------------------------------------------


async def test_a_folder_with_no_analysed_shots_is_refused_before_anything_is_queued(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])

    found = await _refused(env, scope=Scope(kind="folder", folder=UNANALYSED))
    assert (str(found), found.status) == (drafts.NO_SHOTS_FOLDER, 400)
    # "2026/Pete" is not "2026/Pete Dye": a folder never takes its sibling,
    # and one that holds no video is not said to be waiting for analysis.
    found = await _refused(env, scope=Scope(kind="folder", folder="2026/Pete"))
    assert str(found) == drafts.NO_VIDEO_FOLDER
    found = await _refused(env, scope=Scope(kind="selection", asset_ids=[env["bare"].id]))
    assert (str(found), found.status) == (drafts.NO_SHOTS_SELECTION, 400)
    # A scope that names nothing, and lengths that cannot both hold.
    found = await _refused(env, scope=Scope(kind="selection"))
    assert str(found) == drafts.NO_SCOPE
    found = await _refused(env, scope=Scope(kind="folder", library_id=env["library"].id))
    assert str(found) == drafts.NO_SCOPE
    found = await _refused(env, min_shot_s=9, max_shot_s=4)
    assert (str(found), found.status) == (drafts.SHORTEST_LONGER, 400)
    found = await _refused(env, scope=Scope(kind="folder", library_id=uuid.uuid4(), folder=FOLDER))
    assert (str(found), found.status) == (drafts.NO_LIBRARY, 404)
    found = await _refused(env, revises=uuid.uuid4())
    assert (str(found), found.status) == (drafts.NO_CUT, 404)
    # Shots longer than any there is: said as that, not as "never analysed".
    found = await _refused(env, min_shot_s=10, max_shot_s=20)
    assert str(found).startswith("No analysed shot here is at least 10 s long.")

    assert (await _count(env, CutDraft), await _count(env, LocalAiJob)) == (0, 0)
    assert site.woken == []


async def test_footage_that_is_not_there_is_not_said_to_be_waiting_for_analysis(
    env: dict[str, Any],
) -> None:
    """ "Not analysed yet — Processing shows how far along" sends the person to
    wait for footage that does not exist; said only of clips that do."""
    await env["site"].editor(env["factory"])

    # The chat's misheard folder name: nothing matches at all.
    found = await _refused(env, scope=Scope(kind="folder", contains="Pete Dyee"))
    assert str(found) == 'No video on the share is in a folder matching "Pete Dyee".'
    assert str(found) == drafts.NO_VIDEO_MATCHING.format(contains="Pete Dyee")
    # A folder whose every clip has gone from the share since the picker offered it.
    async with env["factory"]() as db:
        await db.execute(
            update(Asset)
            .where(Asset.id.in_([clip.id for clip in env["clips"]]))
            .values(availability="offline")
        )
        await db.commit()
    found = await _refused(
        env, scope=Scope(kind="folder", library_id=env["library"].id, folder=FOLDER)
    )
    assert (str(found), found.status) == (drafts.NO_VIDEO_FOLDER, 400)
    found = await _refused(env, scope=Scope(kind="selection", asset_ids=[env["clips"][0].id]))
    assert str(found) == drafts.NO_VIDEO_SELECTION
    # Clips that are there but were never analysed keep their own sentence.
    found = await _refused(env, scope=Scope(kind="folder", contains="Rev 2"))
    assert str(found) == drafts.NO_SHOTS_FOLDER
    assert (await _count(env, CutDraft), await _count(env, LocalAiJob)) == (0, 0)


async def test_footage_holding_more_shots_than_one_search_reads_is_refused_from_the_chat_too(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shot search reads at most SCAN_LIMIT windows, in no particular
    order, so a draft of more would be made from an arbitrary part of the
    footage. The page's endpoint refused it, but the chat panel's tool asks
    `request_draft` directly: it is refused there, in the page's own words."""
    from framefound.api.v1 import cuts as cuts_api
    from framefound.auth import service
    from framefound.nle import draft_chat, shotlist

    site: Site = env["site"]
    await site.editor(env["factory"])
    async with env["factory"]() as db:
        person = await service.create_user(db, "editor@example.com", "a-strong-password", "admin")
        await db.commit()
    # The folder holds three clips of three shots each: nine.
    monkeypatch.setattr(shotlist, "SCAN_LIMIT", 8)

    async def from_the_chat(**args: Any) -> DraftError:
        async with env["factory"]() as db:
            with pytest.raises(DraftError) as raised:
                await draft_chat.local_cut_draft(
                    db,
                    person.id,
                    {"action": "start", "brief": BRIEF, **args},
                    wake=site.wake,
                    now=site.clock(),
                    settings=site.settings,
                )
        return raised.value

    found = await from_the_chat(folder="Pete Dye")
    assert (str(found), found.status) == (
        'The folders matching "Pete Dye" hold more shots than one draft can read (8). '
        "Name a narrower folder.",
        400,
    )
    found = await from_the_chat(ids=[str(clip.id) for clip in env["clips"]])
    assert str(found) == drafts.TOO_MANY_SHOTS_SELECTION.format(most=8)
    # And through the page's path, whatever its endpoint asks first.
    found = await _refused(env)
    assert (str(found), found.status) == (drafts.TOO_MANY_SHOTS_FOLDER.format(most=8), 400)
    assert (await _count(env, CutDraft), await _count(env, LocalAiJob), site.woken) == (0, 0, [])
    # One sentence for the page and the chat.
    assert (
        drafts.TOO_MANY_SHOTS_FOLDER,
        drafts.TOO_MANY_SHOTS_MATCHING,
        drafts.TOO_MANY_SHOTS_SELECTION,
    ) == (
        cuts_api.TOO_MANY_SHOTS_FOLDER,
        cuts_api.TOO_MANY_SHOTS_MATCHING,
        cuts_api.TOO_MANY_SHOTS_SELECTION,
    )

    # Exactly the bound still fits.
    monkeypatch.setattr(shotlist, "SCAN_LIMIT", 9)
    draft = await _ask_for(env)
    assert (draft.state, draft.shots_in_scope) == ("queued", 9)


async def test_shots_already_used_are_left_out_when_asked_and_all_used_is_said(
    env: dict[str, Any],
) -> None:
    await env["site"].editor(env["factory"])
    first = await _ask_for(env)
    await _drain(env)
    first, _ = await _read(env, first.id)
    cut, pieces = await _cut(env, first.cut_id)
    # Downloaded for Premiere: from now on its shots count as used (D4).
    async with env["factory"]() as db:
        await db.execute(update(Cut).where(Cut.id == cut.id).values(sent_at=datetime.now(UTC)))
        await db.commit()

    def used(candidate: cut_editor.Candidate) -> bool:
        return any(
            p.asset_id == candidate.asset_id
            and min(p.out_ms / 1000, candidate.end_s) - max(p.in_ms / 1000, candidate.start_s)
            > 0.05
            for p in pieces
        )

    offered = [cut_editor.Candidate.from_json(row) for row in first.candidates]
    fresh = [c for c in offered if not used(c)]
    assert 0 < len(fresh) < len(offered)

    unused = await _ask_for(env, title="Fresh shots", exclude_used=True)
    again = await _ask_for(env, title="Any shots")
    left = [cut_editor.Candidate.from_json(row) for row in unused.candidates]
    assert {(c.asset_id, c.start_s) for c in left} == {(c.asset_id, c.start_s) for c in fresh}
    assert unused.exclude_used is True and unused.shots_in_scope == len(fresh)
    # Without the tick, the used shots are offered as before.
    assert len(again.candidates) == len(offered)

    # Claude's finished cut takes every shot there is: nothing is left to offer.
    async with env["factory"]() as db:
        claude = Cut(title="Everything", source="mcp", library_id=env["library"].id)
        db.add(claude)
        await db.flush()
        spans = [(clip, start, end) for clip in env["clips"] for start, end in WINDOWS]
        for position, (clip, start, end) in enumerate(spans):
            db.add(
                CutPiece(
                    cut_id=claude.id,
                    position=position,
                    asset_id=clip.id,
                    filename=clip.filename,
                    in_ms=start,
                    out_ms=end,
                )
            )
        await db.commit()
    found = await _refused(env, title="Nothing left", exclude_used=True)
    assert (str(found), found.status) == (drafts.ALL_USED, 400)
    assert await _count(env, CutDraft) == 3


async def test_an_editor_still_being_copied_is_said_and_nothing_is_written(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"], pending=True)

    async with env["factory"]() as db:
        status = await drafts.editor_status(db, settings=site.settings)
    assert (status.model_id, status.why_not) == (None, drafts.EDITOR_COPYING)
    found = await _refused(env)
    # Not "give a model the role": that has been done.
    assert (str(found), found.status) == (drafts.EDITOR_COPYING, 409)
    assert (await _count(env, CutDraft), await _count(env, LocalAiJob), site.woken) == (0, 0, [])


async def test_no_editor_no_draft_and_the_sentence_says_where_to_go(env: dict[str, Any]) -> None:
    async with env["factory"]() as db:
        status = await drafts.editor_status(db, settings=env["site"].settings)
    assert (status.mode, status.model_id, status.runs, status.why_not) == (
        "ready",
        None,
        None,
        drafts.NO_EDITOR,
    )
    found = await _refused(env)
    assert (str(found), found.status) == (drafts.NO_EDITOR, 409)
    assert "AI models page" in str(found)
    assert (await _count(env, CutDraft), await _count(env, LocalAiJob)) == (0, 0)


async def test_where_models_cannot_run_a_draft_is_refused_and_nothing_is_written(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    # The containers: no runtimes folder, no demo.
    site.settings = Settings(data_dir=site.data, models_dir=site.models, secret_key=SECRET)

    async with env["factory"]() as db:
        status = await drafts.editor_status(db, settings=site.settings)
    assert (status.mode, status.note, status.why_not) == ("unavailable", UNAVAILABLE, UNAVAILABLE)
    found = await _refused(env)
    assert (str(found), found.status) == (UNAVAILABLE, 409)
    assert (await _count(env, CutDraft), await _count(env, LocalAiJob)) == (0, 0)
    assert site.woken == []


async def test_an_editor_that_cannot_hold_the_role_is_named_in_the_refusal(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    model_id = await site.editor(env["factory"], memory_gb=64.0)

    async with env["factory"]() as db:
        status = await drafts.editor_status(db, now=AFTERNOON, settings=site.settings)
    assert (status.model_id, status.runs, status.starts_at) == (model_id, "never", None)
    found = await _refused(env)
    assert found.status == 409
    assert str(found).startswith("The Editor (gemma-4-e4b-it-4bit) cannot draft right now. It ")
    assert "more than the overnight budget" in str(found)


async def test_the_same_draft_is_not_asked_for_twice_while_the_first_waits(
    env: dict[str, Any],
) -> None:
    await env["site"].editor(env["factory"])
    await _ask_for(env)
    found = await _refused(env)
    assert (str(found), found.status) == (drafts.ALREADY_WAITING, 409)
    # Other words, or other footage, are another draft.
    await _ask_for(env, title="Another take")
    assert await _count(env, CutDraft) == 2


async def test_the_same_brief_for_other_ticked_clips_is_another_draft(
    env: dict[str, Any],
) -> None:
    """ "2 clips ticked in Search" is said of any two clips: which ones is what
    makes a draft the same."""
    await env["site"].editor(env["factory"])
    a, b, c = (clip.id for clip in env["clips"])

    first = await _ask_for(env, scope=Scope(kind="selection", asset_ids=[a, b]))
    second = await _ask_for(env, scope=Scope(kind="selection", asset_ids=[b, c]))
    assert first.scope["label"] == second.scope["label"] == "2 clips ticked in Search"
    # The same two clips, ticked in another order, are the same draft.
    found = await _refused(env, scope=Scope(kind="selection", asset_ids=[b, a]))
    assert (str(found), found.status) == (drafts.ALREADY_WAITING, 409)
    async with env["factory"]() as db:
        subjects = {job.subject for job in await db.scalars(select(LocalAiJob))}
    # Both are listed on the AI models page under the same words.
    assert len(subjects) == 1 and await _count(env, CutDraft) == 2

    # Once the first has finished, the same request is a new draft again.
    await _drain(env)
    await _ask_for(env, scope=Scope(kind="selection", asset_ids=[a, b]))


async def test_a_queue_that_is_down_leaves_no_draft_behind(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    site.queue_up = False

    found = await _refused(env)

    assert (str(found), found.status) == (drafts.QUEUE_DOWN, 503)
    assert site.woken == [1]
    assert (await _count(env, CutDraft), await _count(env, LocalAiJob)) == (0, 0)
    # And the next try, with the queue back, is not "already waiting".
    site.queue_up = True
    await _ask_for(env)


# --- waiting, failing and cancelling ------------------------------------------------


async def test_a_draft_asked_for_by_day_waits_for_the_overnight_window(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"], LARGE, memory_gb=10.1)
    async with env["factory"]() as db:
        status = await drafts.editor_status(db, now=AFTERNOON, settings=site.settings)
    # Said before the button is pressed, and not a refusal.
    assert (status.runs, status.starts_at, status.why_not) == (
        "overnight",
        datetime(2026, 11, 14, 21, 30),
        None,
    )
    assert status.model_name == "gemma-4-12B-it-OptiQ-4bit"

    draft = await _ask_for(env)
    await _drain(env)

    state, message, stopping = await _shown(env, draft.id)
    assert (state, stopping) == ("waiting", False)
    assert message.startswith("Waiting for the overnight window (starts 21:30)")
    assert site.server.requests == []
    stored, _job = await _read(env, draft.id)
    # The wait is the job's; the draft only says it is under way.
    assert stored.state == "running" and stored.cut_id is None

    site.clock.at = NIGHT
    await _drain(env)

    stored, job = await _read(env, draft.id)
    assert (stored.state, job.state if job else None) == ("done", "done")
    assert (await _cut(env, stored.cut_id))[0].model == LARGE


async def test_a_paused_lane_holds_a_draft_and_does_not_refuse_it(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    async with env["factory"]() as db:
        config = await load_config(db)
        config.paused = True
        await save_config(db, config)
        await db.commit()
        status = await drafts.editor_status(db, settings=site.settings)
    assert (status.paused, status.why_not) == (True, None)

    draft = await _ask_for(env)
    await _drain(env)

    assert await _shown(env, draft.id) == ("waiting", "Local AI is paused.", False)


async def test_a_model_that_would_not_load_fails_the_draft_with_its_sentence(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    site.server.refuse("load_failed")

    draft = await _ask_for(env)
    await _drain(env)

    stored, job = await _read(env, draft.id)
    assert (stored.state, stored.message) == ("failed", "It would not load.")
    assert job is not None and (job.state, job.message) == ("failed", "It would not load.")
    # Which model failed is still said.
    assert stored.model == EDITOR and stored.cut_id is None
    assert drafts.shown(stored, job) == ("failed", "It would not load.", False)
    assert await _count(env, Cut) == 0


async def test_prose_instead_of_a_cut_is_kept_and_shown(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    site.server.prose_first(2)  # the question and its one repair turn

    draft = await _ask_for(env)
    await _drain(env)

    stored, job = await _read(env, draft.id)
    assert (stored.state, stored.message, stored.answer_text) == (
        "failed",
        drafts.NOT_A_FORM,
        fake.PROSE,
    )
    assert job is not None and (job.state, job.message, job.detail) == (
        "failed",
        drafts.NOT_A_FORM,
        fake.PROSE,
    )
    assert await _count(env, Cut) == 0


async def test_an_answer_that_ran_out_of_room_says_so(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    cut_short = '{"title": "Teaser", "cuts": [{"shot": 1, "in_s": 0.5, "out_s"'
    fake.FakeModelServer.answer_for("EditorAnswer", lambda _body: cut_short)

    draft = await _ask_for(env)
    await _drain(env)

    stored, job = await _read(env, draft.id)
    assert (stored.state, stored.message, stored.answer_text) == (
        "failed",
        drafts.OUT_OF_ROOM,
        cut_short,
    )
    assert job is not None and job.message == drafts.OUT_OF_ROOM


async def test_an_answer_with_no_usable_shot_fails_and_lists_why(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    _answer(
        [
            {"shot": 91, "in_s": 1.0, "out_s": 4.0, "reason": "Invented."},
            {"shot": 0, "in_s": 1.0, "out_s": 4.0, "reason": "Numbered from nought."},
        ]
    )

    draft = await _ask_for(env)
    await _drain(env)

    stored, job = await _read(env, draft.id)
    assert (stored.state, stored.message) == ("failed", drafts.NOTHING_USABLE)
    assert stored.problems[0] == "Cut 1 named shot 91, which is not on the list."
    assert stored.problems[1] == "Cut 2 named shot 0, which is not on the list."
    # The model answered: the job is done, the finding is the draft's.
    assert job is not None and (job.state, job.result) == ("done", {"cuts": 0})
    assert stored.model == EDITOR and stored.answer_text and stored.demo is False
    assert await _count(env, Cut) == 0


async def _job_pruned(env: dict[str, Any], draft: CutDraft) -> tuple[str, str, bool]:
    """How the draft reads once its job row has gone (pruned): only what the
    draft itself was told is left."""
    async with env["factory"]() as db:
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id == draft.job_id))
        await db.commit()
    return await _shown(env, draft.id)


async def test_a_cut_that_cannot_be_written_fails_the_draft_with_its_sentence_and_findings(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    draft = await _ask_for(env)
    async with env["factory"]() as db:
        gone = env["clips"][0].id
        await db.execute(update(Asset).where(Asset.id == gone).values(availability="offline"))
        await db.commit()

    async def refuses(*_args: Any, **_kwargs: Any) -> Any:
        raise roughcut.CutError("The timeline could not be written.")

    monkeypatch.setattr(roughcut, "build_rough_cut", refuses)
    await _drain(env)

    stored, job = await _read(env, draft.id)
    assert (stored.state, stored.message) == ("failed", "The timeline could not be written.")
    assert job is not None and (job.state, job.message) == (stored.state, stored.message)
    # What the check found and what the Editor wrote are kept with it.
    assert len(stored.problems) == 2 and all("was left out" in p for p in stored.problems)
    assert stored.answer_text and stored.model == EDITOR and stored.cut_id is None
    assert await _job_pruned(env, stored) == (
        "failed",
        "The timeline could not be written.",
        False,
    )


async def test_a_model_that_may_never_run_fails_the_draft_in_its_own_words(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await env["site"].editor(env["factory"])
    sentence = "It is no longer on the approved list."

    async def not_approved(*_args: Any, **_kwargs: Any) -> Any:
        raise NotNow("not_approved", sentence)

    monkeypatch.setattr(drafts, "ask", not_approved)
    draft = await _ask_for(env)
    await _drain(env)

    stored, job = await _read(env, draft.id)
    # Not a wait: asking again later cannot work by itself.
    assert (stored.state, stored.message) == ("failed", sentence)
    assert job is not None and (job.state, job.message) == ("failed", sentence)
    assert await _job_pruned(env, stored) == ("failed", sentence, False)


async def test_a_handler_that_crashes_or_an_install_that_cannot_run_ends_the_draft_too(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both end the job without the handler's own words: the kind's tidy-up
    writes the job's sentence on the draft, which outlives the job row."""
    site: Site = env["site"]
    await site.editor(env["factory"])

    async def crashes(*_args: Any, **_kwargs: Any) -> Any:
        raise LookupError("a bug")

    with monkeypatch.context() as patched:
        patched.setattr(drafts, "ask", crashes)
        crashed = await _ask_for(env)
        await _drain(env)
    stored, job = await _read(env, crashed.id)
    assert job is not None and job.state == "failed"
    assert job.message == "It failed unexpectedly (LookupError)."
    assert (stored.state, stored.message) == ("failed", job.message)
    assert await _job_pruned(env, stored) == ("failed", job.message, False)

    # Queued, then this install stops being one that can run models.
    stranded = await _ask_for(env, title="Stranded")
    site.settings = Settings(data_dir=site.data, models_dir=site.models, secret_key=SECRET)
    await _drain(env)
    stored, job = await _read(env, stranded.id)
    assert job is not None and (job.state, job.message) == ("failed", UNAVAILABLE)
    assert (stored.state, stored.message) == ("failed", UNAVAILABLE)
    assert await _job_pruned(env, stored) == ("failed", UNAVAILABLE, False)
    assert site.server.requests == []


async def test_a_clip_that_went_offline_while_the_draft_waited_is_left_out(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    draft = await _ask_for(env)
    gone = env["clips"][0]
    async with env["factory"]() as db:
        await db.execute(update(Asset).where(Asset.id == gone.id).values(availability="offline"))
        await db.commit()

    await _drain(env)

    stored, _job = await _read(env, draft.id)
    assert stored.state == "done"
    left = [p for p in stored.problems if "was left out" in p]
    assert len(left) == 2 and all("is no longer on the share" in p for p in left)
    _cut_row, pieces = await _cut(env, stored.cut_id)
    assert len(pieces) == 4 and gone.id not in {p.asset_id for p in pieces}


async def test_cancel_while_waiting_is_immediate_and_while_running_is_noticed(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"], LARGE, memory_gb=10.1)

    # Waiting for 21:30: cancelled at once, and never run.
    waiting = await _ask_for(env)
    await _drain(env)
    async with env["factory"]() as db:
        row = await db.get(CutDraft, waiting.id)
        assert row is not None
        after = await drafts.cancel(db, row)
        await db.commit()
    assert after.state == "cancelled"
    assert await _shown(env, waiting.id) == ("cancelled", jobs.CANCELLED, False)
    site.clock.at = NIGHT
    await _drain(env)
    assert site.server.requests == [] and await _count(env, Cut) == 0
    with pytest.raises(DraftError) as raised:
        async with env["factory"]() as db:
            row = await db.get(CutDraft, waiting.id)
            assert row is not None
            await drafts.cancel(db, row)
    assert (str(raised.value), raised.value.status) == (jobs.FINISHED_ALREADY, 409)

    # Running: Cancel is pressed while the model answers. The page reads
    # "stopping", and the answer, when it comes, is thrown away.
    real = drafts.ask
    seen: list[tuple[str, str, bool]] = []

    async def answers_after_cancel(db: AsyncSession, who: str, **kwargs: Any) -> Any:
        async with env["factory"]() as other:
            row = await other.scalar(select(CutDraft).where(CutDraft.id == running.id))
            assert row is not None
            await drafts.cancel(other, row)
            await other.commit()
        seen.append(await _shown(env, running.id))
        return await real(db, who, **kwargs)

    monkeypatch.setattr(drafts, "ask", answers_after_cancel)
    running = await _ask_for(env, title="Second try")
    await _drain(env)

    assert seen == [("running", drafts.RUNNING_SAYS, True)]
    stored, job = await _read(env, running.id)
    assert (stored.state, stored.cut_id) == ("cancelled", None)
    assert job is not None and job.state == "cancelled"
    assert await _count(env, Cut) == 0


async def test_a_second_delivery_does_nothing(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    draft = await _ask_for(env)
    await _drain(env)
    stored, job = await _read(env, draft.id)
    assert stored.state == "done" and job is not None

    ctx = jobs.JobContext(job.id, site.deps(), env["factory"])
    again = await drafts.run_draft(ctx, job)

    assert again == {"skipped": True}
    assert await _count(env, Cut) == 1 and len(site.server.requests) == 1


async def test_a_draft_whose_job_row_is_gone_reads_as_stopped(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.editor(env["factory"])
    pruned = await _ask_for(env)
    async with env["factory"]() as db:
        job = await db.get(LocalAiJob, pruned.job_id)
        await db.delete(job)
        await db.commit()
    assert await _shown(env, pruned.id) == ("failed", drafts.STOPPED, False)

    # The job ended "done" but the draft was never told (the handler died
    # between writing the cut and writing the draft): stopped, not "running".
    unfinished = await _ask_for(env, title="Unfinished")
    async with env["factory"]() as db:
        await db.execute(
            update(LocalAiJob).where(LocalAiJob.id == unfinished.job_id).values(state="done")
        )
        await db.commit()
    assert await _shown(env, unfinished.id) == ("failed", drafts.STOPPED, False)

    # A worker that died mid-draft: read as stopped at once, and written so
    # by the next tick, on the draft too.
    died = await _ask_for(env, title="Died")
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    async with env["factory"]() as db:
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == died.job_id)
            .values(state="running", started_at=long_ago, beat_at=long_ago)
        )
        await db.execute(update(CutDraft).where(CutDraft.id == died.id).values(state="running"))
        await db.commit()
    draft, job = await _read(env, died.id)
    assert drafts.shown(draft, job, datetime.now(UTC)) == ("failed", drafts.STOPPED, False)
    await _drain(env)
    stored, _ = await _read(env, died.id)
    assert (stored.state, stored.message) == ("failed", drafts.STOPPED)


async def test_drafting_again_writes_the_next_version_of_the_same_cut(
    env: dict[str, Any],
) -> None:
    await env["site"].editor(env["factory"])
    first = await _ask_for(env)
    await _drain(env)
    first, _ = await _read(env, first.id)
    assert first.cut_id is not None

    again = await _ask_for(env, revises=first.cut_id, title="Pete Dye, tighter")
    await _drain(env)
    again, _ = await _read(env, again.id)

    second, _pieces = await _cut(env, again.cut_id)
    assert (second.version, second.root_id, second.parent_id) == (2, first.cut_id, first.cut_id)
    assert again.revises_cut_id == first.cut_id

    # The cut it revises is deleted while the draft waits: it becomes a
    # first version of its own, and says why.
    third = await _ask_for(env, revises=again.cut_id, title="Third")
    async with env["factory"]() as db:
        gone = await db.get(Cut, again.cut_id)
        await db.delete(gone)
        await db.commit()
    await _drain(env)
    third, _ = await _read(env, third.id)
    assert third.state == "done" and drafts.PARENT_GONE in third.problems
    own, _ = await _cut(env, third.cut_id)
    assert (own.version, own.root_id, own.parent_id) == (1, None, None)


# --- what registers it ---------------------------------------------------------------


def test_the_kind_is_registered_once_the_lane_is_imported() -> None:
    from framefound.processing import local_ai_tasks

    assert drafts.KIND in jobs.registered()
    # One line, at the foot, after L4's (L1 3.18), so the lane's own module
    # never has to know what a draft is.
    tree = ast.parse(Path(local_ai_tasks.__file__).read_text(encoding="utf-8"))
    imported = [
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module in ("framefound.nle", "framefound.processing")
        for alias in node.names
    ]
    assert imported[-2:] == ["colour_jobs", "drafts"]


def test_drafts_never_imports_the_connector() -> None:
    """The `mcp` package is an optional extra (P1): nothing the API or the lane
    imports may need it."""
    tree = ast.parse(Path(drafts.__file__).read_text(encoding="utf-8"))
    names = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not [n for n in names if n == "mcp" or n.startswith(("mcp.", "framefound.mcp"))]
