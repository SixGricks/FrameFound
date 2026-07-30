"""The face review flow.

The operator chose confirm-before-it-counts, so these tests are mostly about
what must NOT happen: nothing attributed without agreement, and a rejection
that sticks. A face database that is confidently wrong is worse than one that
asks.
"""

import math
import uuid as uuidlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Face, Frame, Library, Person

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _vec(seed: float) -> list[float]:
    vector = [0.0] * 512
    vector[0], vector[1] = math.cos(seed), math.sin(seed)
    return vector


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'people.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "people-secret")
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

    async with factory() as db:
        library = Library(name="L", root_path=str(tmp_path))
        db.add(library)
        await db.flush()
        person = Person(name="", slug="", face_count=0)
        db.add(person)
        await db.flush()

        face_ids: list[str] = []
        for i in range(3):
            asset = Asset(
                library_id=library.id,
                relative_path=f"p{i}.jpg",
                filename=f"p{i}.jpg",
                extension="jpg",
                media_type="image",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            frame = Frame(asset_id=asset.id, ts_ms=0, relative_path=f"f/{i}.jpeg")
            db.add(frame)
            await db.flush()
            face = Face(
                frame_id=frame.id,
                asset_id=asset.id,
                person_id=person.id,
                box_x=0.1,
                box_y=0.1,
                box_w=0.2,
                box_h=0.2,
                detection_score=0.9,
                embedding=_vec(0.05 * i),
                source="detected",
            )
            db.add(face)
            await db.flush()
            face_ids.append(str(face.id))
        await db.commit()
        person_id = str(person.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {
            "client": client,
            "person_id": person_id,
            "faces": face_ids,
            "factory": factory,
        }
    await engine.dispose()
    get_settings.cache_clear()


async def test_an_unnamed_cluster_is_listed_as_work_to_do(env: dict) -> None:
    body = (await env["client"].get("/api/v1/people")).json()
    assert len(body) == 1
    assert body[0]["named"] is False
    assert body[0]["name"] == "Unnamed person", "never invent a name"
    assert body[0]["pending_count"] == 3
    assert body[0]["confirmed_count"] == 0, "nothing counts before agreement"


async def test_naming_a_cluster_confirms_the_faces_in_it(env: dict) -> None:
    """Naming is the operator looking at the group and saying who it is. Asking
    them to then tick each face is asking the same question twice."""
    resp = await env["client"].put(
        f"/api/v1/people/{env['person_id']}/name", json={"name": "Brian Kelly"}
    )
    assert resp.status_code == 200
    assert resp.json()["named"] is True
    assert resp.json()["slug"] == "brian-kelly"

    detail = (await env["client"].get(f"/api/v1/people/{env['person_id']}")).json()
    assert detail["confirmed_count"] == 3
    assert detail["pending_count"] == 0


async def test_two_people_cannot_share_a_name(env: dict) -> None:
    await env["client"].put(f"/api/v1/people/{env['person_id']}/name", json={"name": "Brian Kelly"})
    async with env["factory"]() as db:
        other = Person(name="", slug="")
        db.add(other)
        await db.commit()
        other_id = str(other.id)

    resp = await env["client"].put(f"/api/v1/people/{other_id}/name", json={"name": "brian kelly"})
    assert resp.status_code == 409


async def test_rejecting_a_face_keeps_the_pair_so_it_is_not_offered_again(env: dict) -> None:
    """The face keeps its person_id while rejected. That looks odd and is the
    point: the pair is the judgement, and clearing it would lose which person
    was ruled out."""
    await env["client"].put(f"/api/v1/people/{env['person_id']}/name", json={"name": "Brian"})
    resp = await env["client"].post(
        f"/api/v1/people/{env['person_id']}/reject",
        json={"face_ids": [env["faces"][0]]},
    )
    assert resp.json()["rejected"] == 1

    rejected = (
        await env["client"].get(f"/api/v1/people/{env['person_id']}", params={"source": "rejected"})
    ).json()
    assert len(rejected["faces"]) == 1
    assert rejected["faces"][0]["face_id"] == env["faces"][0]


async def test_a_rejected_face_stops_counting_toward_the_person(env: dict) -> None:
    await env["client"].put(f"/api/v1/people/{env['person_id']}/name", json={"name": "Brian"})
    await env["client"].post(
        f"/api/v1/people/{env['person_id']}/reject", json={"face_ids": [env["faces"][0]]}
    )
    listing = (await env["client"].get("/api/v1/people")).json()
    assert listing[0]["confirmed_count"] == 2


async def test_confirming_a_suggestion_counts_it(env: dict) -> None:
    async with env["factory"]() as db:
        await db.execute(sql_update(Face).values(source="detected"))
        await db.commit()

    resp = await env["client"].post(
        f"/api/v1/people/{env['person_id']}/confirm",
        json={"face_ids": [env["faces"][0], env["faces"][1]]},
    )
    assert resp.json()["confirmed"] == 2


async def test_merging_moves_faces_and_removes_the_absorbed_cluster(env: dict) -> None:
    """Clustering splits one person whenever a haircut moves them far enough in
    embedding space, so merging is the commonest correction there is."""
    async with env["factory"]() as db:
        other = Person(name="", slug="")
        db.add(other)
        await db.flush()
        await db.execute(
            sql_update(Face)
            .where(Face.id == uuidlib.UUID(env["faces"][2]))
            .values(person_id=other.id)
        )
        await db.commit()
        other_id = str(other.id)

    resp = await env["client"].post(f"/api/v1/people/{env['person_id']}/merge/{other_id}")
    assert resp.json()["faces_moved"] == 1
    assert (await env["client"].get(f"/api/v1/people/{other_id}")).status_code == 404


async def test_a_person_cannot_be_merged_into_themselves(env: dict) -> None:
    resp = await env["client"].post(f"/api/v1/people/{env['person_id']}/merge/{env['person_id']}")
    assert resp.status_code == 400


async def test_forgetting_a_person_removes_the_face_vectors_too(env: dict) -> None:
    """Someone asking to be removed from a face database is entitled to
    actually be removed. A soft delete would be a lie about that."""
    await env["client"].delete(f"/api/v1/people/{env['person_id']}")
    assert (await env["client"].get(f"/api/v1/people/{env['person_id']}")).status_code == 404

    async with env["factory"]() as db:
        remaining = (await db.execute(select(func.count()).select_from(Face))).scalar_one()
    assert remaining == 0, "the embeddings must be gone, not just unlinked"


async def test_face_recognition_is_on_by_default(env: dict) -> None:
    body = (await env["client"].get("/api/v1/people/settings/current")).json()
    assert body["enabled"] is True
    assert body["unnamed_clusters"] == 1


async def test_turning_it_off_does_not_delete_the_names(env: dict) -> None:
    """Losing named people on a toggle would be its own bug. Purging is a
    separate, explicit choice."""
    await env["client"].put(f"/api/v1/people/{env['person_id']}/name", json={"name": "Brian"})
    await env["client"].put("/api/v1/people/settings/current", json={"enabled": False})

    settings = (await env["client"].get("/api/v1/people/settings/current")).json()
    assert settings["enabled"] is False
    assert settings["people_count"] == 1
    assert (await env["client"].get("/api/v1/people")).json()[0]["name"] == "Brian"


async def test_a_crop_box_is_returned_rather_than_an_image(env: dict) -> None:
    """No crop is stored or served: the frame is already available, and a second
    copy of everyone's face would double the most sensitive data here."""
    body = (await env["client"].get(f"/api/v1/people/faces/{env['faces'][0]}/crop-box")).json()
    assert 0 <= body["box_x"] <= 1
    assert 0 <= body["box_w"] <= 1
    assert body["frame_id"]


async def test_people_requires_a_session(env: dict) -> None:
    await env["client"].post("/api/v1/auth/logout")
    assert (await env["client"].get("/api/v1/people")).status_code == 401
