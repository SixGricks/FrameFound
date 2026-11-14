"""Comparisons on the database (framefound/local_models/comparisons.py): the
rows, the columns' turns as a page reads them, and the figures.

No model is asked and no job is run: the lane's job rows are written by
`jobs.create` and put into each state by hand, as the runner would leave
them. The sample is built here (as `sampling.draw` would hand it over), on
file-backed SQLite.
"""

import dataclasses
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import (
    Comparison,
    ComparisonAnswer,
    ComparisonItem,
    ComparisonModel,
    LocalAiJob,
    LocalModel,
)
from framefound.local_models import agreement, comparisons, jobs, registry
from framefound.local_models.comparisons import BadRequest, CompareRefused
from framefound.local_models.sampling import Candidate, Drawn, SampleSpec
from framefound.local_models.settings_store import LocalModelsConfig
from framefound.looking import prompt

Factory = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 11, 14, 19, 2, 11, tzinfo=UTC)
KINDS = ("mlx-vlm",)


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _the_kind_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """The column's job kind, as `compare_job` registers it; nothing here runs it."""

    async def never_run(_ctx: jobs.JobContext, _job: LocalAiJob) -> dict[str, Any] | None:
        raise AssertionError("no job is run in these tests")

    monkeypatch.setitem(
        jobs._KINDS,
        comparisons.KIND,
        jobs.Kind(never_run, needs_model=True, title=lambda _job: "Comparing"),
    )


@pytest.fixture()
async def factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Factory]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'comparisons.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "comparisons-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# --- building blocks -------------------------------------------------------------------


def _model(
    repo: str = "mlx-community/demo-small-4bit",
    *,
    revision: str = "475b9088" + "0" * 32,
    variant: str = "",
    takes: tuple[str, ...] = ("text", "image"),
    state: str = registry.INSTALLED,
) -> LocalModel:
    publisher = repo.split("/")[0]
    return LocalModel(
        repo_id=repo,
        repo_key=repo.casefold(),
        revision=revision,
        variant=variant,
        publisher=publisher,
        chain=[
            {
                "repo_id": repo,
                "publisher": publisher,
                "bases": ["google/demo-small"],
                "found": True,
            },
            {"repo_id": "google/demo-small", "publisher": "google", "bases": [], "found": True},
        ],
        runtime="mlx-vlm",
        takes=list(takes),
        state=state,
        memory_gb=5.0,
    )


async def add_model(factory: Factory, **more: Any) -> LocalModel:
    async with factory() as db:
        model = _model(**more)
        db.add(model)
        await db.commit()
        return model


def _shot(
    clip: uuid.UUID,
    start: int,
    *,
    used: bool | None = None,
    yes: tuple[str, ...] = (),
    no: tuple[str, ...] = (),
) -> Candidate:
    return Candidate(
        kind="shot",
        asset_id=clip,
        library_name="GELCO",
        relative_path=f"Brookside/2026/{clip.hex[:6]}.MP4",
        filename=f"{clip.hex[:6]}.MP4",
        media_type="video",
        start_ms=start,
        end_ms=start + 4000,
        facts={"length_s": 4.0, "move": "orbit"},
        facts_line="4.0 seconds long; the camera orbits right.",
        truth_yes=tuple((slug, slug.replace("-", " ")) for slug in yes),
        truth_no=tuple((slug, slug.replace("-", " ")) for slug in no),
        used=used,
        used_in=("Brookside Quick 30",) if used else (),
        pictures=((start, Path("a.jpg")), (start + 2000, Path("b.jpg"))),
    )


def _photo(asset: uuid.UUID, *, yes: tuple[str, ...] = (), no: tuple[str, ...] = ()) -> Candidate:
    return Candidate(
        kind="photo",
        asset_id=asset,
        library_name="GELCO",
        relative_path=f"Brookside/2026/Stills/{asset.hex[:6]}.JPG",
        filename=f"{asset.hex[:6]}.JPG",
        media_type="image",
        start_ms=None,
        end_ms=None,
        facts={},
        facts_line="",
        truth_yes=tuple((slug, slug) for slug in yes),
        truth_no=tuple((slug, slug) for slug in no),
        used=None,
        used_in=(),
        pictures=((0, Path("p.jpg")),),
    )


CLIP_A = uuid.UUID(int=1)
CLIP_B = uuid.UUID(int=2)
PHOTO = uuid.UUID(int=3)
CUT = uuid.UUID(int=99)


def _drawn(items: tuple[Candidate, ...] | None = None, **spec: Any) -> Drawn:
    if items is None:
        items = (
            _shot(CLIP_A, 0, used=True, yes=("drone", "golf-course"), no=("sunset",)),
            _shot(CLIP_A, 4000, used=False, yes=("drone", "golf-course"), no=("sunset",)),
            _shot(CLIP_B, 0, used=False),
            _photo(PHOTO, yes=("barn",)),
        )
    filled = SampleSpec(kind="folder", what="both", size=60, stills=3, seed=1830221, **spec)
    return Drawn(
        label="GELCO › Brookside/2026",
        items=items,
        pool={"photos": 4, "shots": 12, "clips": 3},
        counted_cuts=((CUT, "Brookside Quick 30", "import", NOW),),
        left_out={"no_shots": 1, "over": 9},
        vocabulary=(("drone", 12), ("golf course", 7)),
        spec=filled,
    )


