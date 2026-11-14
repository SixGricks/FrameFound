"""One model's turn in a comparison (framefound/local_models/compare_job.py).

The turn runs on the real lane: `jobs.run_next` is awaited directly (no queue,
no worker), the real `ask()` is in front of `fake.FakeModelServer`, the models
are L1's fixtures shrunk to a few kilobytes and placed in a library under
`tmp_path`, and the clock is a `Clock` the test moves. Where a test needs to
act between two items (press Cancel, queue a Test, kill the worker, close the
overnight window) it wraps the real `ask` rather than replacing it. The
comparison is made as the API makes it: `comparisons.create`, then
`sampling.freeze` of real JPEGs drawn here, then `set_pictures` and
`add_models`. A fixture makes starting a process or reaching a real address
fail the test.
"""

import asyncio
import contextlib
import dataclasses
import io
import json
import re
import subprocess
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.models import (
    Asset,
    AssetTag,
    Comparison,
    ComparisonAnswer,
    ComparisonItem,
    ComparisonModel,
    Derivative,
    Library,
    LocalAiJob,
    LocalModel,
    Tag,
)
from framefound.local_models import (
    UNAVAILABLE,
    compare_job,
    comparisons,
    fake,
    handlers,  # noqa: F401 - registers L1's kinds and, at its foot, compare_model
    jobs,
    library,
    preflight,
    registry,
    runtimes,
    sampling,
)
from framefound.local_models.ask import ModelFailed, NotNow
from framefound.local_models.ask import ask as real_ask
from framefound.local_models.deps import Deps
from framefound.local_models.kinds import MlxVlm
from framefound.local_models.sampling import Candidate, Drawn, SampleSpec
from framefound.local_models.settings_store import LocalModelsConfig, load_config, save_config
from framefound.looking import prompt
from framefound.storage import stores

Factory = async_sessionmaker[AsyncSession]

SECRET = "not-a-real-secret-key"
FIRST = "mlx-community/gemma-4-e4b-it-4bit"
SECOND = "mlx-community/demo-large-4bit"
VERSION = "0.7.12"
AFTERNOON = datetime(2026, 11, 14, 14, 0)

_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_MODELS_DIR",
    "FRAMEFOUND_DATA_DIR",
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
    "FRAMEFOUND_SECRET_KEY",
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
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", SECRET)
    # Freezing the pictures checks the free space against the process's settings.
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "0")
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    get_settings.cache_clear()
    stores._CACHE.clear()


@pytest.fixture(autouse=True)
def _kinds_and_answers_restored() -> Iterator[None]:
    """Kinds a test registers, and looks it scripts on the fake server, go with it."""
    before = dict(jobs._KINDS)
    yield
    jobs._KINDS.clear()
    jobs._KINDS.update(before)
    fake.FakeModelServer.answer_for("LookAnswer", None)


