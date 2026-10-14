"""The 2026-10 review's fixes, each pinned by the behaviour it changed.

Performance findings become tests of *what* is asked of the database or the
filesystem — how often, how much, in which order — never of how long it
takes: a timing test is flaky on CI and silent about the cause. Each test
here fails against the code as it was before the fix it names.
"""

import io
import sys
import time
import types
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import rooms as rooms_lib
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    Asset,
    AuthSession,
    Derivative,
    Face,
    Frame,
    Library,
    Listing,
    ListingItem,
    Person,
)

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _unit(index: int) -> list[float]:
    vector = [0.0] * 512
    vector[index] = 1.0
    return vector


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'review.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "review-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    get_settings.cache_clear()
    # Listing detail classifies rooms; no model here.
    monkeypatch.setattr(
        rooms_lib, "room_vectors", lambda: [_unit(i) for i in range(len(rooms_lib.ROOMS))]
    )

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

    root = tmp_path / "lib"
    root.mkdir()
    async with factory() as db:
        library = Library(name="L", root_path=str(root))
        db.add(library)
        await db.commit()
        library_id = library.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {
            "client": client,
            "app": app,
            "factory": factory,
            "library_id": library_id,
            "root": root,
            "data": tmp_path / "data",
        }
    await engine.dispose()
    get_settings.cache_clear()


def _asset(library_id: uuid.UUID, relative_path: str, **extra: Any) -> Asset:
    fields: dict[str, Any] = {
        "library_id": library_id,
        "relative_path": relative_path,
        "filename": relative_path.rpartition("/")[2],
        "extension": "jpg",
        "media_type": "image",
        "size_bytes": 1000,
        "mtime": BASE,
        "availability": "online",
    }
    fields.update(extra)
    return Asset(**fields)


def _face(frame: Frame, person: Person | None, score: float, source: str) -> Face:
    return Face(
        frame_id=frame.id,
        asset_id=frame.asset_id,
        person_id=person.id if person else None,
        box_x=0.1,
        box_y=0.1,
        box_w=0.2,
        box_h=0.2,
        detection_score=score,
        embedding=[1.0, 0.0],
        source=source,
    )


# ------------------------------------------------------------ /system/alerts


async def test_volume_probes_run_once_per_cache_window(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The banner asks on every page; the shares are asked once in a while."""
    from framefound.api.v1 import system as system_module

    probed: list[Path] = []
    real = system_module._probe_volume

    def counting(path: Path, known: int | None) -> Any:
        probed.append(path)
        return real(path, known)

    monkeypatch.setattr(system_module, "_probe_volume", counting)
    monkeypatch.setattr(system_module, "_VOLUMES_CACHE", {})
    client = env["client"]
    assert (await client.get("/api/v1/system/alerts")).status_code == 200
    first = len(probed)
    assert first >= 3, "derivatives, the system disk and the library"
    assert (await client.get("/api/v1/system/alerts")).status_code == 200
    assert (await client.get("/api/v1/system/health")).status_code == 200
    assert len(probed) == first, "inside the window nothing is probed again"


async def test_a_share_that_does_not_answer_is_reported_not_waited_for(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On 2026-10-12 one hung scandir held every request for 59 seconds."""
    from framefound.api.v1 import system as system_module

    monkeypatch.setattr(system_module, "_VOLUMES_CACHE", {})
    monkeypatch.setattr(system_module, "PROBE_TIMEOUT_S", 0.05)
    real = system_module._probe_volume

    def hung(path: Path, known: int | None) -> Any:
        if known is not None:  # the library root: a CIFS mount that stopped answering
            time.sleep(0.5)
        return real(path, known)

    monkeypatch.setattr(system_module, "_probe_volume", hung)
    started = time.monotonic()
    alerts = (await env["client"].get("/api/v1/system/alerts")).json()
    assert time.monotonic() - started < 0.4, "the banner did not wait for the share"
    assert any(a["title"] == "L is unreachable" for a in alerts), alerts
    # Health answers from the same (cached) probe; the detail says why.
    health = (await env["client"].get("/api/v1/system/health")).json()
    library = next(v for v in health["volumes"] if v["label"] == "L")
    assert library["status"] == "unreachable"
    assert "did not answer" in library["detail"]


# ---------------------------------------------------------------- sessions


async def test_the_session_row_is_rewritten_about_once_a_minute(env: dict[str, Any]) -> None:
    """Fifty thumbnails on a Browse page were fifty UPDATEs of one row."""
    client, factory = env["client"], env["factory"]
    async with factory() as db:
        first = (await db.execute(select(AuthSession))).scalars().one().expires_at
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    async with factory() as db:
        session = (await db.execute(select(AuthSession))).scalars().one()
        assert session.expires_at == first, "requests seconds apart do not write the row"
        drifted = first - timedelta(minutes=5)
        session.expires_at = drifted
        await db.commit()
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    async with factory() as db:
        renewed = (await db.execute(select(AuthSession))).scalars().one().expires_at
    assert renewed > drifted, "once it has drifted a minute, the expiry slides again"


async def test_a_readonly_account_cannot_write_but_can_sign_out(env: dict[str, Any]) -> None:
    """The role existed and nothing enforced it: 51 write routes took it."""
    from framefound.auth import service

    async with env["factory"]() as db:
        await service.create_user(db, "ro@example.com", "another-strong-password", "readonly")
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=env["app"]), base_url="http://t") as ro:
        signed_in = await ro.post(
            "/api/v1/auth/login",
            json={"email": "ro@example.com", "password": "another-strong-password"},
        )
        assert signed_in.status_code == 200, signed_in.text
        assert (await ro.get("/api/v1/listings")).status_code == 200
        denied = await ro.post(
            f"/api/v1/listings/{uuid.uuid4()}/reorder", json={"asset_ids": [str(uuid.uuid4())]}
        )
        assert denied.status_code == 403
        assert "read-only" in denied.text
        assert (await ro.post("/api/v1/auth/logout")).status_code == 204


