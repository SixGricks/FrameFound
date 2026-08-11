"""The Drive organizer: propose, apply, manifest, undo — against a fake Drive.

The fake is an httpx.MockTransport pretending to be Google, so the real
client runs its whole code path — JWT assertion, token exchange, list,
rename, multipart manifest upload — with nothing leaving the process. The
service-account key is a real RSA key generated per test run, which keeps
the signing code honest too.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import rooms as rooms_lib
from framefound.ai.embeddings import EmbeddingResult, EmbeddingUnavailable
from framefound.api.v1 import gdrive as gdrive_api
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import AppSetting, Asset, AuditLog, Frame, Library
from framefound.integrations import gdrive as gdrive_lib
from framefound.integrations.organize import folder_tokens, original_stem, proposed_name

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
FOLDER_ID = "FOLDER123abc"


def _unit(index: int) -> list[float]:
    vector = [0.0] * 512
    vector[index] = 1.0
    return vector


FAKE_ROOM_VECTORS = [_unit(i) for i in range(len(rooms_lib.ROOMS))]
ROOM_INDEX = {room.key: i for i, room in enumerate(rooms_lib.ROOMS)}


def _service_account_json() -> str:
    """A syntactically real key file: generated RSA key, fake identity."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "test-project",
            "private_key_id": "kid123",
            "private_key": pem,
            "client_email": "framefound@test-project.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