@pytest.fixture(autouse=True)
def _every_tick_looks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress written, and Cancel looked for, at every item rather than every two seconds."""
    monkeypatch.setattr(jobs, "PROGRESS_EVERY_S", 0.0)


@pytest.fixture()
async def factory(tmp_path: Path) -> AsyncIterator[Factory]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Clock:
    """The Mac's wall clock, naive, moved by the test."""

    def __init__(self, at: datetime = AFTERNOON) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class Site:
    """A Mac with MLX-VLM installed, a model library and a data folder."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.data = tmp_path / "data"
        self.models = tmp_path / "models"
        self.runtimes = tmp_path / "runtimes"
        for folder in (self.data, self.models, self.runtimes):
            folder.mkdir()
        self.hub = fake.FakeHub.from_fixtures(sleep=lambda _seconds: None)
        self.hub.shrink(FIRST)
        self.hub.shrink(SECOND)
        self.settings = Settings(
            data_dir=self.data,
            models_dir=self.models,
            runtimes_dir=self.runtimes,
            secret_key=SECRET,
            min_free_gb=0,
        )
        self.clock = Clock()
        self.server = fake.FakeModelServer(sleep=lambda _seconds: None)
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

    async def add(self, factory: Factory, repo: str = FIRST) -> uuid.UUID:
        """A model checked and added as the page would; its files in the library."""
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
            await db.commit()
            files = registry.snapshot(model)
        library.download(self.hub, files, settings=self.settings)
        return model.id

    def still(self, name: str, colour: tuple[int, int, int]) -> Path:
        """A small JPEG where processing would have left a still."""
        path = self.root / "stills" / name
        path.parent.mkdir(exist_ok=True)
        out = io.BytesIO()
        Image.new("RGB", (480, 270), colour).save(out, "JPEG", quality=85)
        path.write_bytes(out.getvalue())
        return path


@pytest.fixture()
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


# --- the sample ------------------------------------------------------------------------

CLIP = uuid.UUID(int=11)
PHOTO = uuid.UUID(int=12)
VOCABULARY = (("drone", 12), ("golf course", 7), ("barn", 3))
# What the catalogue knows and the model must never be told.
SECRETS = ("DJI_0042", "IMG_7731", "Brookside", "GELCO", "Quick 30", "pond", "sunset")


def _shot(site: Site, start: int, *, used: bool) -> Candidate:
    tone = 40 + start // 100
    return Candidate(
        kind="shot",
        asset_id=CLIP,
        library_name="GELCO",
        relative_path="Brookside/2026/DJI_0042.MP4",
        filename="DJI_0042.MP4",
        media_type="video",
        start_ms=start,
        end_ms=start + 4000,
        facts={"length_s": 4.0, "move": "orbit"},
        facts_line="4.0 seconds long; the camera orbits right, slowly; light: golden.",
        truth_yes=(("pond", "pond"),),
        truth_no=(("sunset", "sunset"),),
        used=used,
        used_in=("Brookside Quick 30",) if used else (),
        pictures=tuple(
            (start + offset, site.still(f"{start}-{offset}.jpg", (tone, 120 + offset // 20, 90)))
            for offset in (200, 2000, 3800)
        ),
    )


def _photo(site: Site) -> Candidate:
    return Candidate(
        kind="photo",
        asset_id=PHOTO,
        library_name="GELCO",
        relative_path="Brookside/2026/Stills/IMG_7731.JPG",
        filename="IMG_7731.JPG",
        media_type="image",
        start_ms=None,
        end_ms=None,
        facts={},
        facts_line="",
        truth_yes=(("pond", "pond"),),
        truth_no=(),
        used=None,
        used_in=(),
        pictures=((0, site.still("photo.jpg", (200, 180, 160))),),
    )


async def make(
    factory: Factory, site: Site, *models: uuid.UUID, shots: int = 3, photo: bool = True
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """A comparison of `shots` shots of one clip (and a photograph), frozen, with
    a column for each model; as the API makes one."""
    items = tuple(_shot(site, n * 4000, used=n == 0) for n in range(shots))
    if photo:
        items = (*items, _photo(site))
    drawn = Drawn(
        label="GELCO › Brookside/2026",
        items=items,
        pool={"photos": 1, "shots": shots, "clips": 1},
        counted_cuts=((uuid.UUID(int=99), "Brookside Quick 30", "import", datetime.now(UTC)),),
        left_out={},
        vocabulary=VOCABULARY,
        spec=SampleSpec(kind="folder", what="both", size=60, stills=3, seed=7),
    )
    async with factory() as db:
        comparison = await comparisons.create(
            db,
            SampleSpec(kind="folder", folder="Brookside/2026"),
            drawn,
            name="Brookside",
            user_id=None,
        )
        frozen = sampling.freeze(comparison.id, drawn.items, site.settings)
        await comparisons.set_pictures(db, comparison, frozen)
        rows = [await registry.get_model(db, model_id) for model_id in models]
        entries = await comparisons.add_models(
            db, comparison, [row for row in rows if row is not None], user_id=None
        )
        await db.commit()
        return comparison.id, [entry.id for entry in entries]


async def drain(factory: Factory, deps: Deps, limit: int = 30) -> None:
    """What the lane does when woken: run jobs until none is queued."""
    for _ in range(limit):
        if not await jobs.run_next(factory, deps):
            return
    raise AssertionError("the lane never ran out of work")


async def entry_row(factory: Factory, entry_id: uuid.UUID) -> ComparisonModel:
    async with factory() as db:
        found = await db.get(ComparisonModel, entry_id)
        assert found is not None
        return found


async def job_row(factory: Factory, entry_id: uuid.UUID) -> LocalAiJob:
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None and entry.job_id is not None
        job = await db.get(LocalAiJob, entry.job_id)
        assert job is not None
        return job


async def answers(factory: Factory, entry_id: uuid.UUID) -> list[tuple[int, ComparisonAnswer]]:
    """The column's answers with their items' positions, by position."""
    async with factory() as db:
        rows = await db.execute(
            select(ComparisonItem.position, ComparisonAnswer)
            .join(ComparisonItem, ComparisonItem.id == ComparisonAnswer.item_id)
            .where(ComparisonAnswer.entry_id == entry_id)
            .order_by(ComparisonItem.position)
        )
        return [(int(position), found) for position, found in rows.all()]


async def shown(factory: Factory, site: Site, entry_id: uuid.UUID) -> tuple[str, str, Any]:
    """The column as a page reads it, at the site's clock."""
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        job = None if entry.job_id is None else await db.get(LocalAiJob, entry.job_id)
        return comparisons.shown(entry, job, now=site.clock())


Ask = Callable[..., Awaitable[Any]]


def wrap_ask(monkeypatch: pytest.MonkeyPatch, before: Callable[[int], Awaitable[None]]) -> None:
    """The real `ask`, with `before(n)` awaited ahead of the n-th question (from 1)."""
    calls = 0

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        await before(calls)
        return await real_ask(*args, **kwargs)

    monkeypatch.setattr(compare_job, "ask", wrapped)


def texts(body: dict[str, Any]) -> str:
    return "\n".join(
        part["text"] for part in body["messages"][0]["content"] if part["type"] == "text"
    )


def look(**values: Any) -> str:
    answer: dict[str, Any] = {
        "caption": "A pond beside a red barn at golden hour.",
        "score": 72,
        "reason": "Sharp and well lit; the horizon is level.",
        "matches": [],
        "new_tags": [],
    }
    answer.update(values)
    return json.dumps(answer)


# --- what every model is shown ------------------------------------------------------------


async def test_every_model_is_shown_the_same_pictures_and_the_same_words(
    factory: Factory, site: Site
) -> None:
    first = await site.add(factory, FIRST)
    second = await site.add(factory, SECOND)
    _comparison, entries = await make(factory, site, first, second)
    await drain(factory, site.deps())

    by_model: dict[str, list[list[dict[str, Any]]]] = {}
    for body in site.server.requests:
        by_model.setdefault(body["model"], []).append(body["messages"][0]["content"])
    assert len(by_model) == 2
    one, other = by_model.values()
    # Four items each, asked in the same order with the same message: the
    # same words and the very same picture bytes.
    assert len(one) == len(other) == 4
    assert one == other
    pictures = [[part for part in content if part["type"] != "text"] for content in one]
    assert [len(shown) for shown in pictures] == [3, 3, 3, 1]
    # A shot is told its facts sentence and what its three pictures are; a
    # photograph neither.
    words = [next(part["text"] for part in content if part["type"] == "text") for content in one]
    assert words[0].startswith(prompt.INSTRUCTIONS)
    assert "start, middle and end of one shot" in words[0]
    assert "the camera orbits right, slowly" in words[0]
    assert "This is one photograph." in words[3] and "Measured" not in words[3]
    for entry_id in entries:
        assert (await entry_row(factory, entry_id)).state == "done"
        assert [position for position, _a in await answers(factory, entry_id)] == [0, 1, 2, 3]