async def test_second_factor_endpoints_are_throttled(env: dict[str, Any]) -> None:
    """A stolen cookie could guess a six-digit code at full speed."""
    client = env["client"]
    started = await client.post("/api/v1/auth/totp/start", json={"password": ADMIN["password"]})
    assert started.status_code == 200, started.text
    statuses: list[int] = []
    for _ in range(12):
        wrong = await client.post("/api/v1/auth/totp/confirm", json={"code": "000000"})
        statuses.append(wrong.status_code)
        if wrong.status_code == 429:
            break
    assert statuses[0] == 400
    assert 429 in statuses, statuses


async def test_api_docs_are_not_served_to_the_internet(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With public access on, FastAPI's docs listed every route to anyone."""
    from framefound.api import public_gate

    monkeypatch.setattr(public_gate, "CACHE_SECONDS", 0.0)
    enabled = await env["client"].put(
        "/api/v1/remote-access", json={"mode": "domain", "public_access_enabled": True}
    )
    assert enabled.status_code == 200, enabled.text
    # A public address. (203.0.113.x, the documentation range, counts as
    # private to Python's ipaddress and would have been classed as LAN.)
    internet = ASGITransport(app=env["app"], client=("8.8.8.8", 4242))
    async with AsyncClient(transport=internet, base_url="http://t") as outside:
        assert (await outside.get("/api/v1/system/info")).status_code == 200, "public access is on"
        assert (await outside.get("/api/openapi.json")).status_code == 403
        assert (await outside.get("/api/docs")).status_code == 403
    assert (await env["client"].get("/api/openapi.json")).status_code == 200, "LAN: still served"


# -------------------------------------------------------------- catalogue


async def test_nearest_assets_are_the_nearest_whatever_the_row_order(env: dict[str, Any]) -> None:
    """The query took the box's first limit×4 rows and sorted those."""
    lid = env["library_id"]
    async with env["factory"]() as db:
        for i in range(9):
            db.add(_asset(lid, f"far{i}.jpg", gps_lat=40.0 + 0.010 + i * 0.001, gps_lon=-75.0))
        db.add(_asset(lid, "nearest.jpg", gps_lat=40.0 + 0.001, gps_lon=-75.0))
        await db.commit()
    hits = (
        await env["client"].get(
            "/api/v1/assets/near", params={"lat": 40.0, "lon": -75.0, "radius_km": 5, "limit": 1}
        )
    ).json()
    assert [h["filename"] for h in hits] == ["nearest.jpg"]


async def test_people_covers_honour_the_chosen_face_and_skip_rejected(
    env: dict[str, Any],
) -> None:
    """One query for the page, and the operator's choice finally shown."""
    lid = env["library_id"]
    async with env["factory"]() as db:
        asset = _asset(lid, "faces.jpg")
        db.add(asset)
        await db.flush()
        frame = Frame(asset_id=asset.id, ts_ms=0, relative_path="f/0.jpeg")
        db.add(frame)
        await db.flush()
        chooser = Person(name="DJ", slug="dj", face_count=2)
        confirmer = Person(name="Brian", slug="brian", face_count=2)
        rejected_only = Person(name="", slug="", face_count=0)
        db.add_all([chooser, confirmer, rejected_only])
        await db.flush()
        a_strong = _face(frame, chooser, 0.95, "detected")
        a_weak = _face(frame, chooser, 0.40, "detected")
        b_guess = _face(frame, confirmer, 0.95, "detected")
        b_confirmed = _face(frame, confirmer, 0.40, "confirmed")
        c_rejected = _face(frame, rejected_only, 0.99, "rejected")
        db.add_all([a_strong, a_weak, b_guess, b_confirmed, c_rejected])
        await db.flush()
        chooser.cover_face_id = a_weak.id
        await db.commit()
        ids = {
            "chooser": str(chooser.id),
            "confirmer": str(confirmer.id),
            "rejected_only": str(rejected_only.id),
            "a_strong": str(a_strong.id),
            "a_weak": str(a_weak.id),
            "b_confirmed": str(b_confirmed.id),
        }

    async def covers() -> dict[str, str | None]:
        people = (await env["client"].get("/api/v1/people")).json()
        return {p["id"]: (p["cover"] or {}).get("face_id") for p in people}

    shown = await covers()
    assert shown[ids["chooser"]] == ids["a_weak"], "the operator's choice wins"
    assert shown[ids["confirmer"]] == ids["b_confirmed"], "confirmed before a guess"
    assert shown[ids["rejected_only"]] is None, "a rejected face is never a cover"

    async with env["factory"]() as db:
        person = await db.get(Person, uuid.UUID(ids["chooser"]))
        assert person is not None
        person.cover_face_id = None
        await db.commit()
    assert (await covers())[ids["chooser"]] == ids["a_strong"], "else the strongest detection"


async def test_similar_media_is_one_hit_per_asset_before_the_limit(env: dict[str, Any]) -> None:
    """A clip whose frames crowded the top left 'more like this' short."""
    lid = env["library_id"]
    async with env["factory"]() as db:
        seeded = {
            "ref.jpg": [[1.0, 0.0, 0.0]],
            "clip.mp4": [[0.99, 0.14, 0.0], [0.98, 0.2, 0.0], [0.97, 0.24, 0.0]],
            "other.jpg": [[0.9, 0.44, 0.0]],
            "far.jpg": [[0.0, 1.0, 0.0]],
        }
        assets = {name: _asset(lid, name) for name in seeded}
        db.add_all(assets.values())
        await db.flush()
        for name, vectors in seeded.items():
            for i, vector in enumerate(vectors):
                db.add(
                    Frame(
                        asset_id=assets[name].id,
                        ts_ms=i * 1000,
                        relative_path=f"f/{name}-{i}.jpeg",
                        embedding=vector,
                    )
                )
        await db.commit()
        ref_id = assets["ref.jpg"].id
    hits = (await env["client"].get(f"/api/v1/search/similar/{ref_id}", params={"limit": 2})).json()
    assert [h["filename"] for h in hits] == ["clip.mp4", "other.jpg"]


async def test_the_ann_search_widens_the_index_before_filtering() -> None:
    """pgvector returns hnsw.ef_search rows (40) before any WHERE applies."""
    from framefound.api.v1 import search as search_module

    statements: list[str] = []

    class Db:
        async def execute(self, statement: Any) -> None:
            statements.append(str(statement))

    await search_module._widen_ann_search(Db(), 25 * search_module.NEIGHBOUR_FRAMES)
    await search_module._widen_ann_search(Db(), 5)
    await search_module._widen_ann_search(Db(), 10_000)
    assert statements == [
        "SET LOCAL hnsw.ef_search = 200",
        "SET LOCAL hnsw.ef_search = 100",
        "SET LOCAL hnsw.ef_search = 1000",
    ]


async def test_derivatives_carry_validators_and_answer_304(env: dict[str, Any]) -> None:
    """Without an ETag every thumbnail was downloaded again after an hour."""
    lid = env["library_id"]
    asset_id = uuid.uuid4()
    rel = f"derivatives/{asset_id}/thumbnail.webp"
    target = env["data"] / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(b"webp-bytes")
    async with env["factory"]() as db:
        db.add(_asset(lid, "a.jpg", id=asset_id))
        db.add(
            Derivative(
                asset_id=asset_id,
                kind="thumbnail",
                relative_path=rel,
                media_format="webp",
                status="ready",
            )
        )
        await db.commit()
    client = env["client"]
    first = await client.get(f"/api/v1/media/{asset_id}/thumbnail")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, max-age=86400"
    assert first.headers["etag"] and first.headers["last-modified"]
    again = await client.get(
        f"/api/v1/media/{asset_id}/thumbnail", headers={"If-None-Match": first.headers["etag"]}
    )
    assert again.status_code == 304
    assert again.content == b""


# ---------------------------------------------------------------- listings


async def test_folder_assets_are_exactly_the_folders_direct_children(
    env: dict[str, Any],
) -> None:
    """A prefix match in SQL instead of 20,000 rows filtered in Python."""
    lid = env["library_id"]
    async with env["factory"]() as db:
        db.add_all(
            _asset(lid, p)
            for p in (
                "A/one.jpg",
                "A/two.jpg",
                "A/B/deep.jpg",
                "AB/other.jpg",
                "A_/under.jpg",
                "root.jpg",
            )
        )
        await db.commit()
    client = env["client"]

    async def names(path: str) -> list[str]:
        resp = await client.get(
            "/api/v1/listings/folders/assets", params={"library_id": str(lid), "path": path}
        )
        assert resp.status_code == 200, resp.text
        return [a["filename"] for a in resp.json()]

    assert await names("A") == ["one.jpg", "two.jpg"]
    assert await names("A/B") == ["deep.jpg"]
    assert await names("") == ["root.jpg"]
    assert await names("A_") == ["under.jpg"], "an underscore is a character, not a wildcard"


async def test_reorder_refuses_repeats_and_keeps_positions_contiguous(
    env: dict[str, Any],
) -> None:
    lid = env["library_id"]
    async with env["factory"]() as db:
        assets = [_asset(lid, f"g/{i}.jpg") for i in range(3)]
        db.add_all(assets)
        await db.flush()
        listing = Listing(name="Smyrna Rd")
        db.add(listing)
        await db.flush()
        db.add_all(
            ListingItem(listing_id=listing.id, asset_id=a.id, position=i)
            for i, a in enumerate(assets)
        )
        await db.commit()
        ids = [str(a.id) for a in assets]
        listing_id = listing.id
    client = env["client"]
    repeated = await client.post(
        f"/api/v1/listings/{listing_id}/reorder", json={"asset_ids": [ids[2], ids[2]]}
    )
    assert repeated.status_code == 422
    moved = await client.post(
        f"/api/v1/listings/{listing_id}/reorder", json={"asset_ids": [ids[2], str(uuid.uuid4())]}
    )
    assert moved.status_code == 200, moved.text
    async with env["factory"]() as db:
        rows = (
            await db.execute(
                select(ListingItem.asset_id, ListingItem.position)
                .where(ListingItem.listing_id == listing_id)
                .order_by(ListingItem.position)
            )
        ).all()
    assert [str(asset_id) for asset_id, _ in rows] == [ids[2], ids[0], ids[1]]
    assert [position for _, position in rows] == [0, 1, 2], "an unknown id leaves no hole"


# ------------------------------------------------------------------ editor


async def test_sky_uploads_apply_the_recipe_name_rule(env: dict[str, Any]) -> None:
    """`sky#1.jpg` could be uploaded and listed, and never chosen."""
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 200)).save(buf, "JPEG")
    client = env["client"]
    refused = await client.put("/api/v1/develop/skies/sky%231.jpg", content=buf.getvalue())
    assert refused.status_code == 400
    accepted = await client.put("/api/v1/develop/skies/dusk-01.jpg", content=buf.getvalue())
    assert accepted.status_code == 201, accepted.text


