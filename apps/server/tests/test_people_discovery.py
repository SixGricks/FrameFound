"""Finding more of a person, and reviewing a queue without clicking every face.

Two properties matter more than the search quality itself:

- **A suggestion must not move a face.** Searching the whole catalogue means
  reaching into clusters that belong to nobody yet. If saying "no" left the
  face attached to the person it is not, the operator would be destroying data
  by reviewing carefully — the exact opposite of what review is for.
- **A bulk confirmation must mean what the operator saw.** Confirming "down to
  here" on a ranked grid has to settle the faces below the fold too, or the
  page silently does a fraction of what it said.
"""

import math
import uuid as uuidlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import people as people_lib
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Face, Frame, Library, Person

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _vec(angle: float) -> list[float]:
    """A unit vector. Cosine similarity between two is cos(difference)."""
    vector = [0.0] * 512
    vector[0], vector[1] = math.cos(angle), math.sin(angle)
    return vector


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'discover.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "discovery-secret")
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

    ids: dict[str, str] = {}
    async with factory() as db:
        library = Library(name="L", root_path=str(tmp_path))
        db.add(library)
        await db.flush()

        known = Person(name="Stef Grick", slug="stef-grick", face_count=0)
        stranger = Person(name="", slug="", face_count=0)
        other_named = Person(name="Someone Else", slug="someone-else", face_count=0)
        db.add_all([known, stranger, other_named])
        await db.flush()

        counter = 0

        async def add_face(person: Person | None, angle: float, source: str) -> Face:
            nonlocal counter
            counter += 1
            asset = Asset(
                library_id=library.id,
                relative_path=f"p{counter}.jpg",
                filename=f"p{counter}.jpg",
                extension="jpg",
                media_type="image",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            frame = Frame(asset_id=asset.id, ts_ms=0, relative_path=f"f/{counter}.jpeg")
            db.add(frame)
            await db.flush()
            face = Face(
                frame_id=frame.id,
                asset_id=asset.id,
                person_id=person.id if person else None,
                box_x=0.1,
                box_y=0.1,
                box_w=0.2,
                box_h=0.2,
                detection_score=0.9,
                embedding=_vec(angle),
                source=source,
            )
            db.add(face)
            await db.flush()
            return face

        # Two faces the operator has already agreed to: the prototype.
        for i in range(2):
            await add_face(known, 0.02 * i, "confirmed")

        # A near-match sitting loose, and one inside an unnamed cluster.
        loose = await add_face(None, 0.10, "detected")
        clustered = await add_face(stranger, 0.12, "detected")
        # Someone else entirely, and a face already claimed by a named person.
        far = await add_face(None, 1.4, "detected")
        claimed = await add_face(other_named, 0.05, "confirmed")

        ids["loose"] = str(loose.id)
        ids["clustered"] = str(clustered.id)
        ids["far"] = str(far.id)
        ids["claimed"] = str(claimed.id)
        ids["cluster"] = str(stranger.id)

        # What confirming those two faces would have left behind.
        known.prototype = people_lib.prototype_for([_vec(0.0), _vec(0.02)])
        known.face_count = 2
        other_named.prototype = people_lib.prototype_for([_vec(0.05)])
        await db.commit()
        ids["person"] = str(known.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client, "ids": ids, "factory": factory}
    await engine.dispose()
    get_settings.cache_clear()


async def _discover(env: dict) -> dict:
    resp = await env["client"].post(f"/api/v1/people/{env['ids']['person']}/discover")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_discovery_reaches_loose_faces_and_unnamed_clusters(env: dict) -> None:
    """The whole point: clustering only ever compared a face to the group it
    landed in, so the near-misses it got wrong are sitting in other groups."""
    body = await _discover(env)
    assert body["found"] == 2, "the loose face and the one in the unnamed cluster"

    resp = await env["client"].get(f"/api/v1/people/{env['ids']['person']}/suggestions")
    offered = {f["face_id"] for f in resp.json()}
    assert offered == {env["ids"]["loose"], env["ids"]["clustered"]}
    assert env["ids"]["far"] not in offered, "a stranger is not a near miss"
    assert env["ids"]["claimed"] not in offered, "already somebody else's answer"


async def test_suggestions_come_back_best_match_first(env: dict) -> None:
    """Ranking is what makes a bulk review possible — an unranked grid has no
    boundary to draw a line at."""
    await _discover(env)
    faces = (await env["client"].get(f"/api/v1/people/{env['ids']['person']}/suggestions")).json()
    scores = [f["similarity"] for f in faces]
    assert scores == sorted(scores, reverse=True)
    assert faces[0]["face_id"] == env["ids"]["loose"], "0.10 rad is closer than 0.12"


async def test_rejecting_a_suggestion_leaves_the_face_where_it_was(env: dict) -> None:
    """The property the whole suggestion model exists to protect."""
    await _discover(env)
    ids = env["ids"]
    resp = await env["client"].post(
        f"/api/v1/people/{ids['person']}/suggestions/reject",
        json={"face_ids": [ids["clustered"], ids["loose"]]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"rejected": 2}

    async with env["factory"]() as db:
        clustered = await db.get(Face, uuidlib.UUID(ids["clustered"]))
        loose = await db.get(Face, uuidlib.UUID(ids["loose"]))
        assert clustered is not None and loose is not None
        assert str(clustered.person_id) == ids["cluster"], "still in its own cluster"
        assert loose.person_id is None, "still nobody's"
        assert clustered.source == "detected", "its own review is untouched"


async def test_a_refused_face_is_never_offered_again(env: dict) -> None:
    """A threshold is a heuristic; a rejection is a guarantee. Re-offering is
    the nagging that teaches an operator to stop trusting the button."""
    ids = env["ids"]
    await _discover(env)
    await env["client"].post(
        f"/api/v1/people/{ids['person']}/suggestions/reject",
        json={"face_ids": [ids["loose"]]},
    )
    again = await _discover(env)
    offered = {
        f["face_id"]
        for f in (await env["client"].get(f"/api/v1/people/{ids['person']}/suggestions")).json()
    }
    assert ids["loose"] not in offered
    assert again["found"] <= 1


async def test_accepting_a_suggestion_moves_the_face_and_teaches_the_prototype(
    env: dict,
) -> None:
    ids = env["ids"]
    await _discover(env)
    async with env["factory"]() as db:
        start = await db.get(Person, uuidlib.UUID(ids["person"]))
        assert start is not None
        before = start.face_count

    resp = await env["client"].post(
        f"/api/v1/people/{ids['person']}/suggestions/accept",
        json={"face_ids": [ids["clustered"]]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}

    async with env["factory"]() as db:
        face = await db.get(Face, uuidlib.UUID(ids["clustered"]))
        assert face is not None
        assert str(face.person_id) == ids["person"], "accepting is what moves it"
        assert face.source == "confirmed"
        assert face.suggested_person_id is None, "the question is answered"
        person = await db.get(Person, uuidlib.UUID(ids["person"]))
        assert person is not None
        assert person.face_count == before + 1, "learned from the agreement"


async def test_forgetting_a_person_does_not_delete_faces_merely_suggested(env: dict) -> None:
    """The FK is SET NULL for this reason. CASCADE would make 'forget this
    person' quietly destroy hundreds of faces belonging to other people."""
    ids = env["ids"]
    await _discover(env)
    resp = await env["client"].delete(f"/api/v1/people/{ids['person']}")
    assert resp.status_code == 204

    async with env["factory"]() as db:
        survivor = await db.get(Face, uuidlib.UUID(ids["clustered"]))
        assert survivor is not None, "a suggestion is an opinion, not ownership"
        assert str(survivor.person_id) == ids["cluster"]


async def test_confirm_above_settles_the_whole_queue_not_just_one_page(env: dict) -> None:
    """The operator draws a line on a ranked grid; everything above it is them,
    including the faces the page never loaded."""
    ids = env["ids"]
    async with env["factory"]() as db:
        # Give the person a review queue with a clear boundary in it.
        rows = (
            (await db.execute(select(Face).where(Face.person_id == uuidlib.UUID(ids["person"]))))
            .scalars()
            .all()
        )
        for i, face in enumerate(rows):
            face.source = "detected"
            face.similarity = 0.9 if i == 0 else 0.5
        await db.commit()

    resp = await env["client"].post(
        f"/api/v1/people/{ids['person']}/confirm-above", json={"min_similarity": 0.8}
    )
    assert resp.status_code == 200
    assert resp.json() == {"confirmed": 1}, "only what cleared the bar"

    resp = await env["client"].post(
        f"/api/v1/people/{ids['person']}/confirm-above", json={"min_similarity": 0.4}
    )
    assert resp.json() == {"confirmed": 1}, "the rest, and nothing twice"


async def test_learning_rescores_the_queue_so_it_can_be_ranked(env: dict) -> None:
    """A face that joined when the cluster was named was never compared to
    anyone, so its similarity is NULL and the review queue has nothing to sort
    by — which is what forced the operator through it one face at a time."""
    ids = env["ids"]
    async with env["factory"]() as db:
        face = await db.get(Face, uuidlib.UUID(ids["clustered"]))
        assert face is not None
        assert face.similarity is None, "precondition: unscored"

    await _discover(env)
    await env["client"].post(
        f"/api/v1/people/{ids['person']}/suggestions/accept",
        json={"face_ids": [ids["clustered"]]},
    )

    async with env["factory"]() as db:
        scored = await db.get(Face, uuidlib.UUID(ids["clustered"]))
        assert scored is not None
        assert scored.similarity is not None, "learning must leave the queue rankable"
        assert scored.similarity > 0.9, "0.12 rad from the prototype"

        # And every other face of this person, not only the one just accepted.
        rest = (
            (
                await db.execute(
                    select(Face.similarity).where(
                        Face.person_id == uuidlib.UUID(ids["person"]),
                        Face.source == "confirmed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert all(s is not None for s in rest), "a stale score is as bad as none"


async def test_a_bulk_judgement_must_say_which_kind_it_is(env: dict) -> None:
    """Ids and a threshold mean different things; sending both, or neither, is
    a caller bug and confirming the wrong set is unrecoverable by hand."""
    for payload in ({}, {"face_ids": [env["ids"]["loose"]], "min_similarity": 0.5}):
        resp = await env["client"].post(
            f"/api/v1/people/{env['ids']['person']}/confirm-above", json=payload
        )
        assert resp.status_code == 422, payload


async def test_discovery_refuses_before_it_has_anything_to_match_against(env: dict) -> None:
    async with env["factory"]() as db:
        cluster = await db.get(Person, uuidlib.UUID(env["ids"]["cluster"]))
        assert cluster is not None
        cluster.name = "Freshly Named"
        await db.commit()
    resp = await env["client"].post(f"/api/v1/people/{env['ids']['cluster']}/discover")
    assert resp.status_code == 400
    assert "confirm" in resp.json()["error"]["message"].lower()