async def make(factory: Factory, drawn: Drawn | None = None, **more: Any) -> Comparison:
    asked = SampleSpec(kind="folder", library_id=uuid.UUID(int=7), folder="Brookside/2026")
    async with factory() as db:
        comparison = await comparisons.create(
            db, asked, drawn or _drawn(), name=more.pop("name", "Brookside"), user_id=None, **more
        )
        await db.commit()
        return comparison


async def add(factory: Factory, comparison: Comparison, *models: LocalModel) -> list[uuid.UUID]:
    async with factory() as db:
        fresh = await db.get(Comparison, comparison.id)
        assert fresh is not None
        made = await comparisons.add_models(db, fresh, models, user_id=None)
        await db.commit()
        return [entry.id for entry in made]


async def entry_of(factory: Factory, entry_id: uuid.UUID) -> ComparisonModel:
    async with factory() as db:
        found = await db.get(ComparisonModel, entry_id)
        assert found is not None
        return found


async def job_of(factory: Factory, entry_id: uuid.UUID) -> LocalAiJob | None:
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None and entry.job_id is not None
        return await db.get(LocalAiJob, entry.job_id)


async def set_job(factory: Factory, entry_id: uuid.UUID, **values: Any) -> None:
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        await db.execute(update(LocalAiJob).where(LocalAiJob.id == entry.job_id).values(**values))
        await db.commit()


async def set_entry(factory: Factory, entry_id: uuid.UUID, **values: Any) -> None:
    async with factory() as db:
        await db.execute(
            update(ComparisonModel).where(ComparisonModel.id == entry_id).values(**values)
        )
        await db.commit()


async def answer(
    factory: Factory,
    entry_id: uuid.UUID,
    position: int,
    *,
    ok: bool = True,
    tags: tuple[tuple[str, float, bool], ...] = (),
    score: int | None = 50,
    seconds: float | None = 4.0,
    problem: str | None = None,
) -> None:
    """One stored answer, as the job would store it."""
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        item = await db.scalar(
            select(ComparisonItem).where(
                ComparisonItem.comparison_id == entry.comparison_id,
                ComparisonItem.position == position,
            )
        )
        assert item is not None
        db.add(
            ComparisonAnswer(
                entry_id=entry_id,
                item_id=item.id,
                ok=ok,
                problem=None if ok else (problem or "bad_answer"),
                tags=[
                    {"name": slug, "slug": slug, "confidence": conf, "in_vocabulary": known}
                    for slug, conf, known in tags
                ],
                score=score if ok else None,
                seconds=seconds if ok else None,
            )
        )
        if ok:
            entry.done += 1
        else:
            entry.failed += 1
        await db.commit()


async def finish(factory: Factory, entry_id: uuid.UUID, at: datetime = NOW) -> None:
    """The column closed as the handler closes it, and its job ended."""
    await set_entry(factory, entry_id, state="done", finished_at=at)
    await set_job(factory, entry_id, state="done", finished_at=at)


async def table_count(factory: Factory, table: type[Any]) -> int:
    async with factory() as db:
        return int(await db.scalar(select(func.count()).select_from(table)) or 0)


# --- may this model be compared ---------------------------------------------------------


def test_a_model_that_cannot_be_compared_is_named_in_the_refusal() -> None:
    config = LocalModelsConfig()
    reads = _model()
    text_only = _model("mlx-community/gemma-4-12B-it-OptiQ-4bit", takes=("text",))
    downloading = _model("mlx-community/demo-large-4bit", state=registry.INCOMPLETE)
    assert comparisons.may_compare(reads, config, KINDS) is None
    assert comparisons.compare_problem(text_only, config, KINDS) == "It does not read pictures."
    assert (
        comparisons.may_compare(text_only, config, KINDS)
        == "gemma-4-12B-it-OptiQ-4bit does not read pictures."
    )
    assert comparisons.may_compare(downloading, config, KINDS) == (
        "demo-large-4bit's download did not finish. Resume the download first."
    )
    assert comparisons.may_compare(reads, config, ()) == (
        "demo-small-4bit needs a program this install does not have."
    )


# --- making a comparison ------------------------------------------------------------------