def test_the_editor_decodes_a_photograph_once_per_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every slider notch re-read and colour-managed the full original."""
    from framefound.api.v1 import develop as develop_api
    from framefound.media import develop as develop_lib

    decoded: list[int | None] = []

    def fake_open(path: Path, already_normalized: bool = False, max_edge: int | None = None) -> Any:
        decoded.append(max_edge)
        return Image.new("RGB", (8, 8), (1, 2, 3))

    monkeypatch.setattr(develop_lib, "open_for_render", fake_open)
    monkeypatch.setattr(develop_api, "_base_cache", develop_api.OrderedDict())
    asset_id = uuid.uuid4()
    first = develop_api._cached_base(asset_id, 0, tmp_path / "x.jpg", False)
    second = develop_api._cached_base(asset_id, 0, tmp_path / "x.jpg", False)
    assert decoded == [develop_api.PREVIEW_EDGE], "decoded once, at preview size"
    assert first is not second, "callers get their own copy to draw on"
    develop_api._cached_base(asset_id, 1, tmp_path / "x.jpg", False)
    assert len(decoded) == 2, "an object removal (a new base version) decodes again"


def test_sixteen_bit_grey_scales_instead_of_clipping(tmp_path: Path) -> None:
    """convert('RGB') on I;16 clipped every value above 255: a white rectangle."""
    from framefound.media.pixels import to_rgb8
    from framefound.processing.thumbnails import make_image_derivative

    assert to_rgb8(Image.new("I;16", (4, 4), 60000)).getpixel((0, 0)) == (234, 234, 234)
    narrow = Image.new("I", (2, 2), 200)  # 8-bit data in a 32-bit container
    assert to_rgb8(narrow).getpixel((0, 0)) == (200, 200, 200)

    src = tmp_path / "grey16.png"
    Image.new("I;16", (100, 80), 30000).save(src)
    dst = tmp_path / "thumb.webp"
    make_image_derivative(src, dst, max_edge=512)
    with Image.open(dst) as out:
        pixel = out.getpixel((0, 0))
        assert isinstance(pixel, tuple)
        assert 90 < pixel[0] < 140, "mid-grey, as the file is"


# ---------------------------------------------------------------- showcase


def test_prompt_vectors_are_encoded_once_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forty prompts at 35 ms each were 1.4 s of every search."""
    from framefound.ai.embeddings import EmbeddingResult
    from framefound.api.v1 import showcase as showcase_api

    class Provider:
        calls = 0

        def embed_text(self, text: str) -> EmbeddingResult:
            Provider.calls += 1
            return EmbeddingResult([1.0, 0.0], "fake")

    provider = Provider()
    monkeypatch.setattr(showcase_api, "get_embedding_provider", lambda: provider)
    showcase_api._text_vectors.clear()
    first = showcase_api._prompt_vectors("golf course", "crane", "finished")
    encoded = Provider.calls
    assert encoded > 10
    assert showcase_api._prompt_vectors("golf course", "crane", "finished") == first
    assert Provider.calls == encoded, "the second search encodes nothing"


