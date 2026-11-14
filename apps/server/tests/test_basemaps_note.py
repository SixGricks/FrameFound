"""Where the Basemaps page says the maps are kept.

It said "served straight out of your data directory" whatever Storage had
done with them, including after a move to the bulk folder on a NAS. The
page now says what the Storage page says about the same folder."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from fastapi.testclient import TestClient

from framefound.api.v1 import basemaps
from framefound.config import Settings, get_settings
from framefound.storage import stores

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    stores._CACHE.clear()
    yield
    stores._CACHE.clear()


def test_each_place_storage_can_put_them_is_named_as_storage_names_it(tmp_path: Path) -> None:
    data, nas = tmp_path / "data", tmp_path / "nas"
    assert "on this machine" in basemaps.kept_where(Settings(data_dir=data))
    assert basemaps.kept_where(Settings(data_dir=data, bulk_dir=nas)) == "kept in the bulk folder"

    stores.set_override(data, "basemaps", tmp_path / "maps")
    stores._CACHE.clear()
    own = basemaps.kept_where(Settings(data_dir=data, bulk_dir=nas))
    assert own == "kept in a folder of their own"


def test_the_page_stops_saying_data_directory_once_they_are_in_the_bulk_folder(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup = client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
    assert setup.status_code == 201

    here = client.get("/api/v1/basemaps").json()["note"]
    assert "kept on this machine" in here and "leaves your network" in here

    nas = tmp_path / "nas"
    stores.mark_bulk_root(nas)
    monkeypatch.setenv("FRAMEFOUND_BULK_DIR", str(nas))
    get_settings.cache_clear()
    stores._CACHE.clear()
    moved = client.get("/api/v1/basemaps").json()["note"]
    assert "kept in the bulk folder" in moved
    assert "data" not in moved
