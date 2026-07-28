"""Smoke tests for the API skeleton."""

from fastapi.testclient import TestClient

from mediahub import __version__
from mediahub.main import app

client = TestClient(app)


def test_healthz() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}


def test_system_info() -> None:
    resp = client.get("/api/v1/system/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "MediaHub"
    assert body["version"] == __version__


def test_openapi_served() -> None:
    resp = client.get("/api/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "MediaHub API"