async def test_a_comparison_records_what_was_drawn_with_its_truth_as_it_was(
    factory: Factory,
) -> None:
    comparison = await make(factory, now=NOW)
    async with factory() as db:
        saved = await db.get(Comparison, comparison.id)
        assert saved is not None
        items = await comparisons.items_of(db, comparison.id)
    # What was drawn, every value filled in — not the request, which left them unsaid.
    assert (saved.what, saved.size, saved.stills, saved.seed) == ("both", 4, 3, 1830221)
    assert saved.source == {
        "kind": "folder",
        "library_id": str(uuid.UUID(int=7)),
        "folder": "Brookside/2026",
        "truth_cut_ids": None,
    }
    assert saved.prompt_version == prompt.PROMPT_VERSION
    assert saved.vocabulary == [["drone", 12], ["golf course", 7]]
    assert saved.counted_cuts == [
        {"id": str(CUT), "title": "Brookside Quick 30", "source": "import"}
    ]
    # Tags are decided per piece of footage: the clip's two shots count once.
    assert saved.counts == {
        "photos": 1,
        "shots": 3,
        "clips": 2,
        "used": 1,
        "unused": 2,
        "footage_with_tags": 2,
        "yes": 3,
        "no": 1,
        "pool": {"photos": 4, "shots": 12, "clips": 3},
        "left_out": {"no_shots": 1, "over": 9},
    }
    assert jobs._stored(saved.truth_at) == NOW
    assert [item.position for item in items] == [0, 1, 2, 3]
    first = items[0]
    assert (first.kind, first.asset_id, first.start_ms, first.end_ms) == ("shot", CLIP_A, 0, 4000)
    assert first.facts_line == "4.0 seconds long; the camera orbits right."
    assert first.picture_ms == [0, 2000]
    assert first.truth_yes == [
        {"slug": "drone", "name": "drone"},
        {"slug": "golf-course", "name": "golf course"},
    ]
    assert first.truth_no == [{"slug": "sunset", "name": "sunset"}]
    assert (first.used, first.used_in) == (True, ["Brookside Quick 30"])
    assert (items[3].kind, items[3].used, items[3].facts_line) == ("photo", None, "")


async def test_without_a_name_it_is_called_after_the_sample_and_the_day(factory: Factory) -> None:
    comparison = await make(factory, name=None, now=NOW)
    assert comparison.name == "GELCO › Brookside/2026, 14 Nov 2026"
    long_label = dataclasses.replace(_drawn(), label="x" * 300)
    named = await make(factory, long_label, name="   ", now=NOW)
    assert len(named.name) <= comparisons.NAME_CHARS
    assert named.name.endswith("…, 14 Nov 2026")
    with pytest.raises(BadRequest, match="Keep the name under 200 characters."):
        await make(factory, name="y" * 201)


async def test_the_pictures_actually_frozen_are_what_an_item_records(factory: Factory) -> None:
    comparison = await make(factory)
    async with factory() as db:
        # The second picture of the first shot would not decode; the photograph's did.
        await comparisons.set_pictures(db, comparison, [(0,), (4000, 6000), (), (0,)])
        await db.commit()
        items = await comparisons.items_of(db, comparison.id)
    assert [item.picture_ms for item in items] == [[0], [4000, 6000], [], [0]]


async def test_each_model_gets_a_column_and_a_queued_job_in_the_order_given(
    factory: Factory,
) -> None:
    comparison = await make(factory)
    first = await add_model(factory)
    second = await add_model(factory, repo="mlx-community/demo-other-4bit")
    ids = await add(factory, comparison, first, second)
    async with factory() as db:
        entries = await comparisons.entries_of(db, comparison.id)
        found = await comparisons.jobs_of(db, entries)
    assert [entry.id for entry in entries] == ids
    assert [entry.model_name for entry in entries] == ["demo-small-4bit", "demo-other-4bit"]
    entry = entries[0]
    assert (entry.model, entry.model_revision, entry.variant) == (
        first.repo_id,
        first.revision,
        "",
    )
    assert (entry.state, entry.prompt_version) == ("queued", prompt.PROMPT_VERSION)
    job = found[entry.id]
    assert entry.job_id == job.id
    assert (job.kind, job.state, job.priority, job.model_id) == (
        "compare_model",
        "queued",
        5,
        first.id,
    )
    assert job.subject == f"{comparison.id}:{entry.id}"
    assert job.payload == {
        "comparison_id": str(comparison.id),
        "entry_id": str(entry.id),
        "model_name": "demo-small-4bit",
        "comparison_name": "Brookside",
    }


async def test_the_same_build_cannot_be_added_to_a_comparison_twice(factory: Factory) -> None:
    comparison = await make(factory)
    model = await add_model(factory)
    await add(factory, comparison, model)
    refusal = "demo-small-4bit (build 475b908) is already in this comparison."
    with pytest.raises(CompareRefused, match=rf"^{re.escape(refusal)}$"):
        await add(factory, comparison, model)
    # Nor twice in one request, to a comparison that does not have it yet. It is
    # refused before anything is written, so the request's session is still good
    # for the next thing it does.
    other = await make(factory)
    second = await add_model(factory, repo="mlx-community/demo-other-4bit")
    async with factory() as db:
        fresh = await db.get(Comparison, other.id)
        assert fresh is not None
        with pytest.raises(CompareRefused, match="is already in this comparison"):
            await comparisons.add_models(db, fresh, [second, model, model], user_id=None)
        await comparisons.add_models(db, fresh, [second], user_id=None)
        await db.commit()
    assert await table_count(factory, ComparisonModel) == 2
    # Two builds of one repository at one commit are two columns.
    q4 = await add_model(factory, repo="mlx-community/demo-gguf", variant="Q4_K_M")
    q8 = await add_model(factory, repo="mlx-community/demo-gguf", variant="Q8_0")
    await add(factory, comparison, q4, q8)
    assert await table_count(factory, ComparisonModel) == 4