async def test_a_model_is_never_told_the_truth(factory: Factory, site: Site) -> None:
    model = await site.add(factory)
    await make(factory, site, model)
    await drain(factory, site.deps())
    assert len(site.server.requests) == 4
    for body in site.server.requests:
        said = texts(body)
        for secret in SECRETS:
            assert secret.lower() not in said.lower(), secret
        # Whether a shot was used in a cut is never said, in any words.
        assert not re.search(r"\bused\b", said, re.IGNORECASE)
        # The vocabulary it is shown is the comparison's, frozen.
        assert "- drone (12)" in said and "- golf course (7)" in said


# --- what is kept of an answer --------------------------------------------------------------


async def test_every_proposal_is_kept_with_its_confidence(factory: Factory, site: Site) -> None:
    fake.FakeModelServer.answer_for(
        "LookAnswer",
        lambda _body: look(
            matches=[
                {"tag": "drone", "confidence": 0.31},
                {"tag": "barn", "confidence": 0.12},
                {"tag": "drone", "confidence": 0.7},
            ],
            new_tags=[{"tag": "silo", "confidence": 0.05}],
        ),
    )
    model = await site.add(factory)
    _comparison, (entry,) = await make(factory, site, model, shots=1, photo=False)
    await drain(factory, site.deps())
    ((_position, stored),) = await answers(factory, entry)
    # Nothing under any floor is dropped, the same word said twice is kept once
    # at its stronger confidence, and the strongest comes first.
    assert stored.tags == [
        {"name": "drone", "slug": "drone", "confidence": 0.7, "in_vocabulary": True},
        {"name": "barn", "slug": "barn", "confidence": 0.12, "in_vocabulary": True},
        {"name": "silo", "slug": "silo", "confidence": 0.05, "in_vocabulary": False},
    ]
    assert (stored.caption, stored.score, stored.reason) == (
        "A pond beside a red barn at golden hour.",
        72,
        "Sharp and well lit; the horizon is level.",
    )


async def test_a_reworded_match_is_a_new_word(factory: Factory, site: Site) -> None:
    fake.FakeModelServer.answer_for(
        "LookAnswer",
        lambda _body: look(
            matches=[{"tag": "Golf Courses", "confidence": 0.9}],
            new_tags=[{"tag": "drone", "confidence": 0.4}],
        ),
    )
    model = await site.add(factory)
    _comparison, (entry,) = await make(factory, site, model, shots=1, photo=False)
    await drain(factory, site.deps())
    ((_position, stored),) = await answers(factory, entry)
    by_slug = {tag["slug"]: tag["in_vocabulary"] for tag in stored.tags}
    # Which list the model put a word in decides nothing; the slug does.
    assert by_slug == {"golf-courses": False, "drone": True}


async def test_an_answer_is_kept_as_it_was_given_with_time_memory_and_build(
    factory: Factory, site: Site
) -> None:
    site.server.memory_gb = 6.1
    site.server.load_seconds = 14.0
    site.server.answer_seconds = 2.5
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    await drain(factory, site.deps())

    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message, entry.done, entry.failed) == ("done", "", 4, 0)
    assert (entry.runtime, entry.runtime_version, entry.ran_from) == (
        "mlx-vlm",
        VERSION,
        "this_machine",
    )
    assert (entry.memory_gb, entry.memory_source, entry.load_seconds) == (6.1, "runtime", 14.0)
    assert entry.started_at is not None and entry.finished_at is not None
    for _position, stored in await answers(factory, entry_id):
        assert (stored.ok, stored.seconds, stored.memory_gb, stored.attempts) == (
            True,
            2.5,
            6.1,
            1,
        )
        assert stored.raw is None  # kept only when a second try was needed
    job = await job_row(factory, entry_id)
    assert (job.state, job.result) == ("done", {"answered": 4, "failed": 0})
    assert (job.done, job.total) == (4, 4)
    assert (job.model, job.prompt_version, job.runtime_version) == (
        FIRST,
        prompt.PROMPT_VERSION,
        VERSION,
    )
    assert jobs.title_of(job) == "Comparing gemma-4-e4b-it-4bit on Brookside"
    assert await shown(factory, site, entry_id) == ("done", "", None)


async def test_a_second_try_keeps_what_the_model_wrote(factory: Factory, site: Site) -> None:
    site.server.prose_first(times=1)
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model, shots=1, photo=False)
    await drain(factory, site.deps())
    ((_position, stored),) = await answers(factory, entry_id)
    assert stored.ok and stored.attempts == 2
    assert stored.raw is not None and stored.raw.startswith("{")


# --- what goes wrong, and what a column says about it ------------------------------------


async def test_a_bad_answer_is_recorded_and_the_run_goes_on(factory: Factory, site: Site) -> None:
    site.server.prose_first(times=2)  # the first item: words, then words again
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    await drain(factory, site.deps())
    kept = await answers(factory, entry_id)
    first = kept[0][1]
    assert (first.ok, first.problem, first.attempts) == (False, "bad_answer", 2)
    assert first.raw == fake.PROSE and first.message.startswith("Its answer was not the form")
    assert [found.ok for _p, found in kept] == [False, True, True, True]
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.done, entry.failed) == ("done", 3, 1)
    assert (await job_row(factory, entry_id)).state == "done"


