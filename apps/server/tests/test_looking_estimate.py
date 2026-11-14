"""About how long a review should take: seconds measured on this model's own answers.

An estimate, never a promise and never a grade: it is time, read off what
the model took before, and its sentence says what it rests on and what it
leaves out.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Library, LocalAiJob, LocalModel, Look
from framefound.local_models import registry
from framefound.looking import estimate
from framefound.looking.scope import Counts

Factory = async_sessionmaker[AsyncSession]
BASE = datetime(2026, 6, 1, tzinfo=UTC)
REVISION = "475b9088" + "0" * 32


def _model(repo: str = "mlx-community/gemma-4-e4b-it-4bit", variant: str = "") -> LocalModel:
    return LocalModel(
        repo_id=repo,
        repo_key=repo.casefold(),
        revision=REVISION,
        variant=variant,
        publisher=repo.split("/")[0],
        runtime="mlx-vlm",
        takes=["text", "image"],
        state=registry.INSTALLED,
        memory_gb=5.0,
    )


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'estimate.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    get_settings.cache_clear()
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path))
        db.add(library)
        await db.flush()
        asset = Asset(
            library_id=library.id,
            relative_path="Pete Dye/IMG_0001.JPG",
            filename="IMG_0001.JPG",
            extension="jpg",
            media_type="image",
            size_bytes=1000,
            mtime=BASE,
            availability="online",
            first_indexed_at=BASE,
        )
        model = _model()
        db.add_all([asset, model])
        await db.commit()
    yield {"factory": factory, "asset": asset.id, "model": model}
    await engine.dispose()
    get_settings.cache_clear()


async def _looks(
    env: dict[str, Any],
    seconds: list[float | None],
    *,
    shot: bool = False,
    ok: bool = True,
    repo: str | None = None,
    variant: str = "",
    start: datetime = BASE,
) -> None:
    async with env["factory"]() as db:
        for i, value in enumerate(seconds):
            db.add(
                Look(
                    asset_id=env["asset"],
                    start_ms=i * 1000 if shot else None,
                    end_ms=i * 1000 + 900 if shot else None,
                    ok=ok,
                    current=False,
                    model=repo or env["model"].repo_id,
                    variant=variant,
                    seconds=value,
                    created_at=start + timedelta(minutes=i),
                )
            )
        await db.commit()


async def _speed(env: dict[str, Any], model: LocalModel | None = None) -> estimate.Speed:
    async with env["factory"]() as db:
        return await estimate.speed(db, model or env["model"])


async def _test_job(
    env: dict[str, Any],
    seconds: float | None,
    *,
    kind: str = "picture",
    state: str = "done",
    at: datetime = BASE,
) -> None:
    async with env["factory"]() as db:
        db.add(
            LocalAiJob(
                kind="test",
                subject="x",
                model_id=env["model"].id,
                state=state,
                seconds=seconds,
                result={"kind": kind},
                created_at=at,
                finished_at=at,
            )
        )
        await db.commit()


def _counts(photos: int = 0, shots: int = 0, stills_only: int = 0, need_copy: int = 0) -> Counts:
    return Counts(
        photos=photos,
        clips=1 if shots else 0,
        shots=shots,
        stills_only=stills_only,
        already=0,
        by_other=0,
        need_copy=need_copy,
        need_copy_footage_s=600.0 * need_copy,
        copy_away=0,
        need_shots=0,
        not_ready=0,
        missing=0,
        audio=0,
        failed_before=0,
    )


async def test_the_estimate_is_the_median_of_what_was_measured_photos_and_shots_apart(
    env: dict[str, Any],
) -> None:
    await _looks(env, [9.0, 9.5, 30.0])
    await _looks(env, [14.0, 12.0, 15.0, 100.0], shot=True)
    # Failures, untimed looks and another model's answers are not this model's speed.
    await _looks(env, [1.0, 1.0, 1.0], ok=False)
    await _looks(env, [None, None, None])
    await _looks(env, [0.5, 0.5, 0.5], repo="google/gemma-4-12B-it")
    speed = await _speed(env)
    assert speed == estimate.Speed(9.5, 14.5, 7, "measured")
    assert estimate.seconds_for(_counts(photos=10, shots=4, stills_only=2), speed) == 172


async def test_the_estimate_borrows_from_the_other_kind_when_one_has_no_history(
    env: dict[str, Any],
) -> None:
    await _looks(env, [10.0])
    assert await _speed(env) == estimate.Speed(10.0, 15.0, 1, "measured")


async def test_shots_alone_lend_a_photograph_two_thirds_of_their_time(env: dict[str, Any]) -> None:
    await _looks(env, [15.0, 15.0], shot=True)
    assert await _speed(env) == estimate.Speed(10.0, 15.0, 2, "measured")


async def test_only_the_newest_answers_count(env: dict[str, Any]) -> None:
    """A runtime update that made the model faster shows within one evening."""
    await _looks(env, [60.0] * 50, start=BASE)
    await _looks(env, [6.0] * estimate.SAMPLE, start=BASE + timedelta(days=2))
    speed = await _speed(env)
    assert (speed.per_photo_s, speed.sample) == (6.0, estimate.SAMPLE)


async def test_another_build_of_the_same_repository_is_timed_on_its_own(
    env: dict[str, Any],
) -> None:
    await _looks(env, [4.0], variant="Q4_K_M")
    assert (await _speed(env)).basis == "none"
    q4 = _model(variant="Q4_K_M")
    assert (await _speed(env, q4)).per_photo_s == 4.0


async def test_the_estimate_comes_from_the_one_test_when_nothing_was_looked_at(
    env: dict[str, Any],
) -> None:
    await _test_job(env, 20.0, at=BASE)
    await _test_job(env, 2.0, kind="text", at=BASE + timedelta(hours=1))
    await _test_job(env, 1.0, state="failed", at=BASE + timedelta(hours=2))
    speed = await _speed(env)
    assert speed == estimate.Speed(20.0, 30.0, 1, "test")
    seconds = estimate.seconds_for(_counts(photos=300), speed)
    assert seconds == 6000
    assert estimate.words(seconds, speed) == (
        "Roughly 1 h 40 min, from the one test this model has run. Loading the model and "
        "making playable copies are extra."
    )


async def test_the_estimate_is_absent_when_nothing_was_measured(env: dict[str, Any]) -> None:
    await _test_job(env, None)
    speed = await _speed(env)
    assert speed == estimate.NONE
    assert estimate.seconds_for(_counts(photos=3), speed) is None
    assert estimate.seconds_left(3, speed) is None
    assert estimate.as_dict(None, speed) == {
        "seconds": None,
        "basis": "none",
        "per_photo_s": None,
        "per_shot_s": None,
        "sample": 0,
        "words": (
            "No estimate yet: nothing has been looked at with this model. The first few will tell."
        ),
    }


def test_clips_still_to_be_made_ready_add_nothing_and_the_sentence_says_so() -> None:
    speed = estimate.Speed(9.0, 14.0, 50, "measured")
    counts = _counts(photos=200, need_copy=12)
    assert estimate.seconds_for(counts, speed) == 1800
    assert estimate.to_make_ready(counts) == 12
    assert estimate.words(6000, speed) == (
        "About 1 h 40 min at this model's measured speed (9 s a photograph, 14 s a shot, from "
        "its last 50 answers). Loading the model and making playable copies are extra."
    )
    assert estimate.words(6000, speed, unready=12) == (
        "About 1 h 40 min at this model's measured speed (9 s a photograph, 14 s a shot, from "
        "its last 50 answers). Loading the model and making playable copies are extra. "
        "Looking at the clips still to be made ready is not counted."
    )
    assert estimate.words(0, speed) == "Nothing here is waiting to be looked at."


def test_a_folder_of_clips_that_only_need_making_ready_is_not_called_empty() -> None:
    """Twelve drone clips with no playable copy and nothing else: nothing can
    be timed, but a review of them will do hours of work, so the words must
    not say there is nothing to do."""
    speed = estimate.Speed(9.0, 14.0, 50, "measured")
    counts = _counts(need_copy=12)
    assert estimate.seconds_for(counts, speed) == 0
    body = estimate.as_dict(0, speed, unready=estimate.to_make_ready(counts))
    assert body["words"] == (
        "The clips here have to be made ready first; how long looking at them takes is "
        "known once their shots are found."
    )
    assert "Nothing here" not in body["words"]


def test_less_than_a_second_of_work_is_still_some_work() -> None:
    """One photograph at a demonstration model's 0.4 s rounds to nothing;
    it must still read as under a minute, not as nothing waiting."""
    speed = estimate.Speed(0.4, 0.6, 50, "measured")
    seconds = estimate.seconds_for(_counts(photos=1), speed)
    assert seconds == 1
    assert estimate.words(seconds, speed).startswith("Under a minute")


def test_a_running_review_is_timed_at_the_mean_of_the_two_rates() -> None:
    speed = estimate.Speed(8.0, 12.0, 4, "measured")
    assert estimate.seconds_left(10, speed) == 100
    assert estimate.seconds_left(-3, speed) == 0


def test_lengths_of_time_read_as_the_pages_write_them() -> None:
    assert [estimate.duration_text(s) for s in (5, 59, 60, 1500, 3600, 6000, 10_860)] == [
        "under a minute",
        "under a minute",
        "1 min",
        "25 min",
        "1 h",
        "1 h 40 min",
        "3 h 1 min",
    ]
    assert [estimate.rate_text(s) for s in (2.5, 4.0, 9.04, 14.4)] == [
        "2.5 s",
        "4 s",
        "9 s",
        "14 s",
    ]


def test_the_ids_of_the_test_kind_and_l1_agree() -> None:
    """L1 registers its Test under this name; the estimate reads its rows."""
    from framefound.local_models import handlers, jobs

    assert handlers.__name__  # imported for the kinds it registers
    assert estimate.TEST_KIND in jobs.registered()