async def test_a_seventh_model_is_refused(factory: Factory) -> None:
    comparison = await make(factory)
    models = [await add_model(factory, repo=f"mlx-community/demo-{n}-4bit") for n in range(7)]
    await add(factory, comparison, *models[:4])
    with pytest.raises(CompareRefused, match=r"^A comparison holds at most six models\.$"):
        await add(factory, comparison, *models[4:])
    assert await table_count(factory, ComparisonModel) == 4
    await add(factory, comparison, *models[4:6])
    assert await table_count(factory, ComparisonModel) == 6


async def test_a_comparison_made_with_an_older_question_cannot_be_added_to(
    factory: Factory,
) -> None:
    comparison = await make(factory)
    model = await add_model(factory)
    [entry_id] = await add(factory, comparison, model)
    await set_job(factory, entry_id, state="failed", message="It would not load.")
    await set_entry(factory, entry_id, state="failed", message="It would not load.")
    async with factory() as db:
        await db.execute(
            update(Comparison).where(Comparison.id == comparison.id).values(prompt_version="look-0")
        )
        await db.commit()
    sentence = (
        "Made with an earlier version of the question (look-0). It can be read but not added to."
    )
    other = await add_model(factory, repo="mlx-community/demo-other-4bit")
    with pytest.raises(CompareRefused) as added:
        await add(factory, comparison, other)
    assert str(added.value) == sentence
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        with pytest.raises(CompareRefused) as carried:
            await comparisons.carry_on(db, entry, retry_failed=False, user_id=None)
    assert str(carried.value) == sentence


# --- how a column reads ---------------------------------------------------------------------


def _entry(state: str = "queued", message: str = "") -> ComparisonModel:
    return ComparisonModel(state=state, message=message, not_before=None)


def _job(state: str, *, message: str = "", beat: datetime | None = NOW, **more: Any) -> LocalAiJob:
    return LocalAiJob(
        state=state, message=message, beat_at=beat, started_at=beat, cancel_requested=False, **more
    )


LATER = NOW + timedelta(hours=2, minutes=28)
WINDOW = (
    "Waiting for the overnight window (starts 21:30): it needs about 10 GB and the "
    "daytime budget is 8 GB."
)


@pytest.mark.parametrize(
    ("entry", "job", "expected"),
    [
        (_entry("queued"), _job("queued"), ("queued", "Waiting its turn.", None)),
        (
            _entry("queued", "Stepped aside for something more urgent. It carries on by itself."),
            _job("queued"),
            (
                "queued",
                "Stepped aside for something more urgent. It carries on by itself.",
                None,
            ),
        ),
        (
            _entry("waiting"),
            _job("waiting", message=WINDOW, not_before=LATER.replace(tzinfo=None)),
            ("waiting", WINDOW, LATER),
        ),
        # Released after a wait (Resume, the window opening) while another
        # turn holds the lane: the wait's sentence is not said any more.
        (
            _entry("waiting", "Local AI is paused."),
            _job("queued"),
            ("queued", "Waiting its turn.", None),
        ),
        (_entry("waiting", WINDOW), _job("queued"), ("queued", "Waiting its turn.", None)),
        (_entry("queued"), _job("running"), ("running", "", None)),
        (_entry("running"), _job("running"), ("running", "", None)),
        (
            _entry("running"),
            _job("running", beat=NOW - timedelta(minutes=5)),
            ("stopped", comparisons.STOPPED, None),
        ),
        (
            _entry("running"),
            _job("cancelled", message="Cancelled."),
            ("stopped", comparisons.CANCELLED, None),
        ),
        (
            _entry("running"),
            _job("failed", message=jobs.STOPPED),
            ("stopped", comparisons.STOPPED, None),
        ),
        (
            _entry("waiting"),
            _job("failed", message="Local AI models run on the Mac install of FrameFound."),
            ("failed", "Local AI models run on the Mac install of FrameFound.", None),
        ),
        (_entry("running"), _job("done"), ("stopped", comparisons.LOST, None)),
        (_entry("running"), None, ("stopped", comparisons.LOST, None)),
        (_entry("done"), _job("running"), ("done", "", None)),
        (
            _entry("stopped", "It failed three items in a row."),
            None,
            ("stopped", "It failed three items in a row.", None),
        ),
        (
            _entry("failed", "It would not load."),
            _job("failed"),
            ("failed", "It would not load.", None),
        ),
    ],
    ids=[
        "queued",
        "stepped-aside",
        "waiting",
        "released-after-pause",
        "released-at-window",
        "just-claimed",
        "running",
        "worker-died",
        "cancelled",
        "reaped",
        "failed",
        "handler-left-it-open",
        "job-row-gone",
        "done",
        "stopped",
        "failed-column",
    ],
)
def test_a_column_reads_as_its_job_says_while_it_is_open(
    entry: ComparisonModel,
    job: LocalAiJob | None,
    expected: tuple[str, str, datetime | None],
) -> None:
    assert comparisons.shown(entry, job, NOW) == expected


