"""Smoke tests for the API skeleton."""

from fastapi.testclient import TestClient

from framefound import __version__
from framefound.main import app

client = TestClient(app)


def test_healthz() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}


def test_system_info() -> None:
    resp = client.get("/api/v1/system/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "FrameFound"
    assert body["version"] == __version__


def test_openapi_served() -> None:
    resp = client.get("/api/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "FrameFound API"