async def test_three_broken_answers_in_a_row_stop_the_run(factory: Factory, site: Site) -> None:
    site.server.refuse("died", times=3)
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model, shots=4)
    await drain(factory, site.deps())

    kept = await answers(factory, entry_id)
    assert [(position, found.problem) for position, found in kept] == [
        (0, "died"),
        (1, "died"),
        (2, "died"),
    ]
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.failed) == ("stopped", 3)
    assert entry.message == (
        "It failed three items in a row (the model stopped unexpectedly), so it was stopped "
        "rather than left to grind through the rest."
    )
    assert (await job_row(factory, entry_id)).state == "failed"
    assert (await shown(factory, site, entry_id))[0] == "stopped"

    # Carry on asks only what has no answer; trying the failed ones again asks those too.
    async with factory() as db:
        found = await db.get(ComparisonModel, entry_id)
        assert found is not None
        await comparisons.carry_on(db, found, retry_failed=False, user_id=None, now=site.clock())
        await db.commit()
    asked = len(site.server.requests)
    await drain(factory, site.deps())
    assert len(site.server.requests) - asked == 2
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.done, entry.failed) == ("done", 2, 3)


async def test_broken_answers_that_are_not_in_a_row_do_not_stop_it(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def breaks(n: int) -> None:
        if n in (1, 2, 4, 5):
            raise ModelFailed("timeout", "It took longer than 5 minutes to answer.")

    wrap_ask(monkeypatch, breaks)
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model, shots=6, photo=False)
    await drain(factory, site.deps())
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.done, entry.failed) == ("done", 2, 4)


async def test_a_model_that_will_not_load_fails_once_not_sixty_times(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def will_not_load(_n: int) -> None:
        raise ModelFailed(
            "load_failed", "It would not load.", detail="RuntimeError: weights do not fit"
        )

    wrap_ask(monkeypatch, will_not_load)
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    await drain(factory, site.deps())
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message, entry.detail) == (
        "failed",
        "It would not load.",
        "RuntimeError: weights do not fit",
    )
    # Not the items' fault: nothing is recorded against them.
    assert await answers(factory, entry_id) == []
    job = await job_row(factory, entry_id)
    assert (job.state, job.message) == ("failed", "It would not load.")


async def test_a_missing_picture_is_recorded_and_the_rest_are_asked(
    factory: Factory, site: Site
) -> None:
    model = await site.add(factory)
    comparison_id, (entry_id,) = await make(factory, site, model)
    sampling.picture_path(comparison_id, 1, 2, site.settings).unlink()
    await drain(factory, site.deps())
    kept = await answers(factory, entry_id)
    assert [(found.ok, found.problem) for _p, found in kept] == [
        (True, None),
        (False, "no_picture"),
        (True, None),
        (True, None),
    ]
    assert kept[1][1].message == compare_job.NO_PICTURE
    # The item is not asked with two of its three pictures.
    assert len(site.server.requests) == 3


