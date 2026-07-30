"""Tagging over HTTP, including the correction loop.

The API's job is to record judgements in a way the learner can use. The thing
most worth guarding is that a removal is stored as a *rejection* rather than a
deletion — delete the row and the same wrong suggestion returns on the next
run, which is the difference between a system that learns and one that nags.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, AssetTag, Library

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'tags.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "tags-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    # The learner runs on a worker; capture what would be queued instead.
    learned: list[str] = []
    monkeypatch.setattr(
        "framefound.api.v1.tags._relearn", lambda tag_id: learned.append(str(tag_id))
    )

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override

    async with factory() as db:
        library = Library(name="L", root_path=str(tmp_path))
        db.add(library)
        await db.flush()
        assets = []
        for name in ("broom-a.mp4", "broom-b.mp4", "green.mp4"):
            asset = Asset(
                library_id=library.id,
                relative_path=name,
                filename=name,
                extension="mp4",
                media_type="video",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            assets.append(asset)
        await db.commit()
        asset_ids = [a.id for a in assets]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client, "assets": asset_ids, "learned": learned, "factory": factory}
    await engine.dispose()
    get_settings.cache_clear()


async def test_tagging_an_asset_creates_the_tag_and_triggers_learning(env: dict) -> None:
    asset_id = env["assets"][0]
    resp = await env["client"].post(f"/api/v1/tags/assets/{asset_id}", json={"name": "Power Broom"})
    assert resp.status_code == 201
    body = resp.json()
    assert body[0]["name"] == "Power Broom"
    assert body[0]["source"] == "manual"
    # The whole point: the tag immediately teaches the system.
    assert len(env["learned"]) == 1


async def test_the_display_name_is_kept_but_lookup_is_by_slug(env: dict) -> None:
    a, b = env["assets"][0], env["assets"][1]
    await env["client"].post(f"/api/v1/tags/assets/{a}", json={"name": "Power Broom"})
    await env["client"].post(f"/api/v1/tags/assets/{b}", json={"name": "power broom"})

    tags = (await env["client"].get("/api/v1/tags")).json()
    assert len(tags) == 1, "differently-cased names must be one tag"
    assert tags[0]["name"] == "Power Broom", "the first spelling is kept"
    assert tags[0]["asset_count"] == 2


async def test_a_name_with_no_usable_characters_is_refused(env: dict) -> None:
    resp = await env["client"].post(f"/api/v1/tags/assets/{env['assets'][0]}", json={"name": "!!!"})
    assert resp.status_code == 400


async def test_tagging_an_unknown_asset_is_a_404(env: dict) -> None:
    resp = await env["client"].post(
        f"/api/v1/tags/assets/{uuid.uuid4()}", json={"name": "Power Broom"}
    )
    assert resp.status_code == 404


async def test_tagging_the_same_asset_twice_is_idempotent(env: dict) -> None:
    asset_id = env["assets"][0]
    for _ in range(3):
        await env["client"].post(f"/api/v1/tags/assets/{asset_id}", json={"name": "Bunker"})
    body = (await env["client"].get(f"/api/v1/tags/assets/{asset_id}")).json()
    assert len(body) == 1


async def test_removing_a_tag_records_a_rejection_rather_than_deleting(env: dict) -> None:
    """Delete the row and the same suggestion comes back next run. Keeping it
    as `rejected` is both a memory and a negative example."""
    asset_id = env["assets"][0]
    created = (
        await env["client"].post(f"/api/v1/tags/assets/{asset_id}", json={"name": "Power Broom"})
    ).json()
    tag_id = created[0]["tag_id"]

    resp = await env["client"].delete(f"/api/v1/tags/assets/{asset_id}/{tag_id}")
    assert resp.status_code == 204

    # Gone from the asset's view...
    assert (await env["client"].get(f"/api/v1/tags/assets/{asset_id}")).json() == []
    # ...but still on record as a rejection.
    async with env["factory"]() as db:
        link = (
            await db.execute(
                select(AssetTag).where(
                    AssetTag.asset_id == asset_id, AssetTag.tag_id == uuid.UUID(tag_id)
                )
            )
        ).scalar_one()
        assert link.source == "rejected"


async def test_accepting_a_suggestion_makes_it_a_positive_example(env: dict) -> None:
    asset_id, other = env["assets"][0], env["assets"][1]
    created = (
        await env["client"].post(f"/api/v1/tags/assets/{asset_id}", json={"name": "Power Broom"})
    ).json()
    tag_id = created[0]["tag_id"]

    async with env["factory"]() as db:
        db.add(
            AssetTag(asset_id=other, tag_id=uuid.UUID(tag_id), source="suggested", confidence=0.91)
        )
        await db.commit()

    resp = await env["client"].post(
        f"/api/v1/tags/assets/{other}/{tag_id}/decide", json={"accept": True}
    )
    assert resp.status_code == 200
    assert [t["source"] for t in resp.json()] == ["confirmed"]

    tags = (await env["client"].get("/api/v1/tags")).json()
    assert tags[0]["asset_count"] == 2, "a confirmed suggestion counts as an example"


async def test_rejecting_a_suggestion_drops_its_confidence_and_hides_it(env: dict) -> None:
    asset_id, other = env["assets"][0], env["assets"][1]
    created = (
        await env["client"].post(f"/api/v1/tags/assets/{asset_id}", json={"name": "Power Broom"})
    ).json()
    tag_id = created[0]["tag_id"]

    async with env["factory"]() as db:
        db.add(
            AssetTag(asset_id=other, tag_id=uuid.UUID(tag_id), source="suggested", confidence=0.91)
        )
        await db.commit()

    assert (
        await env["client"].post(
            f"/api/v1/tags/assets/{other}/{tag_id}/decide", json={"accept": False}
        )
    ).json() == []
    async with env["factory"]() as db:
        link = (
            await db.execute(
                select(AssetTag).where(
                    AssetTag.asset_id == other, AssetTag.tag_id == uuid.UUID(tag_id)
                )
            )
        ).scalar_one()
        assert link.source == "rejected"
        assert link.confidence is None


async def test_deciding_a_suggestion_that_does_not_exist_is_a_404(env: dict) -> None:
    resp = await env["client"].post(
        f"/api/v1/tags/assets/{env['assets'][0]}/{uuid.uuid4()}/decide", json={"accept": True}
    )
    assert resp.status_code == 404


async def test_tagging_by_hand_overrides_a_previous_rejection(env: dict) -> None:
    """The operator is the authority. Changing their mind must work, and the
    asset becomes a positive example rather than staying a negative one."""
    asset_id = env["assets"][0]
    created = (
        await env["client"].post(f"/api/v1/tags/assets/{asset_id}", json={"name": "Power Broom"})
    ).json()
    tag_id = created[0]["tag_id"]
    await env["client"].delete(f"/api/v1/tags/assets/{asset_id}/{tag_id}")

    again = (
        await env["client"].post(f"/api/v1/tags/assets/{asset_id}", json={"name": "Power Broom"})
    ).json()
    assert again[0]["source"] == "manual"


async def test_suggestions_are_listed_strongest_first(env: dict) -> None:
    anchor = env["assets"][0]
    created = (
        await env["client"].post(f"/api/v1/tags/assets/{anchor}", json={"name": "Power Broom"})
    ).json()
    tag_id = created[0]["tag_id"]

    async with env["factory"]() as db:
        for asset_id, score in ((env["assets"][1], 0.82), (env["assets"][2], 0.95)):
            db.add(
                AssetTag(
                    asset_id=asset_id,
                    tag_id=uuid.UUID(tag_id),
                    source="suggested",
                    confidence=score,
                )
            )
        await db.commit()

    pending = (await env["client"].get(f"/api/v1/tags/{tag_id}/pending")).json()
    assert [row["confidence"] for row in pending] == [0.95, 0.82]


async def test_pending_counts_appear_on_the_tag_list(env: dict) -> None:
    anchor = env["assets"][0]
    created = (
        await env["client"].post(f"/api/v1/tags/assets/{anchor}", json={"name": "Power Broom"})
    ).json()
    tag_id = created[0]["tag_id"]
    async with env["factory"]() as db:
        db.add(
            AssetTag(
                asset_id=env["assets"][1],
                tag_id=uuid.UUID(tag_id),
                source="suggested",
                confidence=0.9,
            )
        )
        await db.commit()

    tags = (await env["client"].get("/api/v1/tags")).json()
    assert tags[0]["asset_count"] == 1
    assert tags[0]["pending_count"] == 1


async def test_an_asset_shows_confirmed_tags_before_suggestions(env: dict) -> None:
    asset_id = env["assets"][0]
    confirmed = (
        await env["client"].post(f"/api/v1/tags/assets/{asset_id}", json={"name": "Bunker"})
    ).json()
    guess = (
        await env["client"].post(f"/api/v1/tags/assets/{env['assets'][1]}", json={"name": "Mower"})
    ).json()

    async with env["factory"]() as db:
        db.add(
            AssetTag(
                asset_id=asset_id,
                tag_id=uuid.UUID(guess[0]["tag_id"]),
                source="suggested",
                confidence=0.88,
            )
        )
        await db.commit()

    body = (await env["client"].get(f"/api/v1/tags/assets/{asset_id}")).json()
    assert [t["source"] for t in body] == ["manual", "suggested"]
    assert body[0]["tag_id"] == confirmed[0]["tag_id"]


async def test_relearning_an_unknown_tag_is_a_404(env: dict) -> None:
    assert (await env["client"].post(f"/api/v1/tags/{uuid.uuid4()}/relearn")).status_code == 404


async def test_tags_require_a_session(env: dict) -> None:
    await env["client"].post("/api/v1/auth/logout")
    assert (await env["client"].get("/api/v1/tags")).status_code == 401


async def test_a_tag_is_searchable(env: dict) -> None:
    """The gap this closes: a tag you cannot search for is a label, not a
    search feature."""
    asset_id = env["assets"][0]
    await env["client"].post(f"/api/v1/tags/assets/{asset_id}", json={"name": "Power Broom"})

    body = (await env["client"].get("/api/v1/search", params={"q": "power broom"})).json()
    assert len(body["tag_hits"]) == 1
    hit = body["tag_hits"][0]
    assert hit["tag_name"] == "Power Broom"
    assert hit["confirmed"] is True
    assert hit["asset_id"] == str(asset_id)


async def test_tag_search_matches_partially(env: dict) -> None:
    await env["client"].post(
        f"/api/v1/tags/assets/{env['assets'][0]}", json={"name": "Power Broom"}
    )
    body = (await env["client"].get("/api/v1/search", params={"q": "broom"})).json()
    assert len(body["tag_hits"]) == 1


async def test_search_marks_unjudged_suggestions_as_unconfirmed(env: dict) -> None:
    """A suggestion must not arrive in search results wearing the same
    authority as a decision the operator actually made."""
    anchor, other = env["assets"][0], env["assets"][1]
    created = (
        await env["client"].post(f"/api/v1/tags/assets/{anchor}", json={"name": "Power Broom"})
    ).json()
    async with env["factory"]() as db:
        db.add(
            AssetTag(
                asset_id=other,
                tag_id=uuid.UUID(created[0]["tag_id"]),
                source="suggested",
                confidence=0.9,
            )
        )
        await db.commit()

    hits = (await env["client"].get("/api/v1/search", params={"q": "broom"})).json()["tag_hits"]
    assert len(hits) == 2
    # Confirmed first, and the distinction is reported.
    assert [h["confirmed"] for h in hits] == [True, False]


async def test_a_rejected_tag_never_appears_in_search(env: dict) -> None:
    """Resurrecting a rejection in search results would be exactly the nagging
    the tagging model was built to avoid."""
    asset_id = env["assets"][0]
    created = (
        await env["client"].post(f"/api/v1/tags/assets/{asset_id}", json={"name": "Power Broom"})
    ).json()
    await env["client"].delete(f"/api/v1/tags/assets/{asset_id}/{created[0]['tag_id']}")

    body = (await env["client"].get("/api/v1/search", params={"q": "broom"})).json()
    assert body["tag_hits"] == []


async def test_browse_can_filter_to_a_tag(env: dict) -> None:
    created = (
        await env["client"].post(
            f"/api/v1/tags/assets/{env['assets'][0]}", json={"name": "Power Broom"}
        )
    ).json()
    slug = created[0]["slug"]

    page = (await env["client"].get("/api/v1/assets", params={"tag": slug})).json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == str(env["assets"][0])


async def test_the_browse_filter_excludes_unjudged_suggestions_by_default(env: dict) -> None:
    anchor, other = env["assets"][0], env["assets"][1]
    created = (
        await env["client"].post(f"/api/v1/tags/assets/{anchor}", json={"name": "Power Broom"})
    ).json()
    async with env["factory"]() as db:
        db.add(
            AssetTag(
                asset_id=other,
                tag_id=uuid.UUID(created[0]["tag_id"]),
                source="suggested",
                confidence=0.9,
            )
        )
        await db.commit()

    slug = created[0]["slug"]
    strict = (await env["client"].get("/api/v1/assets", params={"tag": slug})).json()
    assert strict["total"] == 1, "a suggestion is not a confirmed tag"

    loose = (
        await env["client"].get(
            "/api/v1/assets", params={"tag": slug, "include_suggested_tags": True}
        )
    ).json()
    assert loose["total"] == 2


async def test_an_unknown_tag_slug_returns_nothing_rather_than_everything(env: dict) -> None:
    # A filter that silently does nothing is worse than one that returns empty.
    page = (await env["client"].get("/api/v1/assets", params={"tag": "no-such-tag"})).json()
    assert page["total"] == 0
