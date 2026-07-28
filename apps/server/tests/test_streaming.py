"""Byte-range streaming behavior (what <video> seeking depends on)."""

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

from framefound.media.streaming import range_file_response

CONTENT = bytes(range(256)) * 40  # 10,240 bytes, distinguishable positions


@pytest.fixture()
def media_client(tmp_path: Path) -> TestClient:
    file_path = tmp_path / "clip.bin"
    file_path.write_bytes(CONTENT)
    app = FastAPI()

    @app.get("/file")
    def serve(request: Request) -> Response:
        return range_file_response(request, file_path, "application/octet-stream")

    return TestClient(app)


def test_full_file_without_range(media_client: TestClient) -> None:
    resp = media_client.get("/file")
    assert resp.status_code == 200
    assert resp.content == CONTENT
    assert resp.headers["accept-ranges"] == "bytes"


def test_partial_range(media_client: TestClient) -> None:
    resp = media_client.get("/file", headers={"Range": "bytes=100-199"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[100:200]
    assert resp.headers["content-range"] == f"bytes 100-199/{len(CONTENT)}"
    assert resp.headers["content-length"] == "100"


def test_open_ended_range(media_client: TestClient) -> None:
    resp = media_client.get("/file", headers={"Range": "bytes=10000-"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[10000:]


def test_suffix_range(media_client: TestClient) -> None:
    resp = media_client.get("/file", headers={"Range": "bytes=-100"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[-100:]


def test_out_of_bounds_range(media_client: TestClient) -> None:
    resp = media_client.get("/file", headers={"Range": "bytes=999999-"})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(CONTENT)}"


def test_end_clamped_to_size(media_client: TestClient) -> None:
    resp = media_client.get("/file", headers={"Range": "bytes=10200-999999"})
    assert resp.status_code == 206
    assert resp.content == CONTENT[10200:]
