"""The seed's colour part (`infrastructure/scripts/seed_dev.py` `seed_colour`, L4 design 8.6).

A reviewer opening the seeded catalogue sees each colour state without
pressing anything first: three of the local model's proposals on the Davis Rd
listing (one mild, one large, one with a sky worth replacing), a suggested
look on one GELCO clip and a look chosen by hand on another. These tests run
the script as a reviewer does and read the result through the real API, then
seed again as a review catalogue is seeded, and check that nothing a reviewer
did is undone and nothing is added twice: proposals dismissed stay dismissed,
a choice cleared stays cleared (clearing deletes its row, so the seed has to
remember that it added one), and a clip a reviewer gave a look of their own is
not given the seed's as well.

Starting a program or reaching a real address fails the test.
"""

import runpy
import subprocess
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.api.v1 import clip_looks
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    AppSetting,
    Asset,
    ClipLook,
    Library,
    Listing,
    ListingEditSuggestion,
)
from framefound.local_models import ask, demo
from framefound.media import clip_look, photo_judge
from framefound.storage import stores

SEED = Path(__file__).resolve().parents[3] / "infrastructure" / "scripts" / "seed_dev.py"
ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
    "FRAMEFOUND_MIN_FREE_GB",
)


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may start a program or reach a real address; trying to is a failure."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a test tried to start a process or reach a real address")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    for name in _OURS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ask, "_FAKE", None)
    monkeypatch.setattr(demo, "SECONDS_PER_CHUNK", 0.0)
    monkeypatch.setattr(demo, "ANSWER_SECONDS", 0.0)

    def clear() -> None:
        get_settings.cache_clear()
        stores._CACHE.clear()
        clip_looks.clear_learned()

    clear()
    assert demo.stop(), "a demo lane left by an earlier test did not stop"
    yield
    # Reading a clip in demo mode starts the demo's lane; it must not outlive the test.
    assert demo.stop(), "the demo's lane did not stop"
    clear()


def _run_seed(root: Path, monkeypatch: pytest.MonkeyPatch, *more: str) -> None:
    """`python infrastructure/scripts/seed_dev.py <root> …`, in this process."""
    for name, value in {
        "FRAMEFOUND_DATABASE_URL": f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}",
        "FRAMEFOUND_DATA_DIR": str(root / "data"),
        "FRAMEFOUND_MEDIA_ROOT": str(root / "media"),
        "FRAMEFOUND_MODELS_DIR": str(root / "models"),
        "FRAMEFOUND_LOCAL_AI_DEMO": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", [str(SEED), str(root), *more])
    failed: list[BaseException] = []

    def run() -> None:
        try:
            runpy.run_path(str(SEED), run_name="__main__")
        except BaseException as err:  # noqa: BLE001 - handed back to the test to raise
            failed.append(err)

    # The script runs its own event loop; here it must have a thread of its own.
    worker = threading.Thread(target=run)
    worker.start()
    worker.join(300)
    assert not worker.is_alive(), "the seed did not finish"
    if failed:
        raise failed[0]
    get_settings.cache_clear()


