"""Visual search with a deterministic fake embedding provider.

The real CLIP model is exercised on the deployment host; here the concern is
the plumbing: vectors round-trip, similarity ranks correctly, an unindexed
library degrades gracefully, and 'similar' never returns the source asset.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import embeddings as embeddings_module
from framefound.ai.embeddings import EmbeddingResult
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Frame, Library

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}

# Three orthogonal directions stand in for distinct visual concepts.
BARN = [1.0, 0.0, 0.0]
POND = [0.0, 1.0, 0.0]
CROWD = [0.0, 0.0, 1.0]


class FakeProvider:
    """Maps known words to fixed vectors so ranking is deterministic."""

    def embed_text(self, text: str) -> EmbeddingResult:
        lowered = text.lower()
        if "barn" in lowered:
            return EmbeddingResult(BARN, "fake")
        if "pond" in lowered:
            return EmbeddingResult(POND, "fake")
        return EmbeddingResult(CROWD, "fake")

    def embed_image(self, path: Path) -> EmbeddingResult:  # pragma: no cover
        return EmbeddingResult(BARN, "fake")


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "visual-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(embeddings_module, "_provider", FakeProvider())

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override

    ids: dict[str, uuid.UUID] = {}
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    async with factory() as db:
        library = Library(name="L", root_path=str(lib_root))
        db.add(library)
        await db.flush()
        for name, vector in (("barn.jpg", BARN), ("pond.jpg", POND), ("crowd.jpg", CROWD)):
            asset = Asset(
                library_id=library.id,
                relative_path=name,
                filename=name,
                extension="jpg",
                media_type="image",
                size_bytes=10,
                mtime=datetime.now(UTC),
            )
            db.add(asset)
            await db.flush()
            ids[name] = asset.id
            db.add(
                Frame(
                    asset_id=asset.id,
                    ts_ms=0,
                    relative_path=f"frames/{name}.jpeg",
                    embedding=vector,
                    embedding_model="fake",
                )
            )
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client, "ids": ids, "factory": factory}
    await engine.dispose()
    get_settings.cache_clear()


async def test_semantic_query_ranks_by_meaning(env: dict) -> None:
    resp = await env["client"].get("/api/v1/search", params={"q": "red barn at sunset"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["visual_available"] is True
    # The barn image must lead even though the word "barn" is only in the
    # filename by coincidence — this is the vector, not the text, matching.
    assert body["visual_hits"][0]["filename"] == "barn.jpg"
    assert body["visual_hits"][0]["similarity"] == pytest.approx(1.0, abs=1e-3)


async def test_different_query_selects_different_asset(env: dict) -> None:
    body = (await env["client"].get("/api/v1/search", params={"q": "a pond"})).json()
    assert body["visual_hits"][0]["filename"] == "pond.jpg"


async def test_similar_excludes_the_source_asset(env: dict) -> None:
    barn_id = env["ids"]["barn.jpg"]
    resp = await env["client"].get(f"/api/v1/search/similar/{barn_id}")
    assert resp.status_code == 200
    returned = {hit["asset_id"] for hit in resp.json()}
    assert str(barn_id) not in returned
    assert len(returned) == 2


async def test_similar_404_when_not_indexed(env: dict) -> None:
    resp = await env["client"].get(f"/api/v1/search/similar/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_visual_unavailable_when_nothing_indexed(env: dict) -> None:
    from sqlalchemy import update

    async with env["factory"]() as db:
        await db.execute(update(Frame).values(embedding=None))
        await db.commit()
    body = (await env["client"].get("/api/v1/search", params={"q": "barn"})).json()
    assert body["visual_available"] is False
    assert body["visual_hits"] == []
    # Text search must still work with no vectors at all.
    assert len(body["filename_hits"]) >= 1
