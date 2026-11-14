"""The Compare question (framefound/local_models/gate.py; design L6 D11, 3.4).

On file-backed SQLite. L2's Compare record is real rows read through
`comparisons.last_finished_for` where that matters, and stood in for by
`gate._compared` where it does not; a Test is a `local_ai_jobs` row left as
the lane leaves one. No model is asked and no job is run.
"""

import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import framefound.local_models as local_models
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
from framefound.local_models import comparisons, gate, registry
from framefound.local_models.kinds import LlamaCpp, MlxVlm, RuntimeKind

Factory = async_sessionmaker[AsyncSession]
KINDS: dict[str, RuntimeKind] = {"mlx-vlm": MlxVlm(), "llama-cpp": LlamaCpp()}
NOW = datetime(2026, 11, 13, 21, 40, tzinfo=UTC)
GGUF = "google/demo-vision-gguf"


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
async def factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Factory]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'gate.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "gate-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def add_model(
    factory: Factory,
    repo: str = GGUF,
    *,
    runtime: str = "llama-cpp",
    takes: tuple[str, ...] = ("text", "image"),
) -> LocalModel:
    publisher = repo.split("/")[0]
    async with factory() as db:
        model = LocalModel(
            repo_id=repo,
            repo_key=repo.casefold(),
            revision="7b" * 20,
            variant="Q4_K_M" if runtime == "llama-cpp" else "",
            publisher=publisher,
            chain=[{"repo_id": repo, "publisher": publisher, "bases": [], "found": True}],
            runtime=runtime,
            takes=list(takes),
            state=registry.INSTALLED,
            memory_gb=4.0,
        )
        db.add(model)
        await db.commit()
        return model


async def compared(factory: Factory, model: LocalModel, finished: datetime) -> None:
    """A Compare column of this model that finished with one answer, as L2 leaves it."""
    async with factory() as db:
        comparison = Comparison(
            name="Kitchen stills", source_kind="library", prompt_version="look-1"
        )
        db.add(comparison)
        await db.flush()
        item = ComparisonItem(comparison_id=comparison.id, position=0, kind="photo")
        column = ComparisonModel(
            comparison_id=comparison.id,
            model_id=model.id,
            model=model.repo_id,
            model_revision=model.revision,
            prompt_version="look-1",
            state="done",
            finished_at=finished,
        )
        db.add_all([item, column])
        await db.flush()
        db.add(ComparisonAnswer(entry_id=column.id, item_id=item.id, ok=True))
        await db.commit()


async def ran_a_test(
    factory: Factory,
    model: LocalModel,
    finished: datetime,
    *,
    kind: str = "text",
    structured_ok: bool = True,
    state: str = "done",
) -> None:
    """A Test of this model, as the lane leaves it."""
    async with factory() as db:
        db.add(
            LocalAiJob(
                kind="test",
                subject=model.repo_id,
                model_id=model.id,
                state=state,
                finished_at=finished,
                result={"kind": kind, "structured_ok": structured_ok},
            )
        )
        await db.commit()


async def judged(factory: Factory, model: LocalModel) -> dict[str, gate.Evidence]:
    async with factory() as db:
        return await gate.for_roles(db, model, KINDS)