async def test_a_column_whose_worker_died_reads_as_stopped_and_can_carry_on(
    factory: Factory,
) -> None:
    comparison = await make(factory)
    model = await add_model(factory)
    [entry_id] = await add(factory, comparison, model)
    now = datetime.now(UTC)
    await set_job(factory, entry_id, state="running", beat_at=now - timedelta(minutes=5))
    await set_entry(factory, entry_id, state="running")
    await answer(factory, entry_id, 0)
    dead = await job_of(factory, entry_id)
    assert dead is not None
    assert comparisons.shown(await entry_of(factory, entry_id), dead, now)[:2] == (
        "stopped",
        comparisons.STOPPED,
    )
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        fresh = await comparisons.carry_on(db, entry, retry_failed=False, user_id=None, now=now)
        await db.commit()
    # The dead row is ended the way the tick ends it; the new turn is a new row.
    async with factory() as db:
        old = await db.get(LocalAiJob, dead.id)
    assert old is not None and (old.state, old.message) == ("failed", jobs.STOPPED)
    entry = await entry_of(factory, entry_id)
    assert (entry.state, entry.job_id, fresh.state) == ("queued", fresh.id, "queued")
    assert entry.done == 1
    async with factory() as db:
        left = await comparisons.unanswered(db, entry)
    assert [item.position for item in left] == [1, 2, 3]


async def test_a_column_whose_job_row_is_gone_reads_as_stopped(factory: Factory) -> None:
    comparison = await make(factory)
    [entry_id] = await add(factory, comparison, await add_model(factory))
    await set_entry(factory, entry_id, state="running")
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        await db.execute(sa_delete(LocalAiJob))
        await db.commit()
        found = await comparisons.jobs_of(db, [entry])
    assert found == {}
    assert comparisons.shown(entry, None) == ("stopped", comparisons.LOST, None)
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        fresh = await comparisons.carry_on(db, entry, retry_failed=False, user_id=None)
        await db.commit()
    assert fresh.state == "queued"


async def test_a_waiting_column_says_until_when(factory: Factory) -> None:
    comparison = await make(factory)
    [entry_id] = await add(factory, comparison, await add_model(factory))
    until = datetime(2026, 11, 14, 21, 30, tzinfo=UTC)
    await set_job(factory, entry_id, state="waiting", message=WINDOW, not_before=until)
    await set_entry(factory, entry_id, state="waiting", message=WINDOW)
    entry = await entry_of(factory, entry_id)
    job = await job_of(factory, entry_id)
    # SQLite hands the time back without its zone; it is read as the UTC it was.
    assert comparisons.shown(entry, job, NOW) == ("waiting", WINDOW, until)


# --- changing a column -------------------------------------------------------------------------


async def test_cancel_ends_a_turn_not_yet_running_at_once(factory: Factory) -> None:
    comparison = await make(factory)
    [entry_id] = await add(factory, comparison, await add_model(factory))
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        await comparisons.cancel(db, entry)
        await db.commit()
    entry = await entry_of(factory, entry_id)
    job = await job_of(factory, entry_id)
    assert job is not None and job.state == "cancelled"
    assert (entry.state, entry.message) == ("stopped", comparisons.CANCELLED)
    assert comparisons.shown(entry, job)[:2] == ("stopped", comparisons.CANCELLED)
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        with pytest.raises(CompareRefused, match=r"^It is not running\.$"):
            await comparisons.cancel(db, entry)


async def test_cancel_asks_a_running_turn_to_stop_between_items(factory: Factory) -> None:
    comparison = await make(factory)
    [entry_id] = await add(factory, comparison, await add_model(factory))
    now = datetime.now(UTC)
    await set_job(factory, entry_id, state="running", beat_at=now)
    await set_entry(factory, entry_id, state="running")
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        await comparisons.cancel(db, entry, now)
        await db.commit()
    entry = await entry_of(factory, entry_id)
    job = await job_of(factory, entry_id)
    assert job is not None and (job.state, job.cancel_requested) == ("running", True)
    # Still running until the handler notices; the page says Cancel was pressed.
    assert entry.state == "running"
    assert comparisons.cancelling(job)


async def test_a_running_column_cannot_be_removed_or_deleted(factory: Factory) -> None:
    comparison = await make(factory)
    [entry_id] = await add(factory, comparison, await add_model(factory))
    now = datetime.now(UTC)
    await set_job(factory, entry_id, state="running", beat_at=now)
    await set_entry(factory, entry_id, state="running")
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        saved = await db.get(Comparison, comparison.id)
        assert entry is not None and saved is not None
        with pytest.raises(CompareRefused, match=r"^It is still running\. Cancel it first\.$"):
            await comparisons.remove_entry(db, entry, now)
        with pytest.raises(CompareRefused, match=r"^A model is still running\. Cancel it first\.$"):
            await comparisons.delete(db, saved, now)
        with pytest.raises(CompareRefused, match=r"^It is already running\.$"):
            await comparisons.carry_on(db, entry, retry_failed=False, user_id=None, now=now)
    # A queued turn is active too.
    other = await make(factory)
    await add(factory, other, await add_model(factory, repo="mlx-community/demo-other-4bit"))
    async with factory() as db:
        saved = await db.get(Comparison, other.id)
        assert saved is not None
        with pytest.raises(CompareRefused, match="A model is still running"):
            await comparisons.delete(db, saved)
    assert await table_count(factory, Comparison) == 2


