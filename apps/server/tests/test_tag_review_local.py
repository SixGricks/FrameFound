"""A local model's suggestions in the review queue Claude's reviewer uses.

They go through the same `write_proposals`, so the same rules hold — a
rejection stays rejected, an answered tag is left alone — and they carry two
things more: the look that proposed them and, on a clip, the moment. A tag
suggested for several shots of one clip is one row that follows the
strongest guess. And a picture only a local model has guessed at is still
one Claude can review.

tests/test_tag_review.py covers the Claude route and is left unedited.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import tag_review
from framefound.ai.tag_review import ORIGIN_LOCAL, TagProposal
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, AssetTag, Derivative, Library, Look, Tag
from framefound.looking import prompt
from framefound.looking.prompt import LookAnswer

BASE = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'local.db').as_posix()}"
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
        ids: list[uuid.UUID] = []
        for index, name in enumerate(("Drone/DJI_0001.MP4", "Drone/DJI_0002.MP4")):
            asset = Asset(
                library_id=library.id,
                relative_path=name,
                filename=name.rsplit("/", 1)[-1],
                extension="mp4",
                media_type="video",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
                first_indexed_at=datetime(2026, 6, 1 + index, tzinfo=UTC),
            )
            db.add(asset)
            await db.flush()
            ids.append(asset.id)
            db.add(
                Derivative(
                    asset_id=asset.id,
                    kind="thumbnail",
                    relative_path=f"derivatives/{asset.id}/thumbnail.webp",
                    media_format="webp",
                    status="ready",
                )
            )
        looks = []
        for start in (0, 4000, 9000):
            look = Look(asset_id=ids[0], start_ms=start, end_ms=start + 4000, key="k")
            db.add(look)
            looks.append(look)
        db.add(Tag(name="Barn", slug="barn"))
        await db.commit()
        yield {"factory": factory, "ids": ids, "looks": [look.id for look in looks]}
    await engine.dispose()
    get_settings.cache_clear()


def _p(name: str, confidence: float | None, existing: bool = True) -> TagProposal:
    return TagProposal(name=name, confidence=confidence, existing=existing)


async def _link(db: AsyncSession, asset_id: uuid.UUID, slug: str) -> AssetTag:
    return (
        await db.execute(
            select(AssetTag)
            .join(Tag, Tag.id == AssetTag.tag_id)
            .where(AssetTag.asset_id == asset_id, Tag.slug == slug)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_a_local_suggestion_remembers_its_look_and_its_moment(env: dict) -> None:
    clip, look = env["ids"][0], env["looks"][1]
    async with env["factory"]() as db:
        outcomes = await tag_review.write_proposals(
            db,
            clip,
            [_p("barn", 0.8), _p("silo", 0.9, existing=False)],
            origin=ORIGIN_LOCAL,
            look_id=look,
            at_ms=6000,
        )
        await db.commit()

        assert outcomes == {
            "Barn": tag_review.OUTCOME_SUGGESTED,
            "silo": tag_review.OUTCOME_NEW_TAG,
        }
        for slug in ("barn", "silo"):
            row = await _link(db, clip, slug)
            assert (row.source, row.origin, row.look_id, row.at_ms) == (
                "suggested",
                "local",
                look,
                6000,
            ), "a suggestion, never a fact, pointing at the shot that proposed it"


async def test_claudes_suggestions_are_written_as_they_always_were(env: dict) -> None:
    async with env["factory"]() as db:
        await tag_review.write_proposals(db, env["ids"][0], [_p("barn", 0.8)], origin="claude")
        await db.commit()
        row = await _link(db, env["ids"][0], "barn")

    assert (row.origin, row.confidence, row.look_id, row.at_ms) == ("claude", 0.8, None, None)


async def test_a_stronger_shot_takes_over_a_pending_suggestion(env: dict) -> None:
    """One clip, one tag, several shots: the Tags page should open on the
    shot the model was surest of, and the queue should hold one row."""
    clip, (first, second, third) = env["ids"][0], env["looks"]
    async with env["factory"]() as db:
        await tag_review.write_proposals(
            db, clip, [_p("barn", 0.7)], origin=ORIGIN_LOCAL, look_id=first, at_ms=2000
        )
        stronger = await tag_review.write_proposals(
            db,
            clip,
            [_p("barn", 0.9)],
            origin=ORIGIN_LOCAL,
            look_id=second,
            at_ms=6000,
            strengthen=True,
        )
        weaker = await tag_review.write_proposals(
            db,
            clip,
            [_p("barn", 0.9), _p("Barn", 0.65)],
            origin=ORIGIN_LOCAL,
            look_id=third,
            at_ms=11000,
            strengthen=True,
        )
        await db.commit()

        assert stronger == {"Barn": tag_review.OUTCOME_STRENGTHENED}
        assert weaker == {"Barn": tag_review.OUTCOME_PENDING}, "as strong, or weaker: no change"
        rows = (await db.execute(select(AssetTag).where(AssetTag.asset_id == clip))).scalars()
        assert [(r.source, r.confidence, r.look_id, r.at_ms) for r in rows] == [
            ("suggested", 0.9, second, 6000)
        ]


async def test_without_strengthen_a_pending_suggestion_is_left_as_it_was(env: dict) -> None:
    clip, (first, second, _third) = env["ids"][0], env["looks"]
    async with env["factory"]() as db:
        await tag_review.write_proposals(
            db, clip, [_p("barn", 0.6)], origin=ORIGIN_LOCAL, look_id=first, at_ms=2000
        )
        again = await tag_review.write_proposals(
            db, clip, [_p("barn", 0.95)], origin=ORIGIN_LOCAL, look_id=second, at_ms=6000
        )
        await db.commit()
        row = await _link(db, clip, "barn")

    assert again == {"Barn": tag_review.OUTCOME_PENDING}
    assert (row.confidence, row.look_id, row.at_ms) == (0.6, first, 2000)


@pytest.mark.parametrize(
    ("source", "origin", "outcome"),
    [
        ("suggested", "claude", tag_review.OUTCOME_PENDING),
        ("suggested", "clip", tag_review.OUTCOME_PENDING),
        ("suggested", None, tag_review.OUTCOME_PENDING),
        ("rejected", "local", tag_review.OUTCOME_REJECTED),
        ("confirmed", "local", tag_review.OUTCOME_ALREADY),
        ("manual", None, tag_review.OUTCOME_ALREADY),
    ],
)
async def test_strengthening_never_touches_another_opinion_or_a_decision(
    env: dict, source: str, origin: str | None, outcome: str
) -> None:
    """A local look does not overwrite Claude's or the learner's number, and
    a tag the operator has answered — even one a local model first
    suggested — stays answered."""
    clip, look = env["ids"][0], env["looks"][1]
    async with env["factory"]() as db:
        barn = (await db.execute(select(Tag).where(Tag.slug == "barn"))).scalar_one()
        db.add(
            AssetTag(asset_id=clip, tag_id=barn.id, source=source, confidence=0.2, origin=origin)
        )
        await db.commit()
        outcomes = await tag_review.write_proposals(
            db,
            clip,
            [_p("barn", 0.99)],
            origin=ORIGIN_LOCAL,
            look_id=look,
            at_ms=6000,
            strengthen=True,
        )
        await db.commit()
        row = await _link(db, clip, "barn")

    assert outcomes == {"Barn": outcome}
    assert (row.source, row.origin, row.confidence, row.look_id, row.at_ms) == (
        source,
        origin,
        0.2,
        None,
        None,
    )


async def test_a_guess_with_no_number_strengthens_nothing(env: dict) -> None:
    clip, (first, second, _third) = env["ids"][0], env["looks"]
    async with env["factory"]() as db:
        await tag_review.write_proposals(
            db, clip, [_p("barn", 0.7)], origin=ORIGIN_LOCAL, look_id=first, at_ms=2000
        )
        outcomes = await tag_review.write_proposals(
            db, clip, [_p("barn", None)], origin=ORIGIN_LOCAL, look_id=second, strengthen=True
        )
        await db.commit()
        row = await _link(db, clip, "barn")

    assert outcomes == {"Barn": tag_review.OUTCOME_PENDING}
    assert (row.confidence, row.look_id) == (0.7, first)


async def test_a_local_guess_leaves_the_picture_in_claudes_pool(env: dict) -> None:
    clip, other = env["ids"]
    async with env["factory"]() as db:
        assert set(await tag_review.pick_untagged(db, 10)) == {clip, other}
        await tag_review.write_proposals(
            db, clip, [_p("barn", 0.9)], origin=ORIGIN_LOCAL, look_id=env["looks"][0], at_ms=2000
        )
        await db.commit()

        assert set(await tag_review.pick_untagged(db, 10)) == {clip, other}
        assert await tag_review.count_untagged(db) == 2

        # Once the operator answers it, it is their opinion like any other.
        (await _link(db, clip, "barn")).source = "confirmed"
        await db.commit()
        assert await tag_review.pick_untagged(db, 10) == [other]
        assert await tag_review.count_untagged(db) == 1


async def test_an_answer_reaches_the_queue_only_as_admitted_suggestions(env: dict) -> None:
    """End to end on the pure half and the write: a reworded match and a
    weak guess never become rows; what is admitted arrives as suggestions."""
    answer = LookAnswer.model_validate(
        {
            "caption": "A barn from the air.",
            "score": 81,
            "reason": "Level and sharp.",
            "matches": [{"tag": "barn", "confidence": 0.9}, {"tag": "barns", "confidence": 0.8}],
            "new_tags": [{"tag": "Silo", "confidence": 0.95}, {"tag": "fence", "confidence": 0.5}],
        }
    )
    cleaned = prompt.clean(answer, {"barn"}, new_tags=True)
    clip = env["ids"][0]
    async with env["factory"]() as db:
        await tag_review.write_proposals(
            db, clip, cleaned.proposals, origin=ORIGIN_LOCAL, look_id=env["looks"][0], at_ms=2000
        )
        await db.commit()
        rows = (
            await db.execute(
                select(Tag.slug, AssetTag.source, AssetTag.origin)
                .join(AssetTag, AssetTag.tag_id == Tag.id)
                .where(AssetTag.asset_id == clip)
                .order_by(Tag.slug)
            )
        ).all()
        slugs = set((await db.execute(select(Tag.slug))).scalars())

    assert [tuple(row) for row in rows] == [
        ("barn", "suggested", "local"),
        ("silo", "suggested", "local"),
    ]
    assert slugs == {"barn", "silo"}, "no tag was made for a guess that was not admitted"
    assert cleaned.dropped == 2
