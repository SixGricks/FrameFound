"""Tags reach new media, and the bar stops locking tags out.

Five things the learner got wrong, each pinned here:

- new media was never scored against existing tags — only an edit or a
  Re-learn ran the learner, so nothing shot after a tag was taught got it;
- the bar was floored at the top 1% of the library however many examples
  agreed, so a common subject could never be fully found;
- one rejection set the bar for the whole library, so a single near-miss
  locked out every true match less similar than it;
- a video was represented by the mean of every frame, so a long auction clip
  tagged "power broom" taught "auction crowd";
- the words' guess came from two generic prompts, ignoring the kinds of
  picture this library actually holds.
"""

import asyncio
import math
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.ai import tagging
from framefound.ai.embeddings import EmbeddingResult
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, AssetTag, Frame, Library, Tag

BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
WIDTH = 8


def unit(*components: float) -> list[float]:
    padded = list(components) + [0.0] * (WIDTH - len(components))
    return tagging.normalise(padded)


def rotated(angle: float) -> list[float]:
    """`angle` radians from unit(1) in the first plane: cosine == cos(angle)."""
    return unit(math.cos(angle), math.sin(angle))


BROOM = unit(1)
CROWD = unit(0, 0, 1)


# --- the words ------------------------------------------------------------


class _FakeEncoder:
    """Stands in for the embedding provider's text encoder."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.seen: list[str] = []
        self.fail_on = fail_on

    def __call__(self, prompt: str) -> list[float]:
        self.seen.append(prompt)
        if prompt == self.fail_on:
            raise RuntimeError("encoder hiccup")
        # A distinct, deterministic direction per prompt.
        return unit(*[float((len(prompt) * (i + 1)) % 7) + 0.5 for i in range(WIDTH)])


def test_the_words_are_an_ensemble_over_the_librarys_kinds_of_picture() -> None:
    encoder = _FakeEncoder()
    vector = tagging.text_prototype("Power Broom", encoder)
    assert encoder.seen == [
        "a photo of power broom",
        "a real estate photo of power broom",
        "an aerial drone photo of power broom",
        "a photo of a power broom at a farm auction",
    ]
    expected = tagging.mean_vector([_FakeEncoder()(p) for p in encoder.seen])
    assert vector == pytest.approx(expected)
    assert tagging.cosine(vector, vector) == pytest.approx(1.0)


def test_one_prompt_failing_to_encode_does_not_lose_the_rest() -> None:
    encoder = _FakeEncoder(fail_on="an aerial drone photo of power broom")
    vector = tagging.text_prototype("power broom", encoder)
    survivors = [p for p in encoder.seen if p != encoder.fail_on]
    assert vector == pytest.approx(tagging.mean_vector([_FakeEncoder()(p) for p in survivors]))


def test_no_encodable_words_is_no_guess() -> None:
    def broken(prompt: str) -> list[float]:
        raise RuntimeError("no runtime")

    assert tagging.text_prototype("barn", broken) is None


def test_a_tag_named_like_a_room_borrows_the_room_paraphrases() -> None:
    """ai/rooms.py already curated prompts for barns and pastures in this
    library; a tag of the same name should not start from scratch."""
    prompts = tagging.prompt_variants("Barns")
    assert "a large barn on a farm" in prompts
    assert "a photo of barns" in prompts
    assert "a fenced pasture with farmland" in tagging.prompt_variants("field")
    # A name that is not a room gets the templates alone.
    assert len(tagging.prompt_variants("power broom")) == len(tagging.TEXT_TEMPLATES)


# --- the bar ----------------------------------------------------------------


def test_five_agreeing_examples_may_reach_past_the_top_one_percent() -> None:
    scores = [i / 1000 for i in range(1000)]
    prototype = unit(1)
    weak = rotated(math.acos(0.93))  # weakest example scores 0.93
    examples = [unit(1)] * 4 + [weak]

    few = tagging.derive_threshold(prototype, examples, [], tagging.percentile(scores, 99.0))
    assert tagging.baseline_percentile(4) == tagging.BASELINE_PERCENTILE
    assert tagging.baseline_percentile(5) == tagging.RELAXED_BASELINE_PERCENTILE

    baseline = tagging.percentile(scores, tagging.baseline_percentile(len(examples)))
    assert baseline == pytest.approx(0.899, abs=0.002)
    many = tagging.derive_threshold(prototype, examples, [], baseline)
    assert many.value == pytest.approx(0.93 - tagging.POSITIVE_MARGIN)
    assert many.value < few.value, "the 99th-percentile floor no longer holds it"

    # With fewer than five examples the 99th percentile still floors the bar.
    held = tagging.derive_threshold(
        prototype, examples[3:], [], tagging.percentile(scores, tagging.baseline_percentile(2))
    )
    assert held.value == pytest.approx(0.989, abs=0.002)
    assert "top 1%" in held.reason


def test_the_relaxed_floor_is_reported_as_such() -> None:
    reason = tagging.derive_threshold(unit(1), [rotated(1.5)] * 5, [], baseline=0.5).reason
    assert "top 10%" in reason and "5 examples" in reason


def test_a_single_rejection_cannot_lock_a_tag() -> None:
    """Old rule: the bar went above the closest rejection, so one near-miss
    scoring 0.955 put every true match below 0.965 out of reach."""
    prototype = unit(1)
    positives = [rotated(0.6), rotated(-0.6)]
    rejection = rotated(0.3)
    threshold = tagging.derive_threshold(prototype, positives, [rejection])
    true_match = rotated(-0.4)
    assert tagging.cosine(prototype, true_match) >= threshold.value
    assert not tagging.vetoed(true_match, positives, [rejection])
    assert tagging.vetoed(rotated(0.32), positives, [rejection])


def test_without_rejections_nothing_is_vetoed() -> None:
    assert tagging.veto_flags([rotated(0.1), rotated(2.0)], [rotated(0.0)], []) == [False, False]
    assert tagging.veto_flags([], [rotated(0.0)], [rotated(0.1)]) == []


def test_with_nothing_accepted_a_rejection_vetoes_only_near_copies() -> None:
    """Before any example there is nothing to weigh a rejection against, so
    it rules out re-exports of itself, not its whole neighbourhood."""
    rejection = rotated(0.0)
    assert tagging.vetoed(rotated(0.05), [], [rejection])
    assert not tagging.vetoed(rotated(0.8), [], [rejection])


# --- video ------------------------------------------------------------------


def _auction_clip(broom_frames: int = 3, crowd_frames: int = 20) -> list[list[float]]:
    return [rotated(0.05 * i) for i in range(broom_frames)] + [
        unit(0.1, 0, 1, 0.05 * i) for i in range(crowd_frames)
    ]


def test_a_video_is_its_frames_most_like_the_tag_not_the_whole_clip() -> None:
    clip = _auction_clip()
    every_frame = tagging.mean_vector(clip)
    best = tagging.representative(clip, BROOM)
    assert every_frame is not None and best is not None
    assert tagging.cosine(every_frame, CROWD) > tagging.cosine(every_frame, BROOM)
    assert tagging.cosine(best, BROOM) > 0.99


def test_a_still_is_itself_and_no_reference_falls_back_to_every_frame() -> None:
    assert tagging.representative([rotated(0.3)], BROOM) == pytest.approx(rotated(0.3))
    clip = _auction_clip()
    assert tagging.representative(clip, None) == pytest.approx(tagging.mean_vector(clip))
    assert tagging.representative([], BROOM) is None


# --- the database -----------------------------------------------------------


class _FakeProvider:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def embed_image(self, path: Path) -> EmbeddingResult:
        return EmbeddingResult(vector=self.vectors[path.name], model_name="fake-clip")

    def embed_text(self, text: str) -> EmbeddingResult:
        # Every prompt reads as a power broom, so the words' guess is BROOM.
        return EmbeddingResult(vector=BROOM, model_name="fake-clip")


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'arrival.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "arrival-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        library = Library(name="L", root_path=str(tmp_path))
        db.add(library)
        await db.commit()
        library_id = library.id

    vectors: dict[str, list[float]] = {}
    monkeypatch.setattr(
        "framefound.ai.embeddings.get_embedding_provider", lambda: _FakeProvider(vectors)
    )
    yield {
        "factory": factory,
        "library_id": library_id,
        "data": tmp_path / "data",
        "vectors": vectors,
    }
    await engine.dispose()
    get_settings.cache_clear()


async def _asset(env: dict, name: str, frames: list[list[float]], embedded: bool) -> uuid.UUID:
    """An asset with sampled frames; embedded now, or left for _embed_frames."""
    async with env["factory"]() as db:
        asset = Asset(
            library_id=env["library_id"],
            relative_path=name,
            filename=name,
            extension="mp4",
            media_type="video",
            size_bytes=1000,
            mtime=BASE,
            availability="online",
        )
        db.add(asset)
        await db.flush()
        for index, vector in enumerate(frames):
            relpath = f"frames/{asset.id}-{index}.jpg"
            db.add(
                Frame(
                    asset_id=asset.id,
                    ts_ms=index * 1000,
                    relative_path=relpath,
                    embedding=vector if embedded else None,
                )
            )
            if not embedded:
                target = env["data"] / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"jpeg")
                env["vectors"][target.name] = vector
        await db.commit()
        return asset.id


async def _tag(
    env: dict,
    name: str,
    prototype: list[float] | None,
    threshold: float | None,
    *,
    enabled: bool = True,
) -> uuid.UUID:
    async with env["factory"]() as db:
        tag = Tag(
            name=name,
            slug=tagging.slugify(name),
            prototype=prototype,
            threshold=threshold,
            suggest_enabled=enabled,
        )
        db.add(tag)
        await db.commit()
        return tag.id


async def _many(env: dict, prefix: str, vector: list[float], count: int) -> list[uuid.UUID]:
    """`count` stills, each one embedded frame of `vector`."""
    ids: list[uuid.UUID] = []
    async with env["factory"]() as db:
        for index in range(count):
            asset = Asset(
                library_id=env["library_id"],
                relative_path=f"{prefix}-{index}.jpg",
                filename=f"{prefix}-{index}.jpg",
                extension="jpg",
                media_type="image",
                size_bytes=1,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            db.add(Frame(asset_id=asset.id, ts_ms=0, relative_path="x.jpg", embedding=vector))
            ids.append(asset.id)
        await db.commit()
    return ids


async def _pending(env: dict, tag_id: uuid.UUID) -> int:
    async with env["factory"]() as db:
        rows = (
            await db.execute(
                select(AssetTag.asset_id).where(
                    AssetTag.tag_id == tag_id, AssetTag.source == "suggested"
                )
            )
        ).all()
        return len(rows)


async def _link(
    env: dict, asset_id: uuid.UUID, tag_id: uuid.UUID, source: str, confidence: float | None = None
) -> None:
    async with env["factory"]() as db:
        db.add(AssetTag(asset_id=asset_id, tag_id=tag_id, source=source, confidence=confidence))
        await db.commit()


async def _links(env: dict, asset_id: uuid.UUID) -> dict[uuid.UUID, AssetTag]:
    async with env["factory"]() as db:
        rows = (
            (await db.execute(select(AssetTag).where(AssetTag.asset_id == asset_id)))
            .scalars()
            .all()
        )
        return {row.tag_id: row for row in rows}


async def _embed(env: dict, asset_id: uuid.UUID) -> None:
    from framefound.processing.tasks import _embed_frames

    async with env["factory"]() as db:
        asset = await db.get(Asset, asset_id)
        library = await db.get(Library, env["library_id"])
        assert asset is not None and library is not None
        await _embed_frames(db, asset, library, Path(library.root_path) / asset.relative_path)


async def test_new_media_is_scored_against_the_vocabulary_as_it_is_embedded(env: dict) -> None:
    broom = await _tag(env, "Power Broom", BROOM, 0.8)
    barn = await _tag(env, "Barn", unit(0, 1), 0.8)
    clip = await _asset(env, "new-clip.mp4", _auction_clip(), embedded=False)

    await _embed(env, clip)

    links = await _links(env, clip)
    assert set(links) == {broom}, "the broom tag reaches it; the barn tag does not"
    assert links[broom].source == "suggested"
    assert links[broom].origin == "clip"
    assert links[broom].confidence is not None and links[broom].confidence > 0.99
    assert barn not in links


async def test_arrival_scoring_never_touches_an_existing_judgement(env: dict) -> None:
    rejected = await _tag(env, "Broom A", BROOM, 0.5)
    manual = await _tag(env, "Broom B", BROOM, 0.5)
    pending = await _tag(env, "Broom C", BROOM, 0.5)
    fresh = await _tag(env, "Broom D", BROOM, 0.5)
    asset = await _asset(env, "a.mp4", [BROOM], embedded=False)
    await _link(env, asset, rejected, "rejected")
    await _link(env, asset, manual, "manual")
    await _link(env, asset, pending, "suggested", confidence=0.61)

    await _embed(env, asset)

    links = await _links(env, asset)
    assert links[rejected].source == "rejected" and links[rejected].confidence is None
    assert links[manual].source == "manual"
    assert links[pending].source == "suggested" and links[pending].confidence == 0.61
    assert links[fresh].source == "suggested" and links[fresh].origin == "clip"


async def test_arrival_respects_the_bar_and_the_off_switch(env: dict) -> None:
    await _tag(env, "Too strict", BROOM, 0.999)
    await _tag(env, "Switched off", BROOM, 0.1, enabled=False)
    await _tag(env, "Never learned", None, None)
    asset = await _asset(env, "b.mp4", [rotated(0.3)], embedded=False)

    await _embed(env, asset)

    assert await _links(env, asset) == {}


async def test_a_rejection_vetoes_its_look_alikes_on_arrival(env: dict) -> None:
    tag = await _tag(env, "Power Broom", BROOM, 0.8)
    accepted = await _asset(env, "yes.mp4", [rotated(-0.6)], embedded=True)
    wrong = await _asset(env, "no.mp4", [rotated(0.3)], embedded=True)
    await _link(env, accepted, tag, "manual")
    await _link(env, wrong, tag, "rejected")
    look_alike = await _asset(env, "like-no.mp4", [rotated(0.32)], embedded=False)
    true_match = await _asset(env, "like-yes.mp4", [rotated(-0.4)], embedded=False)

    await _embed(env, look_alike)
    await _embed(env, true_match)

    assert await _links(env, look_alike) == {}
    assert (await _links(env, true_match))[tag].source == "suggested"


async def test_a_failure_in_tagging_does_not_fail_the_embedding(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.processing import tag_tasks

    async def broken(db: object, asset_id: uuid.UUID) -> int:
        raise RuntimeError("tag table on fire")

    monkeypatch.setattr(tag_tasks, "_suggest_tags_for_asset", broken)
    await _tag(env, "Power Broom", BROOM, 0.5)
    asset = await _asset(env, "c.mp4", [BROOM], embedded=False)

    await _embed(env, asset)

    async with env["factory"]() as db:
        frames = (await db.execute(select(Frame).where(Frame.asset_id == asset))).scalars().all()
        assert [f.embedding_model for f in frames] == ["fake-clip"]
    assert await _links(env, asset) == {}


async def test_one_rejection_no_longer_locks_the_whole_library(env: dict) -> None:
    """End to end through suggest_for_tag. The rejection scores 0.955; under
    the old rule nothing below 0.965 could be suggested, including a true
    match at 0.92 that sits right next to an accepted example."""
    from framefound.processing.tag_tasks import suggest_for_tag

    tag = await _tag(env, "Power Broom", BROOM, None)
    for side in (0.6, -0.6):
        await _link(env, await _asset(env, f"yes{side}.mp4", [rotated(side)], True), tag, "manual")
    await _link(env, await _asset(env, "no.mp4", [rotated(0.3)], True), tag, "rejected")
    true_match = await _asset(env, "match.mp4", [rotated(-0.4)], True)
    look_alike = await _asset(env, "look-alike.mp4", [rotated(0.32)], True)
    # Enough of the library for a percentile to mean something.
    async with env["factory"]() as db:
        for index in range(500):
            asset = Asset(
                library_id=env["library_id"],
                relative_path=f"filler-{index}.jpg",
                filename=f"filler-{index}.jpg",
                extension="jpg",
                media_type="image",
                size_bytes=1,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            db.add(Frame(asset_id=asset.id, ts_ms=0, relative_path="x.jpg", embedding=CROWD))
        await db.commit()

    await asyncio.to_thread(suggest_for_tag, str(tag))

    assert (await _links(env, true_match))[tag].source == "suggested"
    assert await _links(env, look_alike) == {}
    async with env["factory"]() as db:
        stored = await db.get(Tag, tag)
        assert stored is not None and stored.threshold is not None
        assert stored.threshold < tagging.cosine(BROOM, rotated(-0.4))
        assert "veto" in stored.threshold_reason


async def test_a_long_clip_is_suggested_on_its_best_frames(env: dict) -> None:
    """Scored as the mean of every frame, a clip that is mostly crowd would
    never clear a broom bar; scored on its best frames, it does."""
    from framefound.processing.tag_tasks import suggest_tags_for_asset

    tag = await _tag(env, "Power Broom", BROOM, 0.9)
    clip = await _asset(env, "auction.mp4", _auction_clip(3, 40), embedded=True)
    every_frame = tagging.mean_vector(_auction_clip(3, 40))
    assert every_frame is not None and tagging.cosine(BROOM, every_frame) < 0.9

    async with env["factory"]() as db:
        assert await suggest_tags_for_asset(db, clip) == 1
    assert (await _links(env, clip))[tag].source == "suggested"


async def test_the_nightly_sweep_runs_every_learned_tag(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.celery_app import celery_app
    from framefound.processing import tag_tasks

    entries = {e["task"]: e for e in celery_app.conf.beat_schedule.values()}
    assert "framefound.suggest_all_tags" in entries

    learned = await _tag(env, "Power Broom", BROOM, 0.5)
    await _tag(env, "Never learned", None, None)
    await _tag(env, "Switched off", BROOM, 0.5, enabled=False)
    queued: list[str] = []
    monkeypatch.setattr(tag_tasks.learn_tag, "delay", queued.append)

    await asyncio.to_thread(tag_tasks.suggest_all_tags)

    assert queued == [str(learned)]


# --- the review queue stays bounded -------------------------------------------


async def test_unanswered_suggestions_are_capped_per_tag_not_per_run(env: dict) -> None:
    """The cap used to be per run. Once the nightly sweep re-ran every tag
    unattended, each run skipped what was already waiting and offered the
    next 60, so a queue nobody answered grew by 60 per tag per night."""
    from framefound.processing.tag_tasks import MAX_PENDING_PER_TAG, suggest_for_tag

    tag = await _tag(env, "Power Broom", BROOM, None)
    brooms = await _many(env, "broom", BROOM, 100)
    await _many(env, "crowd", CROWD, 100)

    await asyncio.to_thread(suggest_for_tag, str(tag))
    assert await _pending(env, tag) == MAX_PENDING_PER_TAG

    await asyncio.to_thread(suggest_for_tag, str(tag))
    assert await _pending(env, tag) == MAX_PENDING_PER_TAG, "a second night adds nothing"

    # Answering frees room, and the next run fills exactly that.
    async with env["factory"]() as db:
        waiting = (
            (
                await db.execute(
                    select(AssetTag).where(AssetTag.tag_id == tag, AssetTag.source == "suggested")
                )
            )
            .scalars()
            .all()
        )
        for link in waiting[:10]:
            link.source = "confirmed"
        await db.commit()
    assert await _pending(env, tag) == MAX_PENDING_PER_TAG - 10

    await asyncio.to_thread(suggest_for_tag, str(tag))
    assert await _pending(env, tag) == MAX_PENDING_PER_TAG
    async with env["factory"]() as db:
        offered = (
            await db.execute(
                select(AssetTag.asset_id).where(
                    AssetTag.tag_id == tag, AssetTag.asset_id.in_(brooms)
                )
            )
        ).all()
    assert len(offered) == MAX_PENDING_PER_TAG + 10


async def test_arrival_scoring_stops_at_a_full_queue(env: dict) -> None:
    """New media gets headroom over the sweep's cap, but not an unbounded
    one: arrival scoring had no cap at all."""
    from framefound.processing.tag_tasks import ARRIVAL_PENDING_PER_TAG

    full = await _tag(env, "Broom full", BROOM, 0.5)
    room = await _tag(env, "Broom with room", BROOM, 0.5)
    backlog = await _many(env, "old", CROWD, ARRIVAL_PENDING_PER_TAG)
    async with env["factory"]() as db:
        for asset_id in backlog:
            db.add(AssetTag(asset_id=asset_id, tag_id=full, source="suggested", origin="clip"))
        for asset_id in backlog[:-1]:
            db.add(AssetTag(asset_id=asset_id, tag_id=room, source="suggested", origin="clip"))
        await db.commit()
    asset = await _asset(env, "new.mp4", [BROOM], embedded=False)

    await _embed(env, asset)

    assert set(await _links(env, asset)) == {room}, "one short of the cap still takes it"
    assert await _pending(env, full) == ARRIVAL_PENDING_PER_TAG


# --- the bar arrival scoring judges by ----------------------------------------


async def test_relearning_saves_the_prototype_and_its_measured_bar_together(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """learn_tag can only derive the fallback bar (0.26 for words alone), on
    a different scale from the percentile bar the suggestion pass measures.
    Committed alone, arrival scoring judged new media by it and wrote what
    the real bar would refuse. Committing no bar instead made arrival scoring
    skip the tag until the queued pass ran, and that pass only tops up to 60:
    with 80 waiting, a file indexed in between was never suggested, though
    arrival scoring promises headroom to 120 (2026-11 review)."""
    from framefound.processing import tag_tasks

    tag = await _tag(env, "Power Broom", BROOM, 0.5)
    waiting = await _many(env, "waiting", rotated(0.5), 80)  # each scores 0.878
    await _many(env, "crowd", CROWD, 200)
    async with env["factory"]() as db:
        for asset_id in waiting:
            db.add(AssetTag(asset_id=asset_id, tag_id=tag, source="suggested", origin="clip"))
        await db.commit()
    queued: list[str] = []
    monkeypatch.setattr(tag_tasks.suggest_for_tag, "delay", queued.append)

    await asyncio.to_thread(tag_tasks.learn_tag, str(tag))

    assert queued == [], "the library is scored inside the learn, not after it"
    async with env["factory"]() as db:
        stored = await db.get(Tag, tag)
        assert stored is not None and stored.prototype is not None
        learned_bar = stored.threshold
    # The top 1% of 280 scores, not the 0.26 words-alone fallback.
    assert learned_bar == pytest.approx(math.cos(0.5))

    # Indexed straight after the edit, with 80 waiting: still offered.
    arrival = await _asset(env, "after-edit.mp4", [rotated(0.3)], embedded=False)
    await _embed(env, arrival)
    assert (await _links(env, arrival))[tag].source == "suggested"
    assert await _pending(env, tag) == 81

    # A standalone pass measures the same bar the learn stored.
    await asyncio.to_thread(tag_tasks.suggest_for_tag, str(tag))
    async with env["factory"]() as db:
        stored = await db.get(Tag, tag)
        assert stored is not None and stored.threshold == pytest.approx(learned_bar)


async def test_a_failed_pass_keeps_the_previous_prototype_and_bar(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the learn and the pass committed separately, a pass that failed
    after its retries left the tag with a new prototype and no usable bar
    until the next night."""
    from framefound.processing import tag_tasks

    old = unit(0, 1)
    tag = await _tag(env, "Power Broom", old, 0.6)

    async def broken(db: object, tag: object) -> None:
        raise RuntimeError("scoring failed")

    monkeypatch.setattr(tag_tasks, "_score_library", broken)
    with pytest.raises(RuntimeError):
        await asyncio.to_thread(tag_tasks.learn_tag, str(tag))

    async with env["factory"]() as db:
        stored = await db.get(Tag, tag)
        assert stored is not None
        assert stored.prototype == pytest.approx(old)
        assert stored.threshold == pytest.approx(0.6)


async def test_without_a_pass_coming_learning_keeps_its_own_bar(env: dict) -> None:
    from framefound.processing import tag_tasks

    tag = await _tag(env, "Power Broom", BROOM, None)
    off = await _tag(env, "Switched off", BROOM, None, enabled=False)

    await asyncio.to_thread(tag_tasks.learn_tag, str(tag), False)
    # Switched off: the library is not scored, so no percentile bar comes.
    await asyncio.to_thread(tag_tasks.learn_tag, str(off))

    async with env["factory"]() as db:
        for tag_id in (tag, off):
            stored = await db.get(Tag, tag_id)
            assert stored is not None
            assert stored.threshold == tagging.TEXT_ONLY_FLOOR
            assert "nothing tagged yet" in stored.threshold_reason
