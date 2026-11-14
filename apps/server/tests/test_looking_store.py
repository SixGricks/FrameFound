"""What a look writes: the ledger, the suggestions, the asset's roll-up.

A local model's answer reaches the catalogue only as opinions with an
author: suggestions of origin `local` that the operator still judges, and a
score, caption and reason on the asset that say who gave them. The ledger is
keyed by a shot's times, so a clip re-analysed to the same shots keeps what
was said about them, and "looked at" on a clip means every shot was.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import tag_review
from framefound.ai.tag_review import ORIGIN_LOCAL, TagProposal
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import (
    Asset,
    AssetTag,
    Derivative,
    Library,
    LocalModel,
    Look,
    ShotWindow,
    Tag,
)
from framefound.local_models import registry
from framefound.local_models.ask import Answer
from framefound.looking import INGEST_TRIES, scope, store
from framefound.looking.prompt import PROMPT_VERSION, Cleaned
from framefound.looking.scope import Item
from framefound.processing import shots
from framefound.processing import tasks as task_module

BASE = datetime(2026, 6, 1, tzinfo=UTC)
LATER = datetime(2026, 6, 2, 9, 30, tzinfo=UTC)
REVISION = "475b9088" + "0" * 32
WINDOWS = ((0, 4000), (4000, 9000), (9000, 15000))


def _model(repo: str = "google/gemma-4-E4B-it", variant: str = "") -> LocalModel:
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
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'store.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path))
        db.add(library)
        await db.flush()
        ids: dict[str, uuid.UUID] = {}
        for name, media_type, extension in (
            ("Barns/IMG_0001.JPG", "image", "jpg"),
            ("Drone/DJI_0001.MP4", "video", "mp4"),
            ("Drone/DJI_0002.MP4", "video", "mp4"),
        ):
            asset = Asset(
                library_id=library.id,
                relative_path=name,
                filename=name.rsplit("/", 1)[-1],
                extension=extension,
                media_type=media_type,
                size_bytes=1000,
                mtime=BASE,
                availability="online",
                first_indexed_at=BASE,
            )
            db.add(asset)
            await db.flush()
            ids[name.rsplit("/", 1)[-1]] = asset.id
            kinds = ("thumbnail", "preview") if media_type == "image" else ("thumbnail", "motion")
            for kind in kinds:
                db.add(
                    Derivative(
                        asset_id=asset.id,
                        kind=kind,
                        relative_path=f"derivatives/{asset.id}/{kind}.webp",
                        media_format="webp",
                        status="ready",
                    )
                )
        for idx, (start, end) in enumerate(WINDOWS):
            db.add(
                ShotWindow(
                    asset_id=ids["DJI_0001.MP4"], idx=idx, start_ms=start, end_ms=end, move="orbit"
                )
            )
        model = _model()
        other = _model("google/gemma-4-12B-it")
        db.add_all([model, other, Tag(name="Barn", slug="barn"), Tag(name="Silo", slug="silo")])
        await db.commit()
        yield {
            "factory": factory,
            "library": library.id,
            "photo": ids["IMG_0001.JPG"],
            "clip": ids["DJI_0001.MP4"],
            "bare": ids["DJI_0002.MP4"],
            "model": model,
            "other": other,
        }
    await engine.dispose()
    get_settings.cache_clear()


def _answer(model: LocalModel, *, seconds: float = 4.2, demo: bool = False) -> Answer:
    """Every field L1 lists (P25), so a field it adds later fails here loudly."""
    return Answer(
        text="{}",
        parsed=None,
        model_id=model.id,
        repo_id=model.repo_id,
        revision=model.revision,
        runtime="mlx-vlm",
        runtime_version="0.3.9",
        prompt_version=PROMPT_VERSION,
        seconds=seconds,
        load_seconds=12.0,
        memory_gb=6.1,
        memory_source="runtime",
        finish_reason="stop",
        ran_from="this_machine",
        attempts=1,
        demo=demo,
    )


def _cleaned(
    score: int = 70, caption: str = "A red barn at dusk.", tags: list[TagProposal] | None = None
) -> Cleaned:
    return Cleaned(
        caption=caption,
        score=score,
        reason="Steady and well lit.",
        proposals=tags or [],
        dropped=1,
    )


def _p(name: str, confidence: float | None, existing: bool = True) -> TagProposal:
    return TagProposal(name=name, confidence=confidence, existing=existing)


def _photo(env: dict) -> Item:
    return Item(env["photo"], "IMG_0001.JPG", "image", None, None, None)


def _shot(env: dict, index: int, asset: str = "clip") -> Item:
    start, end = WINDOWS[index]
    return Item(env[asset], "DJI_0001.MP4", "video", start, end, None)


async def _look_at(
    db: AsyncSession,
    env: dict,
    item: Item,
    *,
    score: int = 70,
    tags: list[TagProposal] | None = None,
    model: LocalModel | None = None,
    new_tags_left: int = 30,
    request_id: uuid.UUID | None = None,
    now: datetime = LATER,
) -> tuple[Look, store.Suggested]:
    """What the runner does for one item, in its order."""
    model = model or env["model"]
    cleaned = _cleaned(score=score, caption=f"Shot scored {score}.", tags=tags)
    look = await store.record(
        db, item, _answer(model), cleaned, request_id=request_id, model=model, now=now
    )
    done = await store.suggest(db, look, cleaned.proposals, new_tags_left=new_tags_left)
    await store.roll_up(db, item.asset_id, now=now)
    await db.commit()
    return look, done


async def _asset(db: AsyncSession, asset_id: uuid.UUID) -> Asset:
    return (
        await db.execute(
            select(Asset).where(Asset.id == asset_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _links(db: AsyncSession, asset_id: uuid.UUID) -> dict[str, AssetTag]:
    rows = (
        await db.execute(
            select(Tag.slug, AssetTag)
            .join(Tag, Tag.id == AssetTag.tag_id)
            .where(AssetTag.asset_id == asset_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return {slug: link for slug, link in rows}


# --- suggestions -------------------------------------------------------------------


async def test_tags_arrive_as_suggestions_from_the_local_model_never_as_facts(env: dict) -> None:
    async with env["factory"]() as db:
        look, done = await _look_at(
            db, env, _photo(env), tags=[_p("barn", 0.9), _p("hay bale", 0.95, existing=False)]
        )

        assert (done.written, done.new_tags, done.held) == (2, 1, [])
        links = await _links(db, env["photo"])
        assert set(links) == {"barn", "hay-bale"}
        for link in links.values():
            assert (link.source, link.origin, link.look_id, link.at_ms) == (
                "suggested",
                ORIGIN_LOCAL,
                look.id,
                None,
            )
        stored = (await db.get(Look, look.id, populate_existing=True)).answer  # type: ignore[union-attr]
        assert [(tag["name"], tag["outcome"]) for tag in stored["tags"]] == [
            ("barn", tag_review.OUTCOME_SUGGESTED),
            ("hay bale", tag_review.OUTCOME_NEW_TAG),
        ]
        assert (stored["caption"], stored["score"], stored["dropped"]) == ("Shot scored 70.", 70, 1)


async def test_a_rejected_tag_stays_rejected(env: dict) -> None:
    async with env["factory"]() as db:
        barn = (await db.execute(select(Tag).where(Tag.slug == "barn"))).scalar_one()
        db.add(AssetTag(asset_id=env["photo"], tag_id=barn.id, source="rejected"))
        await db.commit()

        look, done = await _look_at(db, env, _photo(env), tags=[_p("barn", 0.99)])

        assert done.written == 0
        link = (await _links(db, env["photo"]))["barn"]
        assert (link.source, link.look_id) == ("rejected", None)
        assert look.answer is not None
        assert look.answer["tags"][0]["outcome"] == tag_review.OUTCOME_REJECTED


async def test_a_suggestion_on_a_clip_remembers_its_moment(env: dict) -> None:
    async with env["factory"]() as db:
        look, _ = await _look_at(db, env, _shot(env, 1), tags=[_p("barn", 0.9)])

        link = (await _links(db, env["clip"]))["barn"]
        assert (link.look_id, link.at_ms) == (look.id, 6500)


async def test_a_stronger_shot_takes_over_a_pending_suggestion(env: dict) -> None:
    async with env["factory"]() as db:
        await _look_at(db, env, _shot(env, 0), tags=[_p("barn", 0.7)])
        stronger, done = await _look_at(db, env, _shot(env, 1), tags=[_p("barn", 0.9)])
        weaker, after = await _look_at(db, env, _shot(env, 2), tags=[_p("barn", 0.65)])

        links = await _links(db, env["clip"])
        assert list(links) == ["barn"]
        assert (links["barn"].confidence, links["barn"].look_id, links["barn"].at_ms) == (
            0.9,
            stronger.id,
            6500,
        )
        # Strengthened counts as written; a weaker guess leaves the row as it was.
        assert (done.written, after.written) == (1, 0)
        assert weaker.answer is not None
        assert weaker.answer["tags"][0]["outcome"] == tag_review.OUTCOME_PENDING


async def test_a_full_queue_holds_further_suggestions_back_and_says_so(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "MAX_LOCAL_PENDING_PER_TAG", 2)
    async with env["factory"]() as db:
        barn = (await db.execute(select(Tag).where(Tag.slug == "barn"))).scalar_one()
        # Two local suggestions of "barn" are already waiting (the clip's and
        # the bare clip's); a learner's and an answered one do not count.
        db.add_all(
            [
                AssetTag(
                    asset_id=env["clip"], tag_id=barn.id, source="suggested", origin=ORIGIN_LOCAL
                ),
                AssetTag(
                    asset_id=env["bare"], tag_id=barn.id, source="suggested", origin=ORIGIN_LOCAL
                ),
            ]
        )
        await db.commit()

        look, done = await _look_at(db, env, _photo(env), tags=[_p("barn", 0.9), _p("silo", 0.8)])

        assert (done.written, done.held) == (1, ["barn"])
        assert set(await _links(db, env["photo"])) == {"silo"}
        assert look.answer is not None
        assert {tag["name"]: tag["outcome"] for tag in look.answer["tags"]} == {
            "barn": store.OUTCOME_HELD_FULL,
            "silo": tag_review.OUTCOME_SUGGESTED,
        }
        # A suggestion already on this clip is strengthened at the cap: it adds
        # no row to anybody's queue.
        _, strengthened = await _look_at(db, env, _shot(env, 0), tags=[_p("barn", 0.9)])
        assert (strengthened.written, strengthened.held) == (1, [])


async def test_a_review_proposes_only_as_many_new_tags_as_it_may(env: dict) -> None:
    async with env["factory"]() as db:
        look, done = await _look_at(
            db,
            env,
            _photo(env),
            tags=[
                _p("hay bale", 0.95, existing=False),
                _p("tractor", 0.9, existing=False),
                _p("barn", 0.8),
            ],
            new_tags_left=1,
        )

        assert (done.written, done.new_tags, done.held) == (2, 1, ["tractor"])
        assert set(await _links(db, env["photo"])) == {"hay-bale", "barn"}
        assert (await db.execute(select(Tag).where(Tag.slug == "tractor"))).first() is None
        assert look.answer is not None
        assert look.answer["tags"][1]["outcome"] == store.OUTCOME_HELD_NEW

        _, none_left = await _look_at(
            db, env, _shot(env, 0), tags=[_p("windmill", 0.95, existing=False)], new_tags_left=0
        )
        assert (none_left.written, none_left.held) == (0, ["windmill"])


async def test_a_local_guess_leaves_the_picture_in_claudes_pool(env: dict) -> None:
    async with env["factory"]() as db:
        await _look_at(db, env, _photo(env), tags=[_p("barn", 0.9)])

        assert env["photo"] in await tag_review.pick_untagged(db, 10)


# --- the ledger ----------------------------------------------------------------------


async def test_every_look_records_who_said_it(env: dict) -> None:
    built = _model("google/gemma-4-12B-it-GGUF", variant="Q4_K_M")
    async with env["factory"]() as db:
        db.add(built)
        await db.commit()
        cleaned = _cleaned()
        # The variant is read off the model's row when the caller does not hold it.
        look = await store.record(
            db, _photo(env), _answer(built, seconds=7.5, demo=True), cleaned, request_id=None
        )
        await db.commit()
        row = await db.get(Look, look.id, populate_existing=True)

        assert row is not None
        assert (row.model, row.model_revision, row.variant) == (
            "google/gemma-4-12B-it-GGUF",
            REVISION,
            "Q4_K_M",
        )
        assert (row.runtime, row.runtime_version, row.prompt_version) == (
            "mlx-vlm",
            "0.3.9",
            PROMPT_VERSION,
        )
        assert row.key == f"google/gemma-4-12B-it-GGUF@{REVISION}/Q4_K_M#{PROMPT_VERSION}"
        assert row.key == store.key_for(built)
        assert (row.seconds, row.demo, row.ok, row.current) == (7.5, True, True, True)
        assert (row.score, row.caption, row.reason) == (
            70,
            "A red barn at dusk.",
            "Steady and well lit.",
        )
        assert row.request_id is None

        failed = await store.record_failure(
            db, _shot(env, 0), model=built, note="It said\x00 nothing\n usable.", request_id=None
        )
        assert (failed.ok, failed.current, failed.key, failed.variant) == (
            False,
            False,
            store.key_for(built),
            "Q4_K_M",
        )
        assert (failed.model, failed.model_revision, failed.prompt_version) == (
            built.repo_id,
            REVISION,
            PROMPT_VERSION,
        )
        assert failed.note == "It said nothing usable."


async def test_a_look_is_not_stored_under_another_models_key(env: dict) -> None:
    async with env["factory"]() as db:
        with pytest.raises(ValueError, match="not from this model"):
            await store.record(
                db,
                _photo(env),
                _answer(env["model"]),
                _cleaned(),
                request_id=None,
                model=env["other"],
            )
        gone = _model("google/gemma-4-E4B")
        gone.id = uuid.uuid4()  # never added: what a removed model's answer names
        with pytest.raises(ValueError, match="no longer installed"):
            await store.record(db, _photo(env), _answer(gone), _cleaned(), request_id=None)
        assert (await db.execute(select(func.count()).select_from(Look))).scalar_one() == 0


async def test_a_span_has_one_current_look_and_a_redo_replaces_it(env: dict) -> None:
    async with env["factory"]() as db:
        first, _ = await _look_at(db, env, _photo(env), score=40)
        second, _ = await _look_at(db, env, _photo(env), score=85, model=env["other"])
        await store.record_failure(
            db, _photo(env), model=env["model"], note="Timed out.", request_id=None
        )
        await db.commit()

        rows = (
            (await db.execute(select(Look).execution_options(populate_existing=True)))
            .scalars()
            .all()
        )
        assert [(row.id, row.current) for row in rows if row.ok] == [
            (first.id, False),
            (second.id, True),
        ]
        assert list((await store.current(db, env["photo"])).values()) == [second]
        photo = await _asset(db, env["photo"])
        assert (photo.look_score, photo.look_caption, photo.look_reason, photo.looked_by) == (
            85,
            "Shot scored 85.",
            "Steady and well lit.",
            store.key_for(env["other"]),
        )
        assert photo.looked_at is not None


async def test_removing_the_model_leaves_the_look_and_its_tags(env: dict) -> None:
    assert not [fk for fk in Look.__table__.foreign_keys if fk.column.table.name == "local_models"]
    async with env["factory"]() as db:
        look, _ = await _look_at(db, env, _photo(env), tags=[_p("barn", 0.9)])
        await db.execute(delete(LocalModel).where(LocalModel.id == env["model"].id))
        await db.commit()

        row = await db.get(Look, look.id, populate_existing=True)
        assert row is not None and row.model == env["model"].repo_id
        assert (await _links(db, env["photo"]))["barn"].look_id == look.id


async def test_what_the_store_writes_is_what_a_scope_reads(env: dict) -> None:
    """The contract with looking/scope.py: a looked-at item is not handed out
    again, a failed one is given up at ingest after its tries, and the clip
    leaves `needs_work` once every shot is done."""
    key = store.key_for(env["model"])
    async with env["factory"]() as db:
        clip = await _asset(db, env["clip"])
        for index in (0, 1):
            await _look_at(db, env, _shot(env, index))
        for _ in range(INGEST_TRIES):
            await store.record_failure(
                db, _shot(env, 2), model=env["model"], note="No answer.", request_id=None
            )
        await db.commit()
        assert (
            await scope.items_for(
                db, clip, key=key, redo=False, request_id=None, stills_fallback=False
            )
            == []
        )
        # A new review tries the failed shot again.
        again = await scope.items_for(
            db, clip, key=key, redo=False, request_id=uuid.uuid4(), stills_fallback=False
        )
        assert [item.span for item in again] == [WINDOWS[2]]

        await _look_at(db, env, again[0])
        in_scope = select(Asset.id).where(
            *scope.within(scope.Scope(env["library"])), scope.needs_work(key, False)
        )
        assert env["clip"] not in (await db.execute(in_scope)).scalars().all()


# --- the roll-up ---------------------------------------------------------------------


async def test_a_clip_is_looked_at_only_when_every_shot_is(env: dict) -> None:
    async with env["factory"]() as db:
        await _look_at(db, env, _shot(env, 0), score=60)
        await _look_at(db, env, _shot(env, 1), score=55)
        clip = await _asset(db, env["clip"])
        assert (clip.look_score, clip.looked_at, clip.looked_by) == (60, None, None)

        await _look_at(db, env, _shot(env, 2), score=50)
        clip = await _asset(db, env["clip"])
        assert clip.looked_at is not None
        assert clip.looked_by == store.key_for(env["model"])

        # Looked at again later: the moment it was first complete is kept.
        first = clip.looked_at
        await _look_at(db, env, _shot(env, 2), score=52, now=datetime(2026, 6, 9, tzinfo=UTC))
        assert (await _asset(db, env["clip"])).looked_at == first


async def test_the_clips_score_is_its_best_shots(env: dict) -> None:
    async with env["factory"]() as db:
        await _look_at(db, env, _shot(env, 0), score=40)
        await _look_at(db, env, _shot(env, 1), score=91)
        await _look_at(db, env, _shot(env, 2), score=91)

        clip = await _asset(db, env["clip"])
        # Of two equal shots, the earlier one speaks for the clip.
        assert (clip.look_score, clip.look_caption) == (91, "Shot scored 91.")


async def test_shots_looked_at_under_two_keys_roll_up_as_mixed(env: dict) -> None:
    async with env["factory"]() as db:
        await _look_at(db, env, _shot(env, 0))
        await _look_at(db, env, _shot(env, 1), model=env["other"])
        await _look_at(db, env, _shot(env, 2))

        clip = await _asset(db, env["clip"])
        assert clip.looked_by == store.MIXED
        assert store.MIXED not in (store.key_for(env["model"]), store.key_for(env["other"]))


async def test_a_clip_with_no_shots_is_its_look_of_the_whole(env: dict) -> None:
    async with env["factory"]() as db:
        whole = Item(env["bare"], "DJI_0002.MP4", "video", None, None, None)
        await _look_at(db, env, whole, score=33)

        bare = await _asset(db, env["bare"])
        assert (bare.look_score, bare.looked_by) == (33, store.key_for(env["model"]))
        # SQLite hands a time back without its zone.
        assert bare.looked_at is not None
        assert bare.looked_at.replace(tzinfo=UTC) == LATER


async def test_an_asset_with_no_current_look_has_no_roll_up(env: dict) -> None:
    async with env["factory"]() as db:
        await store.record_failure(
            db, _photo(env), model=env["model"], note="No picture.", request_id=None
        )
        await store.roll_up(db, env["photo"])
        await store.roll_up(db, uuid.uuid4())  # an asset deleted meanwhile: nothing to do
        await db.commit()

        photo = await _asset(db, env["photo"])
        assert (photo.look_score, photo.look_caption, photo.looked_at, photo.looked_by) == (
            None,
            None,
            None,
            None,
        )


async def test_the_look_that_speaks_for_an_asset_is_the_one_on_its_own_row(env: dict) -> None:
    """Search, culling and the grid read the score on the asset's row; the
    asset page shows a look and says whose it is. One function chooses for
    both, so the page can never name a look the row was not rolled up from."""

    async def speaks(db: AsyncSession, name: str) -> Look | None:
        asset = await _asset(db, env[name])
        best = await store.best_current(db, asset)
        on_the_row = (asset.look_score, asset.look_caption)
        assert on_the_row == ((None, None) if best is None else (best.score, best.caption))
        return best

    async with env["factory"]() as db:
        assert await speaks(db, "photo") is None
        assert await speaks(db, "clip") is None

        await _look_at(db, env, _photo(env), score=64)
        photo = await speaks(db, "photo")
        assert photo is not None and (photo.start_ms, photo.end_ms, photo.score) == (None, None, 64)

        # A clip half looked at: the best of its shots so far.
        await _look_at(db, env, _shot(env, 0), score=40)
        await _look_at(db, env, _shot(env, 1), score=91)
        best = await speaks(db, "clip")
        assert best is not None and (best.start_ms, best.end_ms) == WINDOWS[1]
        # Of two equal shots, the earlier one.
        await _look_at(db, env, _shot(env, 2), score=91)
        best = await speaks(db, "clip")
        assert best is not None and (best.start_ms, best.end_ms) == WINDOWS[1]

        # The shots are found again at other times. Before anything has
        # retired them, the looks of times no shot has any more are still
        # current — and still do not speak for the clip, whatever their score.
        await _rebuild(db, env["clip"], [WINDOWS[0], (4000, 15000)])
        await store.roll_up(db, env["clip"])
        await db.commit()
        assert len(await store.current(db, env["clip"])) == 3
        best = await speaks(db, "clip")
        assert best is not None and (best.start_ms, best.end_ms, best.score) == (*WINDOWS[0], 40)

        # A clip with no shots is its look of the whole; once it has shots,
        # they are what is looked at and that look says nothing of it.
        whole = Item(env["bare"], "DJI_0002.MP4", "video", None, None, None)
        await _look_at(db, env, whole, score=33)
        bare = await speaks(db, "bare")
        assert bare is not None and (bare.start_ms, bare.score) == (None, 33)
        await _rebuild(db, env["bare"], [(0, 5000)])
        await store.roll_up(db, env["bare"])
        await db.commit()
        assert await speaks(db, "bare") is None


# --- re-analysis ---------------------------------------------------------------------


async def _rebuild(db: AsyncSession, asset_id: uuid.UUID, windows: list[tuple[int, int]]) -> None:
    await db.execute(delete(ShotWindow).where(ShotWindow.asset_id == asset_id))
    for idx, (start, end) in enumerate(windows):
        db.add(ShotWindow(asset_id=asset_id, idx=idx, start_ms=start, end_ms=end, move="pan"))
    await db.commit()


async def test_a_shot_found_again_keeps_its_look_and_a_moved_one_is_asked_again(
    env: dict,
) -> None:
    async with env["factory"]() as db:
        looks = [(await _look_at(db, env, _shot(env, i), score=50 + i))[0] for i in range(3)]
        assert (await _asset(db, env["clip"])).looked_at is not None

        # The last shot is split in two; the first two are found at the same times.
        await _rebuild(db, env["clip"], [(0, 4000), (4000, 9000), (9000, 12000), (12000, 15000)])
        await store.after_reanalysis(db, env["clip"])
        await db.commit()

        assert set(await store.current(db, env["clip"])) == {WINDOWS[0], WINDOWS[1]}
        clip = await _asset(db, env["clip"])
        assert (clip.look_score, clip.looked_at, clip.looked_by) == (51, None, None)
        stale = await db.get(Look, looks[2].id, populate_existing=True)
        assert stale is not None and not stale.current  # kept, as the record


async def test_an_answer_that_arrives_after_its_shot_moved_is_kept_but_not_current(
    env: dict,
) -> None:
    """The clip is analysed again while the model is still answering about
    one of its old shots. `after_reanalysis` has already run, so it cannot
    retire the answer; the answer must not become the current look of a
    shot that no longer exists (search would send the operator there)."""
    async with env["factory"]() as db:
        await _rebuild(db, env["clip"], [(0, 5000), (5000, 15000)])
        await store.after_reanalysis(db, env["clip"])
        await db.commit()

        late, _ = await _look_at(db, env, _shot(env, 1), score=90)  # (4000, 9000): gone
        await db.commit()
        assert (late.ok, late.current) == (True, False)
        assert await store.current(db, env["clip"]) == {}
        assert (await _asset(db, env["clip"])).look_score is None

        # A whole-clip answer about a clip that now has shots is kept the same way.
        whole = Item(env["clip"], "DJI_0001.MP4", "video", None, None, None)
        stale_whole, _ = await _look_at(db, env, whole, score=80)
        assert stale_whole.current is False

        # An answer about a shot that is still there is current, as before.
        found = Item(env["clip"], "DJI_0001.MP4", "video", 0, 5000, None)
        kept, _ = await _look_at(db, env, found, score=70)
        assert kept.current is True
        assert (await _asset(db, env["clip"])).look_score == 70


async def test_a_look_of_the_whole_stands_only_while_a_clip_has_no_shots(env: dict) -> None:
    async with env["factory"]() as db:
        whole = Item(env["bare"], "DJI_0002.MP4", "video", None, None, None)
        await _look_at(db, env, whole, score=33)

        # Analysed again and still no shots: the look from its poster holds.
        await store.after_reanalysis(db, env["bare"])
        await db.commit()
        assert (await _asset(db, env["bare"])).look_score == 33

        # Now shots are found: they are what is looked at, and nothing is done yet.
        await _rebuild(db, env["bare"], [(0, 5000)])
        await store.after_reanalysis(db, env["bare"])
        await db.commit()
        assert await store.current(db, env["bare"]) == {}
        bare = await _asset(db, env["bare"])
        assert (bare.look_score, bare.looked_at) == (None, None)


async def test_the_motion_analysis_moves_the_looks_with_the_windows(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def analysed(db: AsyncSession, asset: Asset, *_args: Any) -> int:
        await _rebuild(db, asset.id, [(0, 4000), (4000, 15000)])
        return 2

    monkeypatch.setattr(shots, "analyse_motion", analysed)
    monkeypatch.setattr(task_module.describe_shots, "delay", lambda *_a: None)
    async with env["factory"]() as db:
        for index in range(3):
            await _look_at(db, env, _shot(env, index))
        clip = await _asset(db, env["clip"])
        library = await db.get(Library, env["library"])

        await task_module._analyze_motion(db, clip, library, Path("unused.mp4"))  # type: ignore[arg-type]

        assert set(await store.current(db, env["clip"])) == {WINDOWS[0]}
        assert (await _asset(db, env["clip"])).looked_at is None


async def test_looking_never_fails_the_motion_analysis(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[uuid.UUID] = []

    async def broken(_db: AsyncSession, asset_id: uuid.UUID, **_kwargs: Any) -> None:
        called.append(asset_id)
        raise RuntimeError("database went away")

    async def analysed(*_args: Any) -> int:
        return 0

    monkeypatch.setattr(shots, "analyse_motion", analysed)
    monkeypatch.setattr(store, "after_reanalysis", broken)
    async with env["factory"]() as db:
        clip = await _asset(db, env["clip"])
        library = await db.get(Library, env["library"])

        await task_module._analyze_motion(db, clip, library, Path("unused.mp4"))  # type: ignore[arg-type]

        assert called == [env["clip"]]
        # The session is clean for whatever follows.
        assert (await db.execute(select(func.count()).select_from(Asset))).scalar_one() == 3