async def test_a_long_load_does_not_make_the_column_look_dead(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ask() can outlast STALE_AFTER_S (a five-minute load). The runner's own
    heartbeat keeps the turn alive; the handler adds nothing for it."""
    monkeypatch.setattr(jobs, "HEARTBEAT_S", 0.01)
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model, shots=1, photo=False)
    seen: list[str] = []

    async def loads_slowly(_n: int) -> None:
        for _ in range(3):
            site.clock.advance(jobs.STALE_AFTER_S * 0.8)
            await asyncio.sleep(0.1)
            seen.append((await shown(factory, site, entry_id))[0])

    wrap_ask(monkeypatch, loads_slowly)
    await drain(factory, site.deps())
    assert seen == ["running"] * 3
    assert (await entry_row(factory, entry_id)).state == "done"


async def test_an_overnight_model_waits_and_keeps_what_it_has(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await site.add(factory)
    async with factory() as db:
        await db.execute(update(LocalModel).where(LocalModel.id == model).values(memory_gb=10.0))
        await db.commit()
    _comparison, (entry_id,) = await make(factory, site, model)
    # Overnight, with two items to go before the window closes at 05:30.
    site.clock.at = datetime(2026, 11, 14, 5, 28)

    async def morning_comes(n: int) -> None:
        # The time each question is asked at is read before it is put, so
        # the clock moved during the second is the third's.
        if n == 2:
            site.clock.at = datetime(2026, 11, 14, 5, 31)

    wrap_ask(monkeypatch, morning_comes)
    await drain(factory, site.deps())
    state, message, not_before = await shown(factory, site, entry_id)
    assert state == "waiting"
    assert message == (
        "Waiting for the overnight window (starts 21:30): it needs about 10 GB and the "
        "daytime budget is 8 GB."
    )
    assert not_before == datetime(2026, 11, 14, 21, 30).astimezone(UTC)
    before = await answers(factory, entry_id)
    assert [position for position, _a in before] == [0, 1]

    site.clock.at = datetime(2026, 11, 14, 14, 0)
    await drain(factory, site.deps())  # still the afternoon: nothing happens
    assert (await shown(factory, site, entry_id))[0] == "waiting"
    site.clock.at = datetime(2026, 11, 14, 21, 31)
    await drain(factory, site.deps())
    after = await answers(factory, entry_id)
    assert [position for position, _a in after] == [0, 1, 2, 3]
    # What it had answered before the wait is the same rows, untouched.
    assert [found.id for _p, found in after[:2]] == [found.id for _p, found in before]
    assert (await entry_row(factory, entry_id)).state == "done"


async def test_a_paused_lane_holds_the_column_and_says_so(factory: Factory, site: Site) -> None:
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    async with factory() as db:
        config = await load_config(db)
        config.paused = True
        await save_config(db, config)
        await db.commit()
    await drain(factory, site.deps())
    assert site.server.requests == []
    assert (await shown(factory, site, entry_id))[:2] == ("waiting", "Local AI is paused.")


async def test_a_model_that_cannot_run_at_all_fails_with_the_reason(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def taken_away(n: int) -> None:
        if n == 2:
            raise NotNow("not_approved", "It is no longer approved: its publisher is blocked.")

    wrap_ask(monkeypatch, taken_away)
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    await drain(factory, site.deps())
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message) == (
        "failed",
        "It is no longer approved: its publisher is blocked.",
    )
    assert [position for position, _a in await answers(factory, entry_id)] == [0]


# --- resuming: every turn starts at the items with no answer -------------------------------


async def test_a_restarted_worker_carries_on_where_it_stopped(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    never = asyncio.Event()
    parked = asyncio.Event()

    async def worker_dies_on_the_third(n: int) -> None:
        if n == 3:
            parked.set()
            await never.wait()

    wrap_ask(monkeypatch, worker_dies_on_the_third)
    running = asyncio.ensure_future(jobs.run_next(factory, site.deps()))
    # The kill lands while the third question is being asked, not at a moment
    # chosen by polling: a coroutine cancelled half way through a statement
    # leaves its SQLite connection holding the file's lock ("database is
    # locked" five seconds later), which a worker whose process died does not.
    await asyncio.wait_for(parked.wait(), timeout=10)
    assert len(await answers(factory, entry_id)) == 2
    # The worker is killed: nothing writes how the turn ended.
    running.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await running
    await asyncio.sleep(0.05)
    assert (await job_row(factory, entry_id)).state == "running"

    site.clock.advance(jobs.STALE_AFTER_S + 5)
    assert await shown(factory, site, entry_id) == ("stopped", comparisons.STOPPED, None)
    # The lane's next tick writes it down, on the column too; it does not
    # start the turn again by itself.
    monkeypatch.setattr(compare_job, "ask", real_ask)
    await drain(factory, site.deps())
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message) == ("stopped", comparisons.STOPPED)
    assert len(site.server.requests) == 2

    async with factory() as db:
        found = await db.get(ComparisonModel, entry_id)
        assert found is not None
        await comparisons.carry_on(db, found, retry_failed=False, user_id=None, now=site.clock())
        await db.commit()
    await drain(factory, site.deps())
    assert [position for position, _a in await answers(factory, entry_id)] == [0, 1, 2, 3]
    assert len(site.server.requests) == 4  # nothing asked twice
    assert (await entry_row(factory, entry_id)).state == "done"


async def test_a_second_delivery_asks_nothing_twice(factory: Factory, site: Site) -> None:
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    await drain(factory, site.deps())
    assert len(site.server.requests) == 4
    # The same job delivered again (the row put back as a redelivery would find it).
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == entry.job_id)
            .values(state="queued", finished_at=None)
        )
        entry.state = "queued"
        await db.commit()
    await drain(factory, site.deps())
    assert len(site.server.requests) == 4
    assert len(await answers(factory, entry_id)) == 4
    assert (await entry_row(factory, entry_id)).state == "done"


async def test_cancel_is_noticed_between_items(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)

    async def cancel_during_the_second(n: int) -> None:
        if n == 2:
            async with factory() as db:
                entry = await db.get(ComparisonModel, entry_id)
                assert entry is not None
                await comparisons.cancel(db, entry, now=site.clock())
                await db.commit()

    wrap_ask(monkeypatch, cancel_during_the_second)
    await drain(factory, site.deps())
    # The item being asked is finished and kept; the next is not asked.
    assert [position for position, _a in await answers(factory, entry_id)] == [0, 1]
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message) == ("stopped", comparisons.CANCELLED)
    assert (await job_row(factory, entry_id)).state == "cancelled"

    async with factory() as db:
        found = await db.get(ComparisonModel, entry_id)
        assert found is not None
        await comparisons.carry_on(db, found, retry_failed=False, user_id=None, now=site.clock())
        await db.commit()
    monkeypatch.setattr(compare_job, "ask", real_ask)
    await drain(factory, site.deps())
    assert [position for position, _a in await answers(factory, entry_id)] == [0, 1, 2, 3]
    assert (await entry_row(factory, entry_id)).state == "done"


async def test_a_test_goes_ahead_and_the_comparison_resumes(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    async def urgent(_ctx: jobs.JobContext, _job: LocalAiJob) -> dict[str, Any]:
        ran.append("test")
        return {}

    jobs.register("urgent", urgent, needs_model=True, title=lambda _job: "Testing")
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    first_job = (await job_row(factory, entry_id)).id

    async def a_test_is_pressed(n: int) -> None:
        ran.append(f"item {n}")
        if n == 1:
            async with factory() as db:
                await jobs.create(db, "urgent", subject="a test", priority=0)
                await db.commit()

    wrap_ask(monkeypatch, a_test_is_pressed)
    assert await jobs.run_next(factory, site.deps()) is True
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message) == ("queued", compare_job.STEPPED_ASIDE)
    assert await shown(factory, site, entry_id) == ("queued", compare_job.STEPPED_ASIDE, None)
    job = await job_row(factory, entry_id)
    assert (job.id, job.state) == (first_job, "queued")

    await drain(factory, site.deps())
    assert ran == ["item 1", "test", "item 2", "item 3", "item 4"]
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.job_id, entry.done) == ("done", first_job, 4)
    async with factory() as db:
        turns = await db.scalar(
            select(func.count()).select_from(LocalAiJob).where(LocalAiJob.kind == compare_job.KIND)
        )
    assert turns == 1  # the same job row, run again


# --- turns that cannot be run ---------------------------------------------------------------


async def test_a_removed_model_closes_its_column_with_the_reason(
    factory: Factory, site: Site
) -> None:
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    async with factory() as db:
        await db.execute(delete(LocalModel).where(LocalModel.id == model))
        await db.commit()
    await drain(factory, site.deps())
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message) == ("failed", compare_job.REMOVED)
    assert site.server.requests == []


async def test_an_older_question_is_refused(factory: Factory, site: Site) -> None:
    model = await site.add(factory)
    comparison_id, (entry_id,) = await make(factory, site, model)
    async with factory() as db:
        await db.execute(
            update(Comparison).where(Comparison.id == comparison_id).values(prompt_version="look-0")
        )
        await db.execute(
            update(ComparisonModel)
            .where(ComparisonModel.id == entry_id)
            .values(prompt_version="look-0")
        )
        await db.commit()
    await drain(factory, site.deps())
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message) == ("failed", comparisons.older("look-0"))
    assert site.server.requests == []


async def test_a_turn_ended_without_its_handler_closes_its_column(
    factory: Factory, site: Site
) -> None:
    model = await site.add(factory)
    _comparison, (cancelled, gone) = await make(
        factory, site, model, await site.add(factory, SECOND)
    )
    async with factory() as db:
        # Cancelled before its turn, by the job alone: the column says so.
        job = await job_row(factory, cancelled)
        await jobs.request_cancel(db, job.id)
        await db.commit()
        # A column taken away meanwhile: its job ends without complaint.
        other = await job_row(factory, gone)
        await db.execute(delete(ComparisonModel).where(ComparisonModel.id == gone))
        await jobs.request_cancel(db, other.id)
        await db.commit()
    entry = await entry_row(factory, cancelled)
    assert (entry.state, entry.message) == ("stopped", comparisons.CANCELLED)
    assert (await job_row(factory, cancelled)).state == "cancelled"
    await drain(factory, site.deps())
    assert site.server.requests == []


async def test_a_column_taken_away_during_its_turn_gets_no_more_answers(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    job_id = (await job_row(factory, entry_id)).id

    async def taken_away(n: int) -> None:
        if n == 2:
            async with factory() as db:
                await db.execute(
                    delete(ComparisonAnswer).where(ComparisonAnswer.entry_id == entry_id)
                )
                await db.execute(delete(ComparisonModel).where(ComparisonModel.id == entry_id))
                await db.commit()

    wrap_ask(monkeypatch, taken_away)
    await drain(factory, site.deps())
    assert await answers(factory, entry_id) == []
    async with factory() as db:
        job = await db.get(LocalAiJob, job_id)
    assert job is not None and (job.state, job.result) == ("done", {"gone": True})
    assert len(site.server.requests) == 2


async def test_an_old_turn_does_not_touch_a_column_carried_on_since(
    factory: Factory, site: Site
) -> None:
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    old = await job_row(factory, entry_id)
    async with factory() as db:
        newer = await jobs.create(db, compare_job.KIND, subject="the newer turn", priority=5)
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        entry.job_id = newer.id
        await db.commit()
    async with factory() as db:
        stale = await db.get(LocalAiJob, old.id)
        assert stale is not None
        await compare_job.abandoned(db, stale, "stopped")
        await db.commit()
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.job_id) == ("queued", newer.id)


async def _newer_turn(
    db: AsyncSession,
    comparison_id: uuid.UUID,
    entry_id: uuid.UUID,
    model: uuid.UUID,
    *,
    not_before: datetime | None = None,
) -> LocalAiJob:
    """The column carried on under a new job, as Carry on leaves it."""
    newer = await jobs.create(
        db,
        compare_job.KIND,
        subject="the newer turn",
        model_id=model,
        payload={"comparison_id": str(comparison_id), "entry_id": str(entry_id)},
        priority=5,
        not_before=not_before,
    )
    await db.execute(
        update(ComparisonModel)
        .where(ComparisonModel.id == entry_id)
        .values(job_id=newer.id, state="queued")
    )
    return newer


async def test_a_turn_still_running_stops_once_its_column_is_carried_on_under_another(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its heartbeat writes failed, the page showed it stopped, Carry on was
    pressed: the old handler, still going, must not store more answers or
    close the column under the newer turn."""
    model = await site.add(factory)
    comparison_id, (entry_id,) = await make(factory, site, model)
    old = (await job_row(factory, entry_id)).id
    newer: list[uuid.UUID] = []

    async def carried_on_meanwhile(n: int) -> None:
        if n == 2:
            async with factory() as db:
                newer.append((await _newer_turn(db, comparison_id, entry_id, model)).id)
                await db.commit()

    wrap_ask(monkeypatch, carried_on_meanwhile)
    await jobs.run_next(factory, site.deps())  # the old turn alone
    async with factory() as db:
        ended = await db.get(LocalAiJob, old)
    assert ended is not None and (ended.state, ended.result) == ("done", {"gone": True})
    assert [position for position, _a in await answers(factory, entry_id)] == [0]
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.job_id) == ("queued", newer[0])

    await drain(factory, site.deps())  # the newer turn asks the rest
    assert [position for position, _a in await answers(factory, entry_id)] == [0, 1, 2, 3]
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.job_id) == ("done", newer[0])


