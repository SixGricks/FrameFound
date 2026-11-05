"""Library API tests: allowlist enforcement, CRUD, scan control."""

import uuid
from pathlib import Path

from conftest import TEST_SETUP_TOKEN
from fastapi import FastAPI
from fastapi.testclient import TestClient

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}


def sign_in_admin(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
    assert resp.status_code == 201, resp.text


def make_lib_dir(tmp_path: Path, name: str = "photos") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_create_requires_auth(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/v1/libraries", json={"name": "x", "root_path": str(make_lib_dir(tmp_path))}
    )
    assert resp.status_code == 401


def test_create_and_list_library(client: TestClient, tmp_path: Path) -> None:
    sign_in_admin(client)
    lib_dir = make_lib_dir(tmp_path)
    resp = client.post(
        "/api/v1/libraries",
        json={
            "name": "Photos",
            "root_path": str(lib_dir),
            "path_mappings": [
                {"profile_name": "Edit Bay", "platform": "windows", "mapped_prefix": "Z:\\Photos"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["read_only"] is True  # the safe default

    listing = client.get("/api/v1/libraries")
    assert listing.status_code == 200
    assert [lib["name"] for lib in listing.json()] == ["Photos"]

    # Creating a library queues its initial scan automatically.
    scan = client.get(f"/api/v1/libraries/{body['id']}/scan")
    assert scan.status_code == 200
    assert scan.json()["status"] == "pending"


def test_path_outside_media_root_rejected(client: TestClient, tmp_path: Path) -> None:
    sign_in_admin(client)
    outside = str(tmp_path.parent.parent)
    resp = client.post("/api/v1/libraries", json={"name": "Evil", "root_path": outside})
    assert resp.status_code == 400
    assert "media root" in resp.json()["error"]["message"]


def test_nonexistent_path_rejected(client: TestClient, tmp_path: Path) -> None:
    sign_in_admin(client)
    resp = client.post(
        "/api/v1/libraries", json={"name": "Ghost", "root_path": str(tmp_path / "nope")}
    )
    assert resp.status_code == 400


def test_duplicate_name_rejected(client: TestClient, tmp_path: Path) -> None:
    sign_in_admin(client)
    lib_dir = make_lib_dir(tmp_path)
    assert (
        client.post(
            "/api/v1/libraries", json={"name": "Photos", "root_path": str(lib_dir)}
        ).status_code
        == 201
    )
    resp = client.post("/api/v1/libraries", json={"name": "Photos", "root_path": str(lib_dir)})
    assert resp.status_code == 409


def test_scan_trigger_conflicts_with_active(client: TestClient, tmp_path: Path) -> None:
    sign_in_admin(client)
    lib_dir = make_lib_dir(tmp_path)
    lib_id = client.post(
        "/api/v1/libraries", json={"name": "Photos", "root_path": str(lib_dir)}
    ).json()["id"]
    # The initial scan is still pending, so a second trigger conflicts.
    resp = client.post(f"/api/v1/libraries/{lib_id}/scan")
    assert resp.status_code == 409


def test_scan_cancel_then_retrigger(client: TestClient, tmp_path: Path) -> None:
    sign_in_admin(client)
    lib_dir = make_lib_dir(tmp_path)
    lib_id = client.post(
        "/api/v1/libraries", json={"name": "Photos", "root_path": str(lib_dir)}
    ).json()["id"]
    assert client.post(f"/api/v1/libraries/{lib_id}/scan/cancel").status_code == 200
    assert client.post(f"/api/v1/libraries/{lib_id}/scan").status_code == 202


def test_delete_requires_matching_confirmation(client: TestClient, tmp_path: Path) -> None:
    sign_in_admin(client)
    lib_dir = make_lib_dir(tmp_path)
    lib_id = client.post(
        "/api/v1/libraries", json={"name": "Photos", "root_path": str(lib_dir)}
    ).json()["id"]

    wrong = client.delete(f"/api/v1/libraries/{lib_id}", params={"confirm_name": "photos"})
    assert wrong.status_code == 400
    right = client.delete(f"/api/v1/libraries/{lib_id}", params={"confirm_name": "Photos"})
    assert right.status_code == 204
    assert client.get(f"/api/v1/libraries/{lib_id}").status_code == 404


def test_assets_listing_empty(client: TestClient, tmp_path: Path) -> None:
    sign_in_admin(client)
    resp = client.get("/api/v1/assets")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "page": 1, "page_size": 50}


# --- path profiles saved before the platform check ---------------------------


def _store_profiles(
    client: TestClient, app: FastAPI, library_id: str, profiles: list[tuple[str, str, str]]
) -> None:
    """Rows written straight to the table, as the API accepted them before it
    checked a prefix against its platform (and as ops/merge_libraries still
    writes them)."""
    from framefound.db.engine import get_session
    from framefound.db.models import PathMapping

    async def store() -> None:
        async for db in app.dependency_overrides[get_session]():
            for name, platform, prefix in profiles:
                db.add(
                    PathMapping(
                        library_id=uuid.UUID(library_id),
                        profile_name=name,
                        platform=platform,
                        mapped_prefix=prefix,
                    )
                )
            await db.commit()

    assert client.portal is not None
    client.portal.call(store)


def test_a_stored_profile_that_no_longer_fits_is_listed_with_a_warning(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    """The response model inherited the new check, so one such row made the
    list a 500 — and the editor, showing nothing, then saved over the rest."""
    sign_in_admin(client)
    lib_id = client.post(
        "/api/v1/libraries", json={"name": "Photos", "root_path": str(make_lib_dir(tmp_path))}
    ).json()["id"]
    _store_profiles(
        client,
        app,
        lib_id,
        [
            ("Mac Mini", "macos", "Z:\\"),
            ("Old PC", "windows", "//nas/share"),
            ("DJ", "windows", "Y:\\"),
        ],
    )

    listed = client.get(f"/api/v1/libraries/{lib_id}/path-mappings")
    assert listed.status_code == 200, listed.text
    warnings = {row["profile_name"]: row["warning"] for row in listed.json()}
    assert "absolute path" in warnings["Mac Mini"]
    assert "Windows profile" in warnings["Old PC"]
    assert warnings["DJ"] is None


def test_a_refused_profile_is_named_and_nothing_is_replaced(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    """The validator's reason only reached the 422's detail; the editor
    showed "Invalid request" with no word of which profile, or why."""
    sign_in_admin(client)
    lib_id = client.post(
        "/api/v1/libraries", json={"name": "Photos", "root_path": str(make_lib_dir(tmp_path))}
    ).json()["id"]
    _store_profiles(client, app, lib_id, [("DJ", "windows", "Y:\\")])

    refused = client.put(
        f"/api/v1/libraries/{lib_id}/path-mappings",
        json=[
            {"profile_name": "DJ", "platform": "windows", "mapped_prefix": "Y:\\"},
            {"profile_name": "Mac Mini", "platform": "macos", "mapped_prefix": "Z:\\"},
        ],
    )
    assert refused.status_code == 422
    message = refused.json()["error"]["message"]
    assert "Mac Mini" in message and "/Volumes" in message
    kept = client.get(f"/api/v1/libraries/{lib_id}/path-mappings").json()
    assert [row["profile_name"] for row in kept] == ["DJ"]

    saved = client.put(
        f"/api/v1/libraries/{lib_id}/path-mappings",
        json=[{"profile_name": "Mac Mini", "platform": "macos", "mapped_prefix": " /Volumes/X "}],
    )
    assert saved.status_code == 200, saved.text
    assert [(r["mapped_prefix"], r["warning"]) for r in saved.json()] == [("/Volumes/X", None)]
