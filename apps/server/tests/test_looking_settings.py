"""Looking's own settings (framefound/looking/settings_store.py) and its numbers.

One `app_settings` row an administrator edits on the AI models page: may
the Looker propose new tags, and how many pictures it is shown per shot.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound import looking
from framefound.db.base import Base
from framefound.db.models import AppSetting
from framefound.looking import settings_store
from framefound.looking.settings_store import LookingConfig, load_config, save_config


@pytest.fixture()
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 's.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def stored_as(db: AsyncSession, value: Any) -> LookingConfig:
    db.add(AppSetting(key=settings_store.CONFIG_KEY, value=value))
    await db.commit()
    db.expunge_all()
    return await load_config(db)


async def test_nothing_saved_reads_as_new_tags_allowed_and_three_pictures(
    db: AsyncSession,
) -> None:
    assert await load_config(db) == LookingConfig(new_tags=True, stills_per_shot=3)


async def test_what_is_saved_is_what_is_read_back(db: AsyncSession) -> None:
    await save_config(db, LookingConfig(new_tags=False, stills_per_shot=1))
    db.expunge_all()
    assert await load_config(db) == LookingConfig(new_tags=False, stills_per_shot=1)
    # Saved again over the same row, not beside it.
    await save_config(db, LookingConfig(new_tags=True, stills_per_shot=3))
    db.expunge_all()
    assert await load_config(db) == LookingConfig()


async def test_a_change_saved_on_the_page_reaches_a_review_that_kept_its_session(
    tmp_path: Path,
) -> None:
    """A review reads the settings before each item on the one session it
    keeps for its whole run; the page saves through its own. Sessions do not
    expire on commit, so while anything in the review's session still holds
    the row (here `held`), a read served from that copy would keep the
    review on the old settings until it ended."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'two.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as page:
            await save_config(page, LookingConfig(new_tags=True, stills_per_shot=3))
        async with factory() as review:
            assert await load_config(review) == LookingConfig()
            held = await review.get(AppSetting, settings_store.CONFIG_KEY)
            async with factory() as page:
                await save_config(page, LookingConfig(new_tags=False, stills_per_shot=1))
            assert await load_config(review) == LookingConfig(new_tags=False, stills_per_shot=1)
            assert held is not None
    finally:
        await engine.dispose()


async def test_a_row_from_a_newer_version_still_loads(db: AsyncSession) -> None:
    config = await stored_as(db, {"new_tags": False, "stills_per_shot": 1, "later_field": 7})
    assert config == LookingConfig(new_tags=False, stills_per_shot=1)


@pytest.mark.parametrize(
    "value",
    [
        {"new_tags": "no", "stills_per_shot": 2},
        {"new_tags": 1, "stills_per_shot": "3"},
        {"stills_per_shot": True},
        ["not", "a", "mapping"],
    ],
)
async def test_a_value_that_cannot_be_read_falls_back_rather_than_stopping_reviews(
    db: AsyncSession, value: Any
) -> None:
    assert await stored_as(db, value) == LookingConfig()


@pytest.mark.parametrize(
    ("config", "words"),
    [
        (LookingConfig(stills_per_shot=2), "3 pictures"),
        (LookingConfig(stills_per_shot=True), "3 pictures"),
        (LookingConfig(new_tags="yes"), "new tags"),  # type: ignore[arg-type]
    ],
)
async def test_a_value_the_page_cannot_offer_is_refused_and_nothing_is_saved(
    db: AsyncSession, config: LookingConfig, words: str
) -> None:
    with pytest.raises(ValueError, match=words):
        await save_config(db, config)
    assert await db.get(AppSetting, settings_store.CONFIG_KEY) is None


def test_a_local_suggestion_is_marked_with_the_word_the_tag_reviewer_uses() -> None:
    """Both modules name the origin. The tag reviewer gains its own
    constant with the step that teaches it the origin; until then this
    reads the word, and from then on it holds the two equal."""
    from framefound.ai import tag_review

    assert looking.ORIGIN_LOCAL == "local"
    assert getattr(tag_review, "ORIGIN_LOCAL", looking.ORIGIN_LOCAL) == looking.ORIGIN_LOCAL
    assert looking.ORIGIN_LOCAL not in {
        tag_review.ORIGIN_CLIP,
        tag_review.ORIGIN_CLAUDE,
        tag_review.ORIGIN_MCP,
    }


def test_a_review_runs_ahead_of_looking_at_what_arrived() -> None:
    # Lower runs first on L1's lane; a person's Test is 0.
    assert 0 < looking.PRIORITY_REVIEW < looking.PRIORITY_INGEST
    assert looking.KINDS == ("look_folder", "look_ingest", "look_prepare")
    # Search is nudged, never decided, by a score.
    assert 0 < looking.LOOK_NUDGE <= 0.10