async def test_an_old_turn_delivered_again_leaves_a_column_carried_on_since(
    factory: Factory, site: Site
) -> None:
    model = await site.add(factory)
    comparison_id, (entry_id,) = await make(factory, site, model)
    old = (await job_row(factory, entry_id)).id
    async with factory() as db:
        # The newer turn waits for the evening; the old row is still queued.
        newer = await _newer_turn(
            db, comparison_id, entry_id, model, not_before=datetime(2030, 1, 1, tzinfo=UTC)
        )
        await db.commit()
    await drain(factory, site.deps())
    assert site.server.requests == []
    async with factory() as db:
        ended = await db.get(LocalAiJob, old)
    assert ended is not None and (ended.state, ended.result) == ("done", {"gone": True})
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.job_id) == ("queued", newer.id)
    assert await answers(factory, entry_id) == []


async def test_a_server_that_cannot_run_models_closes_the_column_with_its_reason(
    factory: Factory, site: Site
) -> None:
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    containers = dataclasses.replace(
        site.deps(), settings=site.settings.model_copy(update={"runtimes_dir": None})
    )
    assert await jobs.run_next(factory, containers) is False
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message) == ("failed", UNAVAILABLE)
    assert site.server.requests == []


async def test_a_turn_that_crashes_says_it_failed_unexpectedly(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await site.add(factory)
    _comparison, (asking, starting) = await make(
        factory, site, model, await site.add(factory, SECOND)
    )

    def broken(*_args: Any, **_kwargs: Any) -> list[bytes]:
        raise RuntimeError("the disk said something odd")

    # Half way through its turn: the handler closes the column itself.
    monkeypatch.setattr(sampling, "read_pictures", broken)
    await jobs.run_next(factory, site.deps())
    entry = await entry_row(factory, asking)
    assert (entry.state, entry.message) == ("failed", compare_job.UNEXPECTED)
    job = await job_row(factory, asking)
    assert (job.state, job.message) == ("failed", "It failed unexpectedly (RuntimeError).")

    # Before its turn had begun: the lane's tidy-up closes it, in the job's words.
    async def no_items(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("the database said something odd")

    monkeypatch.setattr(comparisons, "items_of", no_items)
    await jobs.run_next(factory, site.deps())
    entry = await entry_row(factory, starting)
    assert (entry.state, entry.message) == ("failed", "It failed unexpectedly (RuntimeError).")
    assert site.server.requests == []


@pytest.mark.parametrize("why", ["cancelled", "stopped", "unavailable", "failed"])
async def test_a_turn_whose_column_is_gone_is_closed_without_complaint(
    factory: Factory, site: Site, why: jobs.Why
) -> None:
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    job = await job_row(factory, entry_id)
    async with factory() as db:
        await db.execute(delete(ComparisonModel).where(ComparisonModel.id == entry_id))
        await db.commit()
    async with factory() as db:
        # Called directly: the lane swallows what a hook raises, so only this
        # shows that it raises nothing.
        await compare_job.abandoned(db, job, why)
        await compare_job.abandoned(db, LocalAiJob(kind=compare_job.KIND, payload={}), why)
        await db.commit()


# --- what the pictures folder holds ------------------------------------------------------------


async def test_a_pictures_folder_that_is_not_there_fails_the_turn_and_records_nothing(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data drive that is not mounted looks, file by file, like a missing
    picture; the turn must not mark every item so and close `done`."""
    model = await site.add(factory)
    comparison_id, (entry_id,) = await make(factory, site, model)
    sampling.drop_pictures(comparison_id, site.settings)
    await drain(factory, site.deps())
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message) == ("failed", compare_job.PICTURES_GONE)
    assert await answers(factory, entry_id) == [] and site.server.requests == []
    job = await job_row(factory, entry_id)
    assert (job.state, job.message) == ("failed", compare_job.PICTURES_GONE)
    async with factory() as db:
        assert await comparisons.last_finished_for(db, model) is None

    # It goes away half way through a turn: what was answered is kept.
    other_id, (other_entry,) = await make(factory, site, model)

    async def unplugged(n: int) -> None:
        if n == 2:
            sampling.drop_pictures(other_id, site.settings)

    wrap_ask(monkeypatch, unplugged)
    await drain(factory, site.deps())
    entry = await entry_row(factory, other_entry)
    assert (entry.state, entry.message) == ("failed", compare_job.PICTURES_GONE)
    assert [(p, found.ok) for p, found in await answers(factory, other_entry)] == [
        (0, True),
        (1, True),
    ]


async def test_a_damaged_picture_is_recorded_and_the_rest_are_asked(
    factory: Factory, site: Site
) -> None:
    model = await site.add(factory)
    comparison_id, (entry_id,) = await make(factory, site, model)
    # A frozen file left empty by a crash while it was written.
    sampling.picture_path(comparison_id, 1, 0, site.settings).write_bytes(b"")
    await drain(factory, site.deps())
    kept = await answers(factory, entry_id)
    assert [(found.ok, found.problem) for _p, found in kept] == [
        (True, None),
        (False, comparisons.UNSHOWN),
        (True, None),
        (True, None),
    ]
    assert kept[1][1].message == compare_job.DAMAGED
    assert len(site.server.requests) == 3
    assert (await entry_row(factory, entry_id)).state == "done"


# --- carrying on, cancelling, trying again ----------------------------------------------------


async def test_cancel_stops_a_fast_model_at_the_next_item(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With progress written at its real pace (every two seconds), a model
    that answers in well under that is still not asked again after Cancel."""
    monkeypatch.setattr(jobs, "PROGRESS_EVERY_S", 2.0)
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)

    async def cancel_during_the_second(n: int) -> None:
        if n == 2:
            async with factory() as db:
                entry = await db.get(ComparisonModel, entry_id)
                assert entry is not None
                await comparisons.cancel(db, entry, now=site.clock())
                await db.commit()

    wrap_ask(monkeypatch, cancel_during_the_second)
    await drain(factory, site.deps())
    assert [position for position, _a in await answers(factory, entry_id)] == [0, 1]
    assert len(site.server.requests) == 2
    entry = await entry_row(factory, entry_id)
    assert (entry.state, entry.message) == ("stopped", comparisons.CANCELLED)


async def test_try_again_drops_the_failures_only_when_its_turn_starts(
    factory: Factory, site: Site
) -> None:
    told: list[int] = []

    def answers_once_badly(_body: dict[str, Any]) -> str:
        told.append(1)
        # The first item gets prose twice (the question and its repair turn).
        return "It is a barn, I think." if len(told) <= 2 else look()

    fake.FakeModelServer.answer_for("LookAnswer", answers_once_badly)
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    await drain(factory, site.deps())
    first = await answers(factory, entry_id)
    assert [(p, found.ok) for p, found in first] == [(0, False), (1, True), (2, True), (3, True)]

    async def try_again() -> LocalAiJob:
        async with factory() as db:
            entry = await db.get(ComparisonModel, entry_id)
            assert entry is not None
            fresh = await comparisons.carry_on(
                db, entry, retry_failed=True, user_id=None, now=site.clock()
            )
            await db.commit()
            return fresh

    # Pressed, then cancelled before its turn: the failure is still there.
    await try_again()
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        await comparisons.cancel(db, entry, now=site.clock())
        await db.commit()
    await drain(factory, site.deps())
    assert [found.id for _p, found in await answers(factory, entry_id)] == [
        found.id for _p, found in first
    ]

    # Pressed and run: the failed item is asked again, the others are not.
    turn = await try_again()
    asked = len(site.server.requests)
    await drain(factory, site.deps())
    again = await answers(factory, entry_id)
    assert [(p, found.ok) for p, found in again] == [(0, True), (1, True), (2, True), (3, True)]
    assert [found.id for _p, found in again[1:]] == [found.id for _p, found in first[1:]]
    assert len(site.server.requests) == asked + 1
    async with factory() as db:
        ran = await db.get(LocalAiJob, turn.id)
    # Done once: a turn that stepped aside and came back would not drop again.
    assert ran is not None and comparisons.RETRY_FAILED not in ran.payload
    assert (await entry_row(factory, entry_id)).state == "done"


async def test_a_column_answered_to_the_end_but_left_open_closes_on_carry_on(
    factory: Factory, site: Site
) -> None:
    """The worker died after the last answer and before the column was closed."""
    model = await site.add(factory)
    _comparison, (entry_id,) = await make(factory, site, model)
    await drain(factory, site.deps())
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == entry.job_id)
            .values(state="failed", message=jobs.STOPPED)
        )
        entry.state, entry.message, entry.finished_at = "stopped", comparisons.STOPPED, None
        await db.commit()
    async with factory() as db:
        assert await comparisons.last_finished_for(db, model) is None
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        await comparisons.carry_on(db, entry, retry_failed=False, user_id=None, now=site.clock())
        await db.commit()
    await drain(factory, site.deps())
    entry = await entry_row(factory, entry_id)
    assert entry.state == "done" and entry.finished_at is not None
    assert len(site.server.requests) == 4  # nothing asked again
    async with factory() as db:
        assert await comparisons.last_finished_for(db, model) is not None