async def test_removing_the_chosen_column_clears_the_decision(factory: Factory) -> None:
    comparison = await make(factory)
    chosen_id, other_id = await add(
        factory,
        comparison,
        await add_model(factory),
        await add_model(factory, repo="mlx-community/demo-other-4bit"),
    )
    for entry_id in (chosen_id, other_id):
        await answer(factory, entry_id, 0)
        await finish(factory, entry_id)
    async with factory() as db:
        saved = await db.get(Comparison, comparison.id)
        assert saved is not None
        await comparisons.decide(
            db, saved, verdict="chosen", entry_id=chosen_id, note="Sharper captions.", user_id=None
        )
        await db.commit()
        other = await db.get(ComparisonModel, other_id)
        assert other is not None
        await comparisons.remove_entry(db, other)
        await db.commit()
        await db.refresh(saved)
        assert (saved.verdict, saved.verdict_entry_id) == ("chosen", chosen_id)
        chosen = await db.get(ComparisonModel, chosen_id)
        assert chosen is not None
        await comparisons.remove_entry(db, chosen)
        await db.commit()
        await db.refresh(saved)
    assert (saved.verdict, saved.verdict_entry_id, saved.verdict_at) == ("", None, None)
    assert saved.verdict_note == "Sharper captions."
    assert await table_count(factory, ComparisonModel) == 0
    assert await table_count(factory, ComparisonAnswer) == 0
    # The job rows are history and stay.
    assert await table_count(factory, LocalAiJob) == 2


async def test_carry_on_starts_where_it_stopped_and_try_again_asks_the_failures_again(
    factory: Factory,
) -> None:
    comparison = await make(factory)
    [entry_id] = await add(factory, comparison, await add_model(factory))
    await answer(factory, entry_id, 0)
    await answer(factory, entry_id, 1, ok=False, problem="bad_answer")
    await set_job(factory, entry_id, state="failed", message="It failed three items in a row.")
    await set_entry(factory, entry_id, state="stopped", message="It failed three items in a row.")
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        await comparisons.carry_on(db, entry, retry_failed=False, user_id=None)
        await db.commit()
        assert [i.position for i in await comparisons.unanswered(db, entry)] == [2, 3]
        assert (entry.done, entry.failed, entry.message) == (1, 1, "")
    # It answers the rest; then only the failed one is left to try again.
    for position in (2, 3):
        await answer(factory, entry_id, position)
    await finish(factory, entry_id)
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        with pytest.raises(CompareRefused, match=r"^It has answered everything\.$"):
            await comparisons.carry_on(db, entry, retry_failed=False, user_id=None)
        fresh = await comparisons.carry_on(db, entry, retry_failed=True, user_id=None)
        await db.commit()
        assert (entry.state, entry.done, entry.failed, entry.finished_at) == ("queued", 3, 1, None)
        # The failure is deleted by the turn when it starts, not here: a queue
        # that turns out to be down must leave it as it was.
        assert fresh.payload[comparisons.RETRY_FAILED] is True
    assert await table_count(factory, ComparisonAnswer) == 4


async def test_a_column_answered_to_the_end_but_never_closed_can_be_carried_on(
    factory: Factory,
) -> None:
    """Its worker died between the last answer and closing the column: Carry
    on is its way out (a turn that asks nothing and closes it), not Remove."""
    comparison = await make(factory)
    [entry_id] = await add(factory, comparison, await add_model(factory))
    for position in range(4):
        await answer(factory, entry_id, position)
    await set_job(factory, entry_id, state="failed", message=jobs.STOPPED)
    await set_entry(factory, entry_id, state="stopped", message=comparisons.STOPPED)
    async with factory() as db:
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None
        fresh = await comparisons.carry_on(db, entry, retry_failed=False, user_id=None)
        await db.commit()
        assert (entry.state, entry.job_id) == ("queued", fresh.id)
        assert comparisons.RETRY_FAILED not in fresh.payload


async def test_a_removed_model_cannot_be_carried_on(factory: Factory) -> None:
    comparison = await make(factory)
    model = await add_model(factory)
    [entry_id] = await add(factory, comparison, model)
    await set_job(factory, entry_id, state="cancelled")
    await set_entry(factory, entry_id, state="stopped", message=comparisons.CANCELLED)
    async with factory() as db:
        # SQLite keeps the dangling id: the column reads the model as gone all the same.
        await db.execute(sa_delete(LocalModel))
        await db.commit()
        entry = await db.get(ComparisonModel, entry_id)
        assert entry is not None and entry.model_id == model.id
        with pytest.raises(CompareRefused, match=r"^This model is no longer installed\.$"):
            await comparisons.carry_on(db, entry, retry_failed=False, user_id=None)