def _fake_drive(state: dict[str, Any]) -> httpx.MockTransport:
    """Google, reduced to a dict. state: files {id: name}, mimes, thumbs,
    manifest (last uploaded payload)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "oauth2.googleapis.com":
            assertion = dict(
                pair.split("=", 1) for pair in request.content.decode().split("&")
            ).get("assertion", "")
            assert assertion.count(".") == 2, "expected a signed JWT assertion"
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if url.host == "lh3.googleusercontent.com":
            assert "=s768" in str(url), "thumbnail should be requested at 768px"
            return httpx.Response(200, content=b"\xff\xd8not-really-a-jpeg")
        if url.host == "www.googleapis.com" and url.path == "/upload/drive/v3/files":
            text = request.content.decode()
            manifest_json = text.split("Content-Type: application/json\r\n\r\n")[-1]
            payload = json.loads(manifest_json.split("\r\n--")[0])
            new_id = f"manifest{len(state['files'])}"
            state["files"][new_id] = gdrive_lib.MANIFEST_NAME
            state["mimes"][new_id] = "application/json"
            state["manifest"] = payload
            state["manifest_id"] = new_id
            return httpx.Response(200, json={"id": new_id})
        if url.host != "www.googleapis.com":
            return httpx.Response(500)
        if url.path == "/drive/v3/files":
            q = url.params.get("q", "")
            if "name = '" in q:
                wanted = q.split("name = '")[1].split("'")[0].replace("\\'", "'")
                found = [
                    {"id": fid, "name": name}
                    for fid, name in state["files"].items()
                    if name == wanted
                ]
                return httpx.Response(200, json={"files": found})
            listing = []
            for fid, name in state["files"].items():
                if not state["mimes"].get(fid, "image/jpeg").startswith("image/"):
                    continue
                entry: dict[str, Any] = {"id": fid, "name": name}
                if fid in state["thumbs"]:
                    entry["thumbnailLink"] = state["thumbs"][fid]
                listing.append(entry)
            return httpx.Response(200, json={"files": listing})
        file_id = url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and url.params.get("alt") == "media":
            if file_id == state.get("manifest_id"):
                return httpx.Response(200, json=state["manifest"])
            return httpx.Response(404)
        if request.method == "GET":
            if file_id == FOLDER_ID:
                return httpx.Response(
                    200,
                    json={
                        "id": FOLDER_ID,
                        "name": "09-22 - 1505 W Kings Hwy Gap",
                        "mimeType": "application/vnd.google-apps.folder",
                    },
                )
            if file_id in state["files"]:
                return httpx.Response(
                    200,
                    json={
                        "id": file_id,
                        "name": state["files"][file_id],
                        "mimeType": state["mimes"].get(file_id, "image/jpeg"),
                    },
                )
            return httpx.Response(404)
        if request.method == "PATCH":
            if file_id not in state["files"]:
                return httpx.Response(404)
            state["files"][file_id] = json.loads(request.content)["name"]
            return httpx.Response(200, json={"id": file_id})
        if request.method == "DELETE":
            if state["files"].pop(file_id, None) is None:
                return httpx.Response(404)
            state["mimes"].pop(file_id, None)
            return httpx.Response(204)
        return httpx.Response(500)

    return httpx.MockTransport(handler)


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'gdrive.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "gdrive-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    get_settings.cache_clear()
    monkeypatch.setattr(rooms_lib, "room_vectors", lambda: FAKE_ROOM_VECTORS)

    state: dict[str, Any] = {
        # Two shoot files the catalogue knows, one stranger with a thumbnail,
        # one stranger without.
        "files": {
            "file0001": "IMG_0001.jpg",
            "file0002": "IMG_0002.jpg",
            "file0003": "DJI_0100.jpg",
            "file0004": "DSC_9999.jpg",
        },
        "mimes": {},
        "thumbs": {"file0003": "https://lh3.googleusercontent.com/thumb0003=s220"},
        "manifest": None,
        "manifest_id": None,
    }
    monkeypatch.setattr(gdrive_api, "_transport", _fake_drive(state))

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
        library = Library(name="L", root_path=str(tmp_path / "media"))
        db.add(library)
        await db.flush()

        async def add_asset(relative_path: str, room_key: str) -> None:
            name = relative_path.rsplit("/", 1)[-1]
            asset = Asset(
                library_id=library.id,
                relative_path=relative_path,
                filename=name,
                extension=name.rsplit(".", 1)[-1],
                media_type="image",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            frame = Frame(asset_id=asset.id, ts_ms=0, relative_path=f"f/{name}.jpeg")
            frame.embedding = _unit(ROOM_INDEX[room_key])
            db.add(frame)

        await add_asset("09-22 - 1505 W Kings Hwy Gap/MLS/IMG_0001.jpg", "kitchen")
        await add_asset("09-22 - 1505 W Kings Hwy Gap/MLS/IMG_0002.jpg", "front_exterior")
        # Same stem in an unrelated shoot: token overlap must break the tie.
        await add_asset("07-04 - 200 Elm St/IMG_0001.jpg", "bedroom")
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client, "factory": factory, "state": state}
    await engine.dispose()
    get_settings.cache_clear()


class _FakeProvider:
    """Thumbnail-path stand-in: every Drive thumbnail embeds as an aerial."""

    def embed_image(self, path: Path) -> EmbeddingResult:
        assert path.read_bytes().startswith(b"\xff\xd8")
        return EmbeddingResult(_unit(ROOM_INDEX["aerial"]), "fake")


async def _configure(env: dict) -> None:
    resp = await env["client"].put(
        "/api/v1/gdrive/settings", json={"service_account_json": _service_account_json()}
    )
    assert resp.status_code == 200, resp.text


# --- helpers ---------------------------------------------------------------


def test_original_stem_strips_a_previous_sort_prefix() -> None:
    assert original_stem("IMG_1731.jpg") == ("IMG_1731", ".jpg")
    assert original_stem("07 - Kitchen - IMG_1731.jpg") == ("IMG_1731", ".jpg")
    assert original_stem("19 - Hunting land - DJI_0238_D.JPG") == ("DJI_0238_D", ".JPG")
    assert original_stem("no-extension") == ("no-extension", "")


def test_proposed_name_is_the_demo_format() -> None:
    assert proposed_name(7, "Kitchen", "IMG_1731", ".JPG") == "07 - Kitchen - IMG_1731.jpg"


def test_folder_tokens_keep_address_words() -> None:
    tokens = folder_tokens("09-22 - 1505 W Kings Hwy Gap")
    assert {"1505", "kings", "hwy", "gap"} <= tokens
    assert "w" not in tokens


def test_parse_folder_id_accepts_urls_and_bare_ids() -> None:
    want = "1qbYRL5GapgZ"
    for given in (
        want,
        f"https://drive.google.com/drive/folders/{want}?usp=sharing",
        f"https://drive.google.com/drive/u/0/folders/{want}",
        f"https://drive.google.com/open?id={want}&foo=1",
    ):
        assert gdrive_lib.parse_folder_id(given) == want
    with pytest.raises(gdrive_lib.GdriveError):
        gdrive_lib.parse_folder_id("not a link at all")


def test_sign_jwt_produces_a_three_part_rs256_token() -> None:
    import base64

    sa = json.loads(_service_account_json())
    token = gdrive_lib.sign_jwt(sa, now=1_700_000_000)
    header_b64, claims_b64, signature = token.split(".")

    def unb64(part: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

    assert unb64(header_b64) == {"alg": "RS256", "typ": "JWT", "kid": "kid123"}
    claims = unb64(claims_b64)
    assert claims["iss"] == sa["client_email"]
    assert claims["exp"] - claims["iat"] == 3600
    assert len(signature) > 300  # a 2048-bit signature, not an empty string


# --- settings --------------------------------------------------------------


async def test_settings_seal_the_key_and_expose_only_the_email(env: dict) -> None:
    before = (await env["client"].get("/api/v1/gdrive/settings")).json()
    assert before == {"configured": False, "enabled": True, "client_email": ""}

    await _configure(env)
    after = (await env["client"].get("/api/v1/gdrive/settings")).json()
    assert after["configured"] is True
    assert after["client_email"] == "framefound@test-project.iam.gserviceaccount.com"

    async with env["factory"]() as db:
        row = await db.get(AppSetting, "gdrive")
        stored = str(row.value)
        assert "PRIVATE KEY" not in stored, "key must be sealed at rest"
        assert row.value["client_email"].endswith("gserviceaccount.com")

    cleared = await env["client"].put(
        "/api/v1/gdrive/settings", json={"service_account_json": ""}
    )
    assert cleared.json() == {"configured": False, "enabled": True, "client_email": ""}


async def test_settings_reject_a_non_key_paste(env: dict) -> None:
    for bad in ("not json", json.dumps({"client_email": "x@y.z"})):
        resp = await env["client"].put(
            "/api/v1/gdrive/settings", json={"service_account_json": bad}
        )
        assert resp.status_code == 400


async def test_preview_without_configuration_is_a_polite_503(env: dict) -> None:
    resp = await env["client"].post("/api/v1/gdrive/organize/preview", json={"folder": FOLDER_ID})
    assert resp.status_code == 503
    assert "Security page" in resp.json()["error"]["message"]


# --- preview ---------------------------------------------------------------


async def test_preview_classifies_catalogue_first_in_walkthrough_order(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure(env)
    monkeypatch.setattr(gdrive_api, "get_embedding_provider", lambda: _FakeProvider())

    resp = await env["client"].post("/api/v1/gdrive/organize/preview", json={"folder": FOLDER_ID})
    assert resp.status_code == 200, resp.text
    preview = resp.json()

    assert preview["folder_name"] == "09-22 - 1505 W Kings Hwy Gap"
    assert preview["total"] == 4
    # Canonical order: front exterior, aerial, kitchen. The Elm St bedroom
    # embedding shares the IMG_0001 stem but loses on address tokens.
    assert [r["new_name"] for r in preview["renames"]] == [
        "01 - Front exterior - IMG_0002.jpg",
        "02 - Aerial - DJI_0100.jpg",
        "03 - Kitchen - IMG_0001.jpg",
    ]
    assert [r["source"] for r in preview["renames"]] == ["catalogue", "thumbnail", "catalogue"]
    assert preview["from_catalogue"] == 2
    assert preview["from_thumbnail"] == 1
    assert [s["name"] for s in preview["skipped"]] == ["DSC_9999.jpg"]
    assert preview["skipped"][0]["reason"] == "no thumbnail"


async def test_preview_degrades_when_the_ai_runtime_is_missing(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure(env)

    def unavailable() -> None:
        raise EmbeddingUnavailable("no onnxruntime on this host")

    monkeypatch.setattr(gdrive_api, "get_embedding_provider", unavailable)
    resp = await env["client"].post("/api/v1/gdrive/organize/preview", json={"folder": FOLDER_ID})
    assert resp.status_code == 200
    preview = resp.json()
    # Catalogue matches still classify; only the thumbnail path is lost.
    assert preview["from_catalogue"] == 2
    assert preview["from_thumbnail"] == 0
    assert len(preview["skipped"]) == 2


# --- apply / undo ----------------------------------------------------------


async def test_apply_renames_writes_manifest_and_undo_restores(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure(env)
    monkeypatch.setattr(gdrive_api, "get_embedding_provider", lambda: _FakeProvider())
    client, state = env["client"], env["state"]

    preview = (
        await client.post("/api/v1/gdrive/organize/preview", json={"folder": FOLDER_ID})
    ).json()
    resp = await client.post(
        "/api/v1/gdrive/organize/apply",
        json={"folder": FOLDER_ID, "renames": preview["renames"]},
    )
    assert resp.status_code == 200, resp.text
    applied = resp.json()
    assert applied["renamed"] == 3
    assert applied["failed"] == []
    assert applied["manifest_file_id"]

    assert state["files"]["file0002"] == "01 - Front exterior - IMG_0002.jpg"
    assert state["files"]["file0003"] == "02 - Aerial - DJI_0100.jpg"
    assert state["files"]["file0001"] == "03 - Kitchen - IMG_0001.jpg"
    assert state["files"]["file0004"] == "DSC_9999.jpg", "unclassified files stay untouched"
    assert state["manifest"]["renames"][0]["old_name"] == "IMG_0002.jpg"

    async with env["factory"]() as db:
        events = (await db.execute(select(AuditLog.event))).scalars().all()
        assert "gdrive.organized" in events

    # A second preview on the renamed folder strips the prefixes back to the
    # true stems: same plan, no stacking.
    again = (
        await client.post("/api/v1/gdrive/organize/preview", json={"folder": FOLDER_ID})
    ).json()
    assert [r["new_name"] for r in again["renames"]] == [
        r["new_name"] for r in preview["renames"]
    ]

    undo = await client.post("/api/v1/gdrive/organize/undo", json={"folder": FOLDER_ID})
    assert undo.status_code == 200, undo.text
    assert undo.json() == {"restored": 3, "failed": []}
    assert state["files"]["file0001"] == "IMG_0001.jpg"
    assert state["files"]["file0002"] == "IMG_0002.jpg"
    assert state["files"]["file0003"] == "DJI_0100.jpg"
    assert gdrive_lib.MANIFEST_NAME not in state["files"].values(), "manifest cleaned up"


async def test_undo_without_a_manifest_is_a_404(env: dict) -> None:
    await _configure(env)
    resp = await env["client"].post("/api/v1/gdrive/organize/undo", json={"folder": FOLDER_ID})
    assert resp.status_code == 404


async def test_apply_refuses_path_like_names(env: dict) -> None:
    await _configure(env)
    resp = await env["client"].post(
        "/api/v1/gdrive/organize/apply",
        json={
            "folder": FOLDER_ID,
            "renames": [
                {"file_id": "file0001", "old_name": "IMG_0001.jpg", "new_name": "../escape.jpg"}
            ],
        },
    )
    assert resp.status_code == 400