# --- the catalogue ---------------------------------------------------------------------------


async def test_nothing_is_written_to_the_catalogue(factory: Factory, site: Site) -> None:
    async with factory() as db:
        gelco = Library(name="GELCO", root_path=str(site.root / "gelco"))
        db.add(gelco)
        await db.flush()
        for asset_id, path, media in (
            (CLIP, "Brookside/2026/DJI_0042.MP4", "video"),
            (PHOTO, "Brookside/2026/Stills/IMG_7731.JPG", "image"),
        ):
            db.add(
                Asset(
                    id=asset_id,
                    library_id=gelco.id,
                    relative_path=path,
                    filename=path.rsplit("/", 1)[-1],
                    extension=path.rsplit(".", 1)[-1].lower(),
                    media_type=media,
                    size_bytes=1000,
                    mtime=datetime(2026, 6, 1, tzinfo=UTC),
                    availability="online",
                )
            )
        pond = Tag(name="pond", slug="pond")
        db.add(pond)
        await db.flush()
        db.add(AssetTag(asset_id=CLIP, tag_id=pond.id, source="confirmed"))
        await db.commit()

    async def catalogue() -> dict[str, Any]:
        async with factory() as db:
            counts = {
                table.__name__: await db.scalar(select(func.count()).select_from(table))
                for table in (Asset, AssetTag, Tag, Derivative)
            }
            assets = (await db.scalars(select(Asset).order_by(Asset.id))).all()
            rows = [
                {column.key: getattr(asset, column.key) for column in Asset.__table__.columns}
                for asset in assets
            ]
            return {"counts": counts, "assets": rows}

    before = await catalogue()
    fake.FakeModelServer.answer_for(
        "LookAnswer",
        lambda _body: look(
            matches=[{"tag": "drone", "confidence": 0.95}],
            new_tags=[{"tag": "silo", "confidence": 0.9}],
        ),
    )
    first = await site.add(factory, FIRST)
    second = await site.add(factory, SECOND)
    _comparison, entries = await make(factory, site, first, second)
    await drain(factory, site.deps())
    for entry_id in entries:
        assert (await entry_row(factory, entry_id)).state == "done"
    assert await catalogue() == before


def test_the_turn_is_a_job_the_lane_knows_and_it_runs_a_model() -> None:
    assert compare_job.KIND == comparisons.KIND == "compare_model"
    assert compare_job.KIND in jobs.registered()
    known = jobs._KINDS[compare_job.KIND]
    assert known.needs_model is True and known.abandoned is compare_job.abandoned
    nameless = LocalAiJob(kind=compare_job.KIND, subject="x:y", payload={})
    assert jobs.title_of(nameless) == "Comparing a model on a sample"