@asynccontextmanager
async def _review(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    """The real app on the seeded catalogue, signed in as its first administrator."""
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "seed-colour-secret")
    get_settings.cache_clear()
    engine = create_async_engine(get_settings().db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            setup = await client.post(
                "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
            )
            if setup.status_code != 201:  # already set up: sign in instead
                signed = await client.post("/api/v1/auth/login", json=ADMIN)
                assert signed.status_code == 200, signed.text
            yield client, factory
    finally:
        await engine.dispose()


async def _get(client: AsyncClient, url: str) -> dict[str, Any]:
    answer = await client.get(url)
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


async def _seeded(factory: async_sessionmaker[AsyncSession]) -> tuple[str, list[str]]:
    """The Davis Rd listing's id and the first two GELCO clips' ids, by path."""
    async with factory() as db:
        listing = await db.scalar(select(Listing.id).where(Listing.name == "130 Davis Rd Auction"))
        clips = (
            await db.scalars(
                select(Asset.id)
                .join(Library, Library.id == Asset.library_id)
                .where(Library.name == "GELCO", Asset.media_type == "video")
                .order_by(Asset.relative_path)
                .limit(2)
            )
        ).all()
    assert listing is not None and len(clips) == 2
    return str(listing), [str(clip) for clip in clips]


async def _rows(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with factory() as db:
        proposals = await db.scalar(select(func.count()).select_from(ListingEditSuggestion))
        looks = await db.scalar(select(func.count()).select_from(ClipLook))
    return int(proposals or 0), int(looks or 0)


async def test_a_seeded_catalogue_shows_every_colour_state_and_seeding_again_undoes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "ff-ui"
    root.mkdir()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    # The plain seed, with no flag: the colour part is not asked for separately.
    _run_seed(root, monkeypatch)
    said = capsys.readouterr().out
    [colour] = [line for line in said.splitlines() if line.startswith("Colour:")]
    assert colour.startswith("Colour: added three of the local model's proposals on 130 Davis Rd")
    assert "a suggested look on " in colour and "a look set by hand on " in colour

    async with _review(monkeypatch) as (client, factory):
        listing_id, (suggested_clip, chosen_clip) = await _seeded(factory)

        # The listing: three proposals waiting, in gallery order, made up by the
        # demo's Looker — one mild, one large, one where the sky is worth replacing.
        edit = await _get(client, f"/api/v1/listings/{listing_id}/local-edit")
        assert edit["mode"] == "demo" and edit["ok"] is True
        assert (edit["proposed"], edit["applied"], edit["dismissed"]) == (3, 0, 0)
        rows = edit["suggestions"]
        assert [row["position"] for row in rows] == [0, 1, 2]
        assert [row["large"] for row in rows] == [False, True, False]
        assert [row["sky_worth_replacing"] for row in rows] == [False, False, True]
        assert [row["slug"] for row in rows] == [
            "front-exterior-lawn",
            "kitchen-island",
            "rear-yard-deck",
        ]
        assert all(row["demo"] and row["state"] == "proposed" for row in rows)
        assert {row["provenance"]["model"] for row in rows} == {demo.LOOKER_MODEL}
        assert {row["provenance"]["prompt_version"] for row in rows} == {photo_judge.PROMPT_VERSION}
        # Each can be looked at as the edit would land.
        preview = await client.get(
            f"/api/v1/listings/{listing_id}/local-edit/{rows[1]['item_id']}/preview.jpg"
        )
        assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"

        # One clip opens on three tiles, the model's filled in and marked demo,
        # with nothing chosen yet; its preview is a picture.
        looks = await _get(client, f"/api/v1/clip-looks/{suggested_clip}")
        model, learned, none = looks["candidates"]
        assert (model["source"], learned["source"], none["source"]) == ("model", "learned", "none")
        assert model["available"] and model["demo"] and model["reason"]
        assert model["provenance"]["model"] == demo.LOOKER_MODEL
        assert model["provenance"]["prompt_version"] == clip_look.PROMPT_VERSION
        assert set(model["recipe"]) == {"exposure", "contrast", "vibrance", "temperature"}
        assert looks["chosen"] is None
        picture = await client.get(
            f"/api/v1/clip-looks/{suggested_clip}/preview.jpg", params={"source": "model"}
        )
        assert picture.status_code == 200 and picture.headers["content-type"] == "image/jpeg"

        # The other has a look set by hand, and its LUT downloads.
        other = await _get(client, f"/api/v1/clip-looks/{chosen_clip}")
        assert other["chosen"]["source"] == "custom" and other["chosen"]["demo"] is False
        assert other["candidates"][0]["available"] is False  # no suggestion there
        cube = await client.get(f"/api/v1/clip-looks/{chosen_clip}/look.cube")
        assert cube.status_code == 200 and "LUT_3D_SIZE 33" in cube.text

        # What a reviewer does: dismiss the proposals, and press Clear the
        # choice on the one clip that has a choice (which deletes its row).
        dismissed = await client.post(f"/api/v1/listings/{listing_id}/local-edit/dismiss")
        assert dismissed.status_code == 200, dismissed.text
        assert dismissed.json()["dismissed"] == 3
        cleared = await client.delete(f"/api/v1/clip-looks/{chosen_clip}/choice")
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["chosen"] is None
        before = await _rows(factory)
        assert before == (3, 1)

    # Seeded again, as the review catalogue is: nothing added, nothing put back.
    _run_seed(root, monkeypatch, "--local-ai-only")
    assert (
        "Colour: everything it needs is already in this catalogue; nothing was added."
        in capsys.readouterr().out.splitlines()
    )
    async with _review(monkeypatch) as (client, factory):
        assert await _rows(factory) == before
        edit = await _get(client, f"/api/v1/listings/{listing_id}/local-edit")
        assert (edit["proposed"], edit["dismissed"]) == (0, 3)
        # The cleared choice is still cleared: the card shows nothing chosen.
        assert (await _get(client, f"/api/v1/clip-looks/{chosen_clip}"))["chosen"] is None

        # A catalogue no seed has kept a note for (seeded before the note was
        # kept, or never seeded with these), where a reviewer pressed Suggest
        # on that clip and chose nothing: the clip is theirs. No choice is
        # made for them, and the dismissed proposals are not put back.
        async with factory() as db:
            await db.execute(delete(AppSetting).where(AppSetting.key == demo.SEEDED_KEY))
            theirs = await db.scalar(select(ClipLook).where(ClipLook.kind == "suggested"))
            assert theirs is not None and str(theirs.asset_id) == suggested_clip
            db.add(
                ClipLook(
                    asset_id=uuid.UUID(chosen_clip),
                    kind="suggested",
                    source="model",
                    recipe=dict(theirs.recipe),
                    measured=dict(theirs.measured),
                    left_out=[],
                    still_ts_ms=theirs.still_ts_ms,
                    features=list(theirs.features),
                    reason="Asked for by a reviewer.",
                    demo=True,
                )
            )
            await db.commit()
        before = await _rows(factory)
        assert before == (3, 2)

    _run_seed(root, monkeypatch, "--local-ai-only")
    assert (
        "Colour: everything it needs is already in this catalogue; nothing was added."
        in capsys.readouterr().out.splitlines()
    )
    async with _review(monkeypatch) as (client, factory):
        assert await _rows(factory) == before
        theirs_now = await _get(client, f"/api/v1/clip-looks/{chosen_clip}")
        assert theirs_now["chosen"] is None and theirs_now["candidates"][0]["available"] is True
        edit = await _get(client, f"/api/v1/listings/{listing_id}/local-edit")
        assert (edit["proposed"], edit["dismissed"]) == (0, 3)
        # What it found is noted, so even an emptied clip is not filled again.
        async with factory() as db:
            await db.execute(delete(ClipLook))
            await db.commit()

    _run_seed(root, monkeypatch, "--local-ai-only")
    assert (
        "Colour: everything it needs is already in this catalogue; nothing was added."
        in capsys.readouterr().out.splitlines()
    )
    async with _review(monkeypatch) as (_client, factory):
        assert await _rows(factory) == (3, 0)


async def test_a_catalogue_without_the_seeded_media_is_told_what_was_not_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "ff-ui"
    root.mkdir()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    # `--local-ai-only` on an empty catalogue: no listing, no clips.
    _run_seed(root, monkeypatch, "--local-ai-only")
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Colour:")]
    assert lines == [
        "Colour: not added: the listing's proposals (no listing holds the Davis Rd photographs); "
        "the clip looks (this catalogue has fewer than two GELCO clips)."
    ]
    async with _review(monkeypatch) as (_client, factory):
        assert await _rows(factory) == (0, 0)
