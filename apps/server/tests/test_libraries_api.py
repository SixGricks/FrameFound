"""Library API tests: allowlist enforcement, CRUD, scan control."""

from pathlib import Path

from conftest import TEST_SETUP_TOKEN
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