async def test_a_decision_says_which_model_and_is_only_a_record(factory: Factory) -> None:
    comparison = await make(factory)
    [entry_id] = await add(factory, comparison, await add_model(factory))
    stranger = await make(factory)
    [foreign_id] = await add(factory, stranger, await add_model(factory, repo="mlx-community/x"))
    user = uuid.UUID(int=5)
    async with factory() as db:
        saved = await db.get(Comparison, comparison.id)
        assert saved is not None
        with pytest.raises(BadRequest, match=r"^Say which model\.$"):
            await comparisons.decide(
                db, saved, verdict="chosen", entry_id=None, note="", user_id=user
            )
        with pytest.raises(BadRequest, match=r"^That model is not in this comparison\.$"):
            await comparisons.decide(
                db, saved, verdict="chosen", entry_id=foreign_id, note="", user_id=user
            )
        with pytest.raises(BadRequest):
            await comparisons.decide(
                db, saved, verdict="best", entry_id=None, note="", user_id=user
            )
        await comparisons.decide(
            db,
            saved,
            verdict="chosen",
            entry_id=entry_id,
            note="  Keeps to the form. ",
            user_id=user,
            now=NOW,
        )
        assert (saved.verdict, saved.verdict_entry_id, saved.verdict_note) == (
            "chosen",
            entry_id,
            "Keeps to the form.",
        )
        assert (saved.verdict_at, saved.verdict_by) == (NOW, user)
        # "None is good enough yet" names no column.
        await comparisons.decide(
            db, saved, verdict="none", entry_id=entry_id, note="", user_id=user
        )
        assert (saved.verdict, saved.verdict_entry_id) == ("none", None)
        await comparisons.decide(db, saved, verdict="", entry_id=None, note="", user_id=user)
        assert (saved.verdict, saved.verdict_at, saved.verdict_by) == ("", None, None)
        with pytest.raises(BadRequest, match=r"^Give it a name\.$"):
            await comparisons.rename(db, saved, "  ")
        await comparisons.rename(db, saved, " Brookside   Rev 2 shots ")
        assert saved.name == "Brookside Rev 2 shots"
        await db.commit()
    # No role was given by any of it.
    async with factory() as db:
        given = await registry.roles(db)
        assert [row.model_id for row in given.values()] == [None, None]
        assert [row.pending_model_id for row in given.values()] == [None, None]


async def test_deleting_a_comparison_removes_its_rows_and_nobody_elses(factory: Factory) -> None:
    keep = await make(factory)
    gone = await make(factory)
    model = await add_model(factory)
    [kept_entry] = await add(factory, keep, model)
    [gone_entry] = await add(factory, gone, model)
    for entry_id in (kept_entry, gone_entry):
        await answer(factory, entry_id, 0)
        await finish(factory, entry_id)
    async with factory() as db:
        saved = await db.get(Comparison, gone.id)
        assert saved is not None
        await comparisons.delete(db, saved)
        await db.commit()
    assert await table_count(factory, Comparison) == 1
    assert await table_count(factory, ComparisonItem) == 4
    assert await table_count(factory, ComparisonModel) == 1
    assert await table_count(factory, ComparisonAnswer) == 1
    assert await table_count(factory, LocalAiJob) == 2


# --- the figures ---------------------------------------------------------------------------------


async def test_figures_are_counted_over_what_every_finished_model_answered(
    factory: Factory,
) -> None:
    comparison = await make(factory)
    first, second, third = await add(
        factory,
        comparison,
        await add_model(factory),
        await add_model(factory, repo="mlx-community/demo-other-4bit"),
        await add_model(factory, repo="mlx-community/demo-third-4bit"),
    )
    # Positions: 0 used shot of clip A, 1 unused shot of A, 2 unused shot of B, 3 the photograph.
    await answer(factory, first, 0, tags=(("drone", 0.9, True), ("bunker", 0.5, False)), score=80)
    await answer(factory, first, 1, tags=(("sunset", 0.7, False),), score=40)
    await answer(factory, first, 2, score=80, seconds=6.0)
    await answer(factory, first, 3, tags=(("barn", 0.95, True),), score=None, seconds=2.0)
    await finish(factory, first)
    # The second failed the unused shot of clip A: no model is counted on it.
    await answer(factory, second, 0, tags=(("golf-course", 0.8, True),), score=60)
    await answer(factory, second, 1, ok=False, problem="timeout")
    await answer(factory, second, 2, score=60)
    await answer(factory, second, 3, score=None)
    await finish(factory, second)
    # The third is still running, and has not answered item 3: it changes nothing.
    await answer(factory, third, 0, score=10)
    async with factory() as db:
        saved = await db.get(Comparison, comparison.id)
        assert saved is not None
        figures = await comparisons.measures(db, saved)

    mine = figures[first]
    assert mine.over == 3
    assert mine.form == agreement.Form(asked=4, answered=4, second_tries=0, failed={})
    assert mine.shots == agreement.Pace(answered=3, median_s=4.0, slowest_s=6.0, per_hour=771)
    assert mine.photos.answered == 1
    assert mine.tags is not None and len(mine.tags) == len(agreement.FLOORS)
    everything = mine.tags[0]
    # Clip A's truth is drone, golf course / not sunset; its sunset proposal was on
    # the shot nobody is counted on, so it is not held against it.
    assert (everything.footage, everything.yes_total, everything.yes_found) == (2, 3, 2)
    assert (everything.no_total, everything.no_repeated) == (1, 0)
    assert (everything.proposed, everything.unjudged, everything.unjudged_new) == (3, 1, 1)
    assert mine.tags[1].proposed == 2  # 0.6 and above: bunker drops out
    # One used shot against the one unused shot still counted: scored the same.
    assert mine.scores == agreement.ScoreAgreement(
        used=1, unused=1, pairs=1, higher=0, lower=0, same=1, scored=2, distinct=1, few=True
    )

    theirs = figures[second]
    assert theirs.over == 3
    assert theirs.form.failed == {"timeout": 1}
    assert theirs.tags is not None and theirs.tags[0].yes_found == 1

    running = figures[third]
    assert (running.tags, running.scores) == (None, None)
    assert running.shots.answered == 1
    async with factory() as db:
        entries = await comparisons.entries_of(db, comparison.id)
        answers = await comparisons.answers_of(db, [e.id for e in entries])
        items = await comparisons.items_of(db, comparison.id)
    counted = comparisons.over_items(entries, answers)
    assert counted == {str(items[p].id) for p in (0, 2, 3)}