def test_near_duplicates_are_found_against_everything_offered() -> None:
    import numpy as np

    from framefound.media import showcase as showcase_lib

    offered = [np.asarray([1.0, 0.0]), np.asarray([0.0, 1.0])]
    assert showcase_lib._near_any(np.asarray([0.0, 0.99]), offered)
    assert not showcase_lib._near_any(np.asarray([0.7, 0.7]), offered)
    assert not showcase_lib._near_any(np.asarray([1.0, 0.0]), [])


# ----------------------------------------------------------------- scanner


async def test_face_clustering_is_not_requeued_for_unchanged_faces(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production queued it 268 times a day for the same 828 singletons."""
    from framefound.scanner import __main__ as scanner

    queued: list[int] = []
    fake = types.ModuleType("framefound.processing.tasks")
    fake.cluster_unassigned_faces = types.SimpleNamespace(  # type: ignore[attr-defined]
        delay=lambda: queued.append(1)
    )
    monkeypatch.setitem(sys.modules, "framefound.processing.tasks", fake)
    lid = env["library_id"]
    async with env["factory"]() as db:
        asset = _asset(lid, "crowd.jpg")
        db.add(asset)
        await db.flush()
        frame = Frame(asset_id=asset.id, ts_ms=0, relative_path="f/c.jpeg")
        db.add(frame)
        await db.flush()
        db.add_all(_face(frame, None, 0.9, "detected") for _ in range(scanner.MIN_FACES_TO_CLUSTER))
        await db.commit()

        memo: dict[str, Any] = {}
        await scanner._cluster_new_faces(db, memo)
        await scanner._cluster_new_faces(db, memo)
        assert len(queued) == 1, "the same loose faces are not clustered again"
        db.add(_face(frame, None, 0.9, "detected"))
        await db.commit()
        await scanner._cluster_new_faces(db, memo)
        assert len(queued) == 2, "a new detection is"
        await scanner._cluster_new_faces(db)
        assert len(queued) == 3, "without the loop's memo every call still asks"


def test_the_visual_sweep_has_its_own_slower_cadence() -> None:
    from framefound.scanner import __main__ as scanner

    assert scanner.VISUALS_SWEEP_EVERY_S >= 1800
    assert timedelta(minutes=30) <= scanner.VISUALS_GRACE, "the sweep never races a fresh asset"


def test_the_dead_batch_task_is_gone() -> None:
    from framefound.processing import tasks

    assert not hasattr(tasks, "index_visual_batch")


# -------------------------------------------------------------------- Drive


def test_drive_listing_stops_paging_once_the_cap_is_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty requests to count a folder the caller was going to refuse."""
    from framefound.integrations import gdrive as gdrive_lib

    page_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_sizes.append(int(request.url.params.get("pageSize", 0)))
        if request.url.params.get("pageToken") == "p2":
            return httpx.Response(200, json={"files": [{"id": "c"}, {"id": "d"}]})
        return httpx.Response(
            200, json={"files": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"}
        )

    client = gdrive_lib.GdriveClient({}, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client, "_access_token", lambda: "token")
    assert len(client.list_images("folder", max_files=1)) == 2
    assert page_sizes == [2], "one page of cap+1 says the folder is over the cap"
    page_sizes.clear()
    assert len(client.list_images("folder")) == 4
    assert page_sizes == [1000, 1000], "without a cap, every page as before"


# ------------------------------------------------------------------- panels


class _LookalikeProvider:
    """Every text is nearest to the one indexed frame: the way CLIP's
    neighbours of a filename string crowd out the filename itself."""

    def embed_text(self, text: str) -> Any:
        from framefound.ai.embeddings import EmbeddingResult

        return EmbeddingResult([1.0, 0.0, 0.0], "fake")

    def embed_image(self, path: Path) -> Any:  # pragma: no cover
        return self.embed_text("")


async def test_panel_search_ranks_the_named_file_first_and_skips_offline(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lightroom's paths diagnostic showed another photograph's paths."""
    from framefound.ai import embeddings as embeddings_module

    monkeypatch.setattr(embeddings_module, "_provider", _LookalikeProvider())
    lid = env["library_id"]
    async with env["factory"]() as db:
        wanted = _asset(lid, "2026/DJI_0323.JPG")
        gone = _asset(lid, "2025/DJI_0323.JPG", availability="missing")
        clip = _asset(lid, "flight.mp4", media_type="video", extension="mp4")
        db.add_all([wanted, gone, clip])
        await db.flush()
        db.add(Frame(asset_id=clip.id, ts_ms=5000, relative_path="f/c.jpeg", embedding=[1, 0, 0]))
        await db.commit()
    data = (await env["client"].get("/api/v1/panel/search", params={"q": "DJI_0323"})).json()
    names = [(r["filename"], r["matched"], r["hit_ms"]) for r in data["results"]]
    assert names == [("DJI_0323.JPG", "name", None), ("flight.mp4", "visual", 5000)]
    only_images = (
        await env["client"].get(
            "/api/v1/panel/search", params={"q": "DJI_0323", "media_type": "image"}
        )
    ).json()
    assert [r["filename"] for r in only_images["results"]] == ["DJI_0323.JPG"]


async def test_panel_me_names_the_token_and_its_scopes(env: dict[str, Any]) -> None:
    client = env["client"]
    me = (await client.get("/api/v1/panel/me")).json()
    assert me["kind"] == "session" and "export" in me["scopes"]
    minted = await client.post(
        "/api/v1/auth/panel-tokens", json={"name": "Edit bay", "scopes": ["read"]}
    )
    assert minted.status_code == 201, minted.text
    async with AsyncClient(transport=ASGITransport(app=env["app"]), base_url="http://t") as panel:
        me = await panel.get(
            "/api/v1/panel/me", headers={"Authorization": f"Bearer {minted.json()['token']}"}
        )
    assert me.status_code == 200, me.text
    assert me.json()["kind"] == "token"
    assert me.json()["name"] == "Edit bay"
    assert me.json()["scopes"] == ["read"]
