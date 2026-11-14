"""What a local model made of an asset, where Browse reads it (L3·S2).

`GET /assets` carries each item's score, caption and whether it is done,
and can put the best first; a folder level counts what has nothing left to
look at. Without the new parameter every list is what it was, plus three
empty fields.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.api.v1 import assets as assets_api
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Library

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
LOOKED = datetime(2026, 11, 12, 22, 14, 3, tzinfo=UTC)

# (path, media type, score, caption, looked at, captured hours after BASE)
SHOOT: tuple[tuple[str, str, int | None, str | None, datetime | None, int], ...] = (
    ("Pete Dye/Drone/orbit.MP4", "video", 82, "A slow orbit at golden hour.", LOOKED, 1),
    # A clip with one shot still to look at: an opinion, not done.
    ("Pete Dye/Drone/pass.MP4", "video", 61, "A fast low pass.", None, 2),
    ("Pete Dye/Drone/RAW/still.DNG", "image", 82, "The ninth green.", LOOKED, 3),
    ("Pete Dye/Drone/RAW/blur.JPG", "image", 12, "Soft and tilted.", LOOKED, 4),
    ("Pete Dye/Drone/RAW/new.JPG", "image", None, None, None, 5),
    # Audio is never looked at; a stray value on one is not counted.
    ("Pete Dye/Interview/take.WAV", "audio", None, None, LOOKED, 6),
    ("Pete Dye/hole9.JPG", "image", None, None, None, 7),
)


@pytest.fixture()
async def browse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'looks.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "looks-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    engine = create_async_engine(url)
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
    async with factory() as db:
        gelco = Library(name="GELCO", root_path=str(tmp_path / "GELCO"))
        db.add(gelco)
        await db.flush()
        for relative, media_type, score, caption, looked_at, hours in SHOOT:
            asset = _asset(
                gelco.id,
                relative,
                media_type,
                look_score=score,
                look_caption=caption,
                look_reason="Steady." if score is not None else None,
                looked_at=looked_at,
                looked_by="mlx-community/demo@abc#look-1" if looked_at else None,
                captured_at=BASE + timedelta(hours=hours),
            )
            db.add(asset)
            await db.flush()
            ids[relative] = asset.id
        # Premiere's proxy of a looked-at clip: out of every count.
        db.add(
            _asset(
                gelco.id,
                "Pete Dye/Drone/Proxies/orbit_Proxy.MP4",
                "video",
                proxy_of_id=ids["Pete Dye/Drone/orbit.MP4"],
                look_score=99,
                looked_at=LOOKED,
            )
        )
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        assert (
            await http.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"http": http, "library_id": gelco.id, "ids": ids}
    await engine.dispose()
    get_settings.cache_clear()


def _asset(library_id: uuid.UUID, relative: str, media_type: str, **extra: Any) -> Asset:
    return Asset(
        library_id=library_id,
        relative_path=relative,
        filename=relative.rsplit("/", 1)[-1],
        extension=relative.rsplit(".", 1)[-1].lower(),
        media_type=media_type,
        size_bytes=1,
        mtime=BASE,
        **extra,
    )


async def _list(browse: dict[str, Any], **params: Any) -> Any:
    response = await browse["http"].get(
        "/api/v1/assets",
        params={"library_id": str(browse["library_id"]), "page_size": 200, **params},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_each_item_carries_what_the_model_made_of_it_and_nothing_for_the_rest(
    browse: dict[str, Any],
) -> None:
    items = {item["relative_path"]: item for item in (await _list(browse))["items"]}
    orbit = items["Pete Dye/Drone/orbit.MP4"]
    assert (orbit["look_score"], orbit["look_caption"]) == (82, "A slow orbit at golden hour.")
    assert orbit["looked_at"].startswith("2026-11-12T22:14:03")
    # An opinion on part of a clip is shown; "done" is not claimed for it.
    passing = items["Pete Dye/Drone/pass.MP4"]
    assert (passing["look_score"], passing["looked_at"]) == (61, None)
    untouched = items["Pete Dye/hole9.JPG"]
    assert (untouched["look_score"], untouched["look_caption"], untouched["looked_at"]) == (
        None,
        None,
        None,
    )
    # The roll-up's other columns are the asset page's, not the grid's.
    assert "look_reason" not in orbit and "looked_by" not in orbit


async def test_sorting_by_score_puts_the_best_first_and_what_nobody_looked_at_last(
    browse: dict[str, Any],
) -> None:
    order = [item["relative_path"] for item in (await _list(browse, sort="score"))["items"]]
    assert order == [
        # Two at 82: the newer capture first.
        "Pete Dye/Drone/RAW/still.DNG",
        "Pete Dye/Drone/orbit.MP4",
        "Pete Dye/Drone/pass.MP4",
        "Pete Dye/Drone/RAW/blur.JPG",
        # No opinion is not the worst opinion: last, newest first.
        "Pete Dye/hole9.JPG",
        "Pete Dye/Interview/take.WAV",
        "Pete Dye/Drone/RAW/new.JPG",
    ]


async def test_sorting_by_score_pages_without_repeating_or_skipping(
    browse: dict[str, Any],
) -> None:
    whole = [item["id"] for item in (await _list(browse, sort="score"))["items"]]
    paged: list[str] = []
    for page in (1, 2, 3, 4):
        body = await _list(browse, sort="score", page=page, page_size=2)
        assert body["total"] == len(SHOOT)
        paged += [item["id"] for item in body["items"]]
    assert paged == whole


async def test_the_other_orders_are_what_they_were(browse: dict[str, Any]) -> None:
    by_name = [item["filename"] for item in (await _list(browse, sort="name"))["items"]]
    assert by_name == sorted(by_name)
    refused = await browse["http"].get("/api/v1/assets", params={"sort": "best"})
    assert refused.status_code == 422


async def test_a_folder_counts_what_has_nothing_left_to_look_at(browse: dict[str, Any]) -> None:
    response = await browse["http"].get(
        "/api/v1/assets/folders",
        params={"library_id": str(browse["library_id"]), "path": "Pete Dye"},
    )
    assert response.status_code == 200, response.text
    level = response.json()
    by_name = {row["name"]: row for row in level["folders"]}
    # Drone: the finished clip and two photographs. Not the clip with a shot
    # still to do, not the new photograph, not the paired proxy.
    assert (by_name["Drone"]["items"], by_name["Drone"]["looked"]) == (5, 3)
    assert by_name["Interview"]["looked"] == 0
    assert level["inside"] == {"items": 7, "photos": 4, "clips": 2, "looked": 3}

    raw = (
        await browse["http"].get(
            "/api/v1/assets/folders",
            params={"library_id": str(browse["library_id"]), "path": "Pete Dye/Drone"},
        )
    ).json()
    assert {row["name"]: row["looked"] for row in raw["folders"]} == {"RAW": 2}
    assert raw["inside"]["looked"] == 3


def test_the_looked_count_reads_the_same_on_postgresql() -> None:
    sql = str(
        assets_api.folder_level_query(uuid.uuid4(), "Pete Dye", "postgresql").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "assets.looked_at IS NOT NULL" in sql
    assert "sum(anon_1.looked)" in sql
    assert "GROUP BY anon_1.name" in sql


def test_unscored_items_go_last_on_postgresql_too() -> None:
    """SQLite puts NULLs last in a descending order by itself; PostgreSQL
    puts them first, so the order has to say so."""
    sql = " ".join(
        str(part.compile(dialect=postgresql.dialect())) for part in assets_api.SORTS["score"]
    )
    assert sql == "assets.look_score DESC NULLS LAST assets.captured_at DESC NULLS LAST"
