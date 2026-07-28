"""First-run setup and login/session flow against in-memory SQLite."""

from conftest import TEST_SETUP_TOKEN
from fastapi.testclient import TestClient

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}


def do_setup(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
    assert resp.status_code == 201, resp.text


def test_setup_rejects_bad_token(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/setup", json={"setup_token": "wrong", **ADMIN})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_setup_creates_admin_and_signs_in(client: TestClient) -> None:
    do_setup(client)
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"
    assert me.json()["role"] == "admin"


def test_setup_cannot_run_twice(client: TestClient) -> None:
    do_setup(client)
    resp = client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
    assert resp.status_code == 409


def test_setup_enforces_password_length(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/setup",
        json={"setup_token": TEST_SETUP_TOKEN, "email": "a@b.com", "password": "short"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_login_logout_cycle(client: TestClient) -> None:
    do_setup(client)
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me").status_code == 401

    resp = client.post("/api/v1/auth/login", json=ADMIN)
    assert resp.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200

    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_wrong_password_is_generic_401(client: TestClient) -> None:
    do_setup(client)
    client.post("/api/v1/auth/logout")
    resp = client.post(
        "/api/v1/auth/login", json={"email": ADMIN["email"], "password": "not-the-password"}
    )
    assert resp.status_code == 401
    assert "email or password" in resp.json()["error"]["message"].lower()


def test_login_lockout_after_repeated_failures(client: TestClient) -> None:
    do_setup(client)
    client.post("/api/v1/auth/logout")
    bad = {"email": ADMIN["email"], "password": "not-the-password"}
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json=bad).status_code == 401
    locked = client.post("/api/v1/auth/login", json=bad)
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers
    # Even the CORRECT password is refused while locked (no oracle).
    assert client.post("/api/v1/auth/login", json=ADMIN).status_code == 429


def test_system_health_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/system/health").status_code == 401
    do_setup(client)
    resp = client.get("/api/v1/system/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["database"]["status"] == "ok"
    assert body["version"]
