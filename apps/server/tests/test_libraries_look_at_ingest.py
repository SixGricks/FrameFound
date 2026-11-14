"""The per-library switch for looking at what arrives (L3, `look_at_ingest`).

Off for every library until an administrator turns it on, refused where
local models cannot run (the containers), and the moment it goes on is the
line older material stays behind.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.auth import service as auth_service
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import AuditLog, Library
from framefound.local_models import UNAVAILABLE

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
EDITOR = {"email": "editor@example.com", "password": "another-strong-password"}


@pytest.fixture()
async def site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """A server with one library, an administrator signed in, and the
    database at hand to read what the requests wrote. Models cannot run
    here until a test says they can."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'libraries.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "look-switch-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR", raising=False)
    monkeypatch.delenv("FRAMEFOUND_LOCAL_AI_DEMO", raising=False)
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
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path / "GELCO"))
        db.add(library)
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        assert (
            await http.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"http": http, "app": app, "factory": factory, "library_id": library.id}
    await engine.dispose()
    get_settings.cache_clear()


def models_can_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRAMEFOUND_RUNTIMES_DIR", str(tmp_path / "runtimes"))
    get_settings.cache_clear()


async def switch(site: dict[str, Any], on: bool, **also: Any) -> Any:
    return await site["http"].patch(
        f"/api/v1/libraries/{site['library_id']}", json={"look_at_ingest": on, **also}
    )


def moment(text: str) -> datetime:
    """A time from the API. SQLite hands a stored time back without its
    zone, which is UTC, so a row read back reads the same as one just set."""
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


async def stored(site: dict[str, Any]) -> Library:
    async with site["factory"]() as db:
        library = await db.get(Library, site["library_id"])
        assert library is not None
        return library


async def audited(site: dict[str, Any]) -> list[AuditLog]:
    async with site["factory"]() as db:
        rows = await db.execute(
            select(AuditLog).where(AuditLog.event == "library.look_at_ingest").order_by(AuditLog.id)
        )
        return list(rows.scalars())


async def test_a_library_starts_with_looking_switched_off(site: dict[str, Any]) -> None:
    listed = (await site["http"].get("/api/v1/libraries")).json()
    one = (await site["http"].get(f"/api/v1/libraries/{site['library_id']}")).json()
    for body in (listed[0], one):
        assert (body["look_at_ingest"], body["look_since"]) == (False, None)


async def test_switching_it_on_where_models_cannot_run_is_refused_with_the_reason(
    site: dict[str, Any],
) -> None:
    response = await switch(site, True, name="Renamed")
    assert response.status_code == 409
    assert response.json()["error"]["message"] == UNAVAILABLE
    # Nothing in the refused request was applied, the rename included.
    library = await stored(site)
    assert (library.look_at_ingest, library.look_since, library.name) == (False, None, "GELCO")
    assert await audited(site) == []


async def test_switching_it_on_marks_the_moment_and_says_who(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models_can_run(monkeypatch, tmp_path)
    before = datetime.now(UTC)
    response = await switch(site, True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["look_at_ingest"] is True
    since = moment(body["look_since"])
    assert before <= since <= datetime.now(UTC)

    (row,) = await audited(site)
    assert row.detail == {"name": "GELCO", "on": True}
    assert row.actor_user_id is not None


async def test_asking_for_the_state_it_is_in_keeps_the_line_where_it_was(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Moving `look_since` forward would leave what arrived in between
    behind for good, so saying "on" again (a form that sends every field)
    changes nothing and records nothing."""
    models_can_run(monkeypatch, tmp_path)
    first = moment((await switch(site, True)).json()["look_since"])
    again = await switch(site, True, transcribe_enabled=False)
    assert again.status_code == 200
    assert moment(again.json()["look_since"]) == first
    assert again.json()["transcribe_enabled"] is False
    assert len(await audited(site)) == 1


async def test_switching_it_off_forgets_the_moment_and_works_where_models_cannot_run(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A library switched on before the install moved to the containers
    must still be switchable off there: refusing a way out is a trap."""
    models_can_run(monkeypatch, tmp_path)
    assert (await switch(site, True)).status_code == 200
    monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR")
    get_settings.cache_clear()

    response = await switch(site, False)
    assert response.status_code == 200, response.text
    assert (response.json()["look_at_ingest"], response.json()["look_since"]) == (False, None)
    library = await stored(site)
    assert (library.look_at_ingest, library.look_since) == (False, None)
    assert [row.detail["on"] for row in await audited(site)] == [True, False]


async def test_turning_it_off_and_on_again_does_not_reach_back(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models_can_run(monkeypatch, tmp_path)
    first = moment((await switch(site, True)).json()["look_since"])
    await switch(site, False)
    second = moment((await switch(site, True)).json()["look_since"])
    assert second >= first


async def test_the_demo_may_switch_it_on(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRAMEFOUND_LOCAL_AI_DEMO", "true")
    get_settings.cache_clear()
    assert (await switch(site, True)).status_code == 200


async def test_only_an_administrator_can_switch_it(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models_can_run(monkeypatch, tmp_path)
    async with site["factory"]() as db:
        await auth_service.create_user(db, EDITOR["email"], EDITOR["password"], "user")
        await db.commit()
    http = site["http"]
    http.cookies.clear()
    assert (await http.post("/api/v1/auth/login", json=EDITOR)).status_code == 200
    response = await switch(site, True)
    assert response.status_code == 403
    # Reading it is for everybody signed in.
    one = await http.get(f"/api/v1/libraries/{site['library_id']}")
    assert one.status_code == 200 and one.json()["look_at_ingest"] is False


def test_the_switch_is_not_part_of_creating_a_library() -> None:
    """A library is made with looking off; turning it on is its own
    decision, which the PATCH checks and records."""
    from framefound.api.v1.libraries import LibraryCreate

    assert "look_at_ingest" not in LibraryCreate.model_fields