async def test_a_sample_with_no_truth_has_no_tag_or_score_figures(factory: Factory) -> None:
    bare = _drawn((_shot(CLIP_A, 0), _shot(CLIP_B, 0), _photo(PHOTO)))
    comparison = await make(factory, bare)
    [entry_id] = await add(factory, comparison, await add_model(factory))
    for position in range(3):
        await answer(factory, entry_id, position, tags=(("drone", 0.9, True),))
    await finish(factory, entry_id)
    async with factory() as db:
        saved = await db.get(Comparison, comparison.id)
        assert saved is not None
        figures = (await comparisons.measures(db, saved))[entry_id]
    assert (figures.over, figures.tags, figures.scores) == (3, None, None)
    assert figures.form.answered == 3


def test_a_figure_is_a_count_never_a_grade() -> None:
    words = {"share", "rate", "rank", "ranking", "grade", "best", "quality", "percent", "winner"}
    for name in (field.name for field in dataclasses.fields(comparisons.Measures)):
        assert not set(name.split("_")) & words, name


async def test_a_model_has_not_been_compared_until_a_column_of_its_finishes(
    factory: Factory,
) -> None:
    model = await add_model(factory)
    first = await make(factory)
    [entry_id] = await add(factory, first, model)
    async with factory() as db:
        assert await comparisons.last_finished_for(db, model.id) is None
    await answer(factory, entry_id, 0)
    async with factory() as db:
        # Answering is not finishing.
        assert await comparisons.last_finished_for(db, model.id) is None
    # A column closed `done` with nothing stored has not been through anything.
    empty = await make(factory)
    [empty_id] = await add(factory, empty, model)
    await finish(factory, empty_id, NOW + timedelta(days=2))
    async with factory() as db:
        assert await comparisons.last_finished_for(db, model.id) is None
    # Nor has one that was never shown a picture (the pictures were gone).
    unshown = await make(factory)
    [unshown_id] = await add(factory, unshown, model)
    for position in range(4):
        await answer(factory, unshown_id, position, ok=False, problem=comparisons.UNSHOWN)
    await finish(factory, unshown_id, NOW + timedelta(days=3))
    async with factory() as db:
        assert await comparisons.last_finished_for(db, model.id) is None
    await finish(factory, entry_id, NOW)
    second = await make(factory)
    [later_id] = await add(factory, second, model)
    await answer(factory, later_id, 0, ok=False)
    await finish(factory, later_id, NOW + timedelta(days=1))
    async with factory() as db:
        assert await comparisons.last_finished_for(db, model.id) == NOW + timedelta(days=1)
        assert await comparisons.last_finished_for(db, uuid.uuid4()) is None


async def test_the_builder_hints_how_long_an_item_takes(factory: Factory) -> None:
    model = await add_model(factory)
    async with factory() as db:
        assert await comparisons.seconds_hint(db, model.id) is None
        # A Test that finished, as the lane leaves one; an older one is not the hint.
        for seconds, at in ((12.0, NOW - timedelta(days=1)), (9.5, NOW)):
            db.add(
                LocalAiJob(
                    kind="test",
                    subject=f"test {seconds}",
                    model_id=model.id,
                    state="done",
                    seconds=seconds,
                    finished_at=at,
                )
            )
        await db.commit()
        assert await comparisons.seconds_hint(db, model.id) == 9.5
    comparison = await make(factory)
    [entry_id] = await add(factory, comparison, model)
    for position, seconds in enumerate((3.0, 5.0, 4.0)):
        await answer(factory, entry_id, position, seconds=seconds)
    await answer(factory, entry_id, 3, ok=False)
    await finish(factory, entry_id)
    async with factory() as db:
        assert await comparisons.seconds_hint(db, model.id) == 4.0


def test_a_comparisons_state_on_the_list_comes_from_its_columns() -> None:
    assert comparisons.overall([]) == "empty"
    assert comparisons.overall(["done", "queued"]) == "running"
    assert comparisons.overall(["done", "running", "waiting"]) == "running"
    assert comparisons.overall(["done", "waiting", "failed"]) == "waiting"
    assert comparisons.overall(["done", "stopped"]) == "stopped"
    assert comparisons.overall(["done", "failed"]) == "stopped"
    assert comparisons.overall(["done", "done"]) == "finished"