async def test_an_mlx_model_is_never_asked_about(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await add_model(factory, "mlx-community/gemma-4-e4b-it-4bit", runtime="mlx-vlm")

    async def never(_db: AsyncSession, _model_id: uuid.UUID) -> datetime | None:
        raise AssertionError("nothing is looked up for a program that does not ask")

    monkeypatch.setattr(gate, "_compared", never)
    monkeypatch.setattr(gate, "_tested_as_text", never)
    for role, known in (await judged(factory, model)).items():
        assert known == gate.NOT_NEEDED, role
        assert not known.asks(False) and known.said() == "not_needed"
    # Nor is a model whose program this install does not have (the role is
    # refused for that, in its own words, before any question).
    elsewhere = await add_model(factory, "example-org/other", runtime="mlxcel")
    assert set((await judged(factory, elsewhere)).values()) == {gate.NOT_NEEDED}


async def test_a_llama_cpp_model_that_was_compared_is_not_asked_about(factory: Factory) -> None:
    model = await add_model(factory)
    other = await add_model(factory, "google/demo-other-gguf")
    await compared(factory, other, NOW)  # another model's comparison is not this one's
    before = await judged(factory, model)
    assert before["looker"].asks(False) and before["editor"].asks(False)

    await compared(factory, model, NOW - timedelta(days=1))
    await compared(factory, model, NOW)
    after = await judged(factory, model)
    for role in ("looker", "editor"):
        known = after[role]
        assert (known.needed, known.seen, known.by, known.at) == (True, True, "compare", NOW)
        assert known.question == "" and not known.asks(False) and known.said() == "seen"
    async with factory() as db:
        assert await gate.evidence(db, model, "looker", KINDS) == after["looker"]


async def never_compared(_db: AsyncSession, _model_id: uuid.UUID) -> datetime | None:
    return None


async def test_the_editor_accepts_a_text_test_as_evidence_and_the_looker_does_not(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gate._compared
    monkeypatch.setattr(gate, "_compared", never_compared)
    # Only a model that does not read pictures is given a text Test
    # (handlers.pick_sample), so that is the model that can show one.
    model = await add_model(factory, "google/demo-text-gguf", takes=("text",))
    # None of these shows an answer in the form asked for drafting.
    await ran_a_test(factory, model, NOW - timedelta(hours=4), kind="picture")
    await ran_a_test(factory, model, NOW - timedelta(hours=3), structured_ok=False)
    await ran_a_test(factory, model, NOW - timedelta(hours=2), state="failed")
    await ran_a_test(factory, model, NOW - timedelta(hours=1), state="running")
    found = await judged(factory, model)
    assert found["editor"].asks(False) and found["editor"].by == ""

    await ran_a_test(factory, model, NOW - timedelta(days=1))
    await ran_a_test(factory, model, NOW - timedelta(hours=5))
    found = await judged(factory, model)
    editor, looker = found["editor"], found["looker"]
    assert (editor.seen, editor.by, editor.at, editor.question) == (
        True,
        "test",
        NOW - timedelta(hours=5),
        "",
    )
    assert editor.said() == "seen"
    assert (looker.seen, looker.by, looker.at) == (False, "", None)
    assert looker.question == (
        "demo-text-gguf runs on llama.cpp and has not been through Compare on your footage, "
        "so nobody has seen how well it looks at your pictures. Make it the Looker anyway?"
    )
    # A comparison is the better evidence, and is what the Editor is then told it has.
    monkeypatch.setattr(gate, "_compared", real)
    await compared(factory, model, NOW)
    assert (await judged(factory, model))["editor"].by == "compare"


async def test_a_model_that_reads_pictures_is_asked_about_compare_alone_for_the_editor(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "_compared", never_compared)
    model = await add_model(factory)  # reads pictures, as most GGUF builds do
    # Its Test is a picture Test, however well it went: that says nothing of drafting.
    await ran_a_test(factory, model, NOW - timedelta(hours=1), kind="picture")
    editor = (await judged(factory, model))["editor"]
    assert (editor.seen, editor.by, editor.at) == (False, "", None)
    # So the question does not send the operator to a Test that can never count.
    assert editor.question == (
        "demo-vision-gguf runs on llama.cpp and has not been through Compare on your footage, "
        "so nobody has seen an answer from it. Make it the Editor anyway?"
    )
    assert "Test" not in editor.question


async def test_compare_being_absent_means_the_question_is_asked(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await add_model(factory, takes=("text",))
    await compared(factory, model, NOW)
    # L2's module cannot be imported: the question is asked, never skipped.
    monkeypatch.delattr(local_models, "comparisons", raising=False)
    monkeypatch.setitem(sys.modules, "framefound.local_models.comparisons", None)
    found = await judged(factory, model)
    for role in ("looker", "editor"):
        assert found[role].asks(False), role
        # One click still answers it: a yes goes ahead, and the audit says so.
        assert not found[role].asks(True) and found[role].said() == "skipped"
    assert found["editor"].question == (
        "demo-vision-gguf runs on llama.cpp and has not been through Compare or a text Test, "
        "so nobody has seen an answer from it. Make it the Editor anyway?"
    )


async def test_compare_renamed_or_reshaped_means_the_question_is_asked_not_a_crash(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await add_model(factory)
    await compared(factory, model, NOW)
    assert not (await judged(factory, model))["looker"].asks(False)

    # L2's function renamed: the module imports, the name is gone.
    monkeypatch.delattr(comparisons, "last_finished_for")
    found = await judged(factory, model)
    assert found["looker"].asks(False) and found["editor"].asks(False)
    assert not found["looker"].asks(True) and found["looker"].said() == "skipped"

    # L2's function given another shape: still a question, never an exception.
    async def reshaped(db: AsyncSession) -> datetime | None:
        raise AssertionError("never called with the wrong arguments")

    monkeypatch.setattr(comparisons, "last_finished_for", reshaped, raising=False)
    assert (await judged(factory, model))["looker"].asks(False)

    # A failure inside the real lookup is a broken database, and is not hidden.
    async def broken(_db: AsyncSession, _model_id: uuid.UUID) -> datetime | None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(comparisons, "last_finished_for", broken)
    with pytest.raises(RuntimeError, match="locked"):
        await judged(factory, model)


async def test_a_model_row_is_judged_for_the_looker_if_it_reads_pictures(
    factory: Factory,
) -> None:
    sees = await add_model(factory)
    reads = await add_model(factory, "google/demo-text-gguf", takes=("text",))
    assert (gate.shown_role(sees), gate.shown_role(reads)) == ("looker", "editor")


async def test_an_unknown_role_is_a_mistake_not_a_question(factory: Factory) -> None:
    model = await add_model(factory)
    with pytest.raises(ValueError, match="narrator"):
        async with factory() as db:
            await gate.evidence(db, model, "narrator", KINDS)
