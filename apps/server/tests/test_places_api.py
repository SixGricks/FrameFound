"""Places and proximity search over HTTP.

Worth testing at this level rather than only as units: `/assets/near` sits in
the same router as `/assets/{asset_id}`, and a literal path registered after a
parameterised one is unreachable. Nothing below the API layer can catch that.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Library

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
JACOBS = (41.8781, -87.6298)
COLUMBIA = (41.9200, -87.7000)  # a few kilometres away: a separate job


@pytest.fixture()
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'places.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "places-test-secret")
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

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override

    async with factory() as db:
        library = Library(name="L", root_path=str(tmp_path))
        db.add(library)
        await db.flush()
        rows = [
            ("2026/Feb 4 - 513 Jacobs Rd/a.jpg", JACOBS, None, 0),
            ("2026/Feb 4 - 513 Jacobs Rd/b.jpg", JACOBS, None, 5),
            ("2026/Feb 4 - 513 Jacobs Rd/c.jpg", JACOBS, "inferred", 8),
            ("2026/Columbia Ave/d.jpg", COLUMBIA, None, 200),
        ]
        for path, (lat, lon), source, minutes in rows:
            db.add(
                Asset(
                    library_id=library.id,
                    relative_path=path,
                    filename=path.rpartition("/")[2],
                    extension="jpg",
                    media_type="image",
                    size_bytes=1000,
                    mtime=BASE,
                    captured_at=BASE + timedelta(minutes=minutes),
                    gps_lat=lat,
                    gps_lon=lon,
                    gps_source=source,
                    availability="online",
                )
            )
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (
            await c.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield c
    await engine.dispose()
    get_settings.cache_clear()


async def test_the_near_route_is_not_shadowed_by_the_asset_id_route(
    client: AsyncClient,
) -> None:
    """`/assets/near` must reach its handler, not be parsed as a UUID."""
    resp = await client.get(
        "/api/v1/assets/near", params={"lat": JACOBS[0], "lon": JACOBS[1], "radius_km": 1}
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


async def test_nearby_assets_come_back_closest_first(client: AsyncClient) -> None:
    body = (
        await client.get(
            "/api/v1/assets/near",
            params={"lat": JACOBS[0], "lon": JACOBS[1], "radius_km": 2},
        )
    ).json()
    assert len(body) == 3  # the Columbia Ave job is out of range
    distances = [row["distance_km"] for row in body]
    assert distances == sorted(distances)


async def test_places_groups_a_shoot_and_names_it_from_the_folder(
    client: AsyncClient,
) -> None:
    body = (await client.get("/api/v1/places")).json()
    assert [p["name"] for p in body] == ["Feb 4 - 513 Jacobs Rd", "Columbia Ave"]
    biggest = body[0]
    assert biggest["asset_count"] == 3
    assert biggest["inferred_count"] == 1
    assert biggest["first_captured_at"] < biggest["last_captured_at"]


async def test_places_can_exclude_inferred_positions(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/places", params={"include_inferred": False})).json()
    biggest = next(p for p in body if p["name"] == "Feb 4 - 513 Jacobs Rd")
    assert biggest["asset_count"] == 2
    assert biggest["inferred_count"] == 0


async def test_a_wide_radius_merges_the_two_jobs(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/places", params={"radius_km": 20})).json()
    assert len(body) == 1
    assert body[0]["asset_count"] == 4


async def test_places_requires_a_session(client: AsyncClient) -> None:
    await client.post("/api/v1/auth/logout")
    assert (await client.get("/api/v1/places")).status_code == 401


async def test_an_unknown_asset_id_still_returns_not_found(client: AsyncClient) -> None:
    """Guards the fix: moving `/near` up must not break the parameterised
    route that used to shadow it."""
    resp = await client.get(f"/api/v1/assets/{uuid.uuid4()}")
    assert resp.status_code == 404
