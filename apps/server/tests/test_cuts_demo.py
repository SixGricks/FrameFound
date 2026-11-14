"""A rough cut drafted by the local Editor, end to end, in demo mode (design L5 S9).

The other tests of drafting hand the lane a model server of their own
(`test_cut_drafts.py`, `test_cuts_api.py`). This one hands in nothing: it is
the review copy as a reviewer drives it. Demo mode builds its own stand-ins
for Hugging Face, the software that runs models and the model server, the
Editor is given its role through the AI models page's own routes, and the
draft runs on the demo's lane inside the process. So it is the one test that
fails when a draft works everywhere except where the owner first sees one.

The only things the test changes are the demo's pauses. The footage is rows:
clips and their shot windows. Starting a program or reaching a real address
fails the test.
"""

import asyncio
import json
import subprocess
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import cut_editor
from framefound.api.v1 import local_models as models_api
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Library, ShotWindow
from framefound.local_models import ask, demo, jobs
from framefound.storage import stores

MODELS = "/api/v1/local-models"
CUTS = "/api/v1/cuts"
ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
# The demo model that fits the daytime budget, so the draft runs whatever
# the hour the test is run at. (The plan's Editor fits only overnight: a
# draft of it asked for by day waits for 21:30, which `test_cut_drafts.py`
# checks with a clock of its own.)
EDITOR = "mlx-community/gemma-4-e4b-it-4bit"
FOLDER = "2026/Pete Dye"
BRIEF = "Thirty seconds of smooth aerials at golden hour, opening wide."
SHOT = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
# Three shots a clip: 7 s, 8 s and 7 s.
WINDOWS = ((0, 7000), (7000, 15000), (15000, 22000))

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
    # The demo's one model server is made afresh for each test.
    monkeypatch.setattr(ask, "_FAKE", None)
    # A demo download and a demo answer as fast as the machine allows.
    monkeypatch.setattr(demo, "SECONDS_PER_CHUNK", 0.0)
    monkeypatch.setattr(demo, "ANSWER_SECONDS", 0.0)

    def clear() -> None:
        get_settings.cache_clear()
        stores._CACHE.clear()
        models_api._STATUS.clear()
        models_api._LOOKING.clear()

    clear()
    assert demo.stop(), "a demo lane left by an earlier test did not stop"
    yield
    # The lane must not outlive the test that started it.
    assert demo.stop(), "the demo's lane did not stop"
    clear()


class Review:
    """The review copy: the real app in demo mode, signed in as its administrator."""

    def __init__(self, client: AsyncClient, factory: async_sessionmaker[AsyncSession]) -> None:
        self.client = client
        self.factory = factory

    async def get(self, url: str) -> Any:
        answer = await self.client.get(url)
        assert answer.status_code == 200, answer.text
        return answer.json()

    async def post(self, url: str, body: Any = None, status: int = 200) -> Any:
        answer = await self.client.post(url, json=body)
        assert answer.status_code == status, answer.text
        return answer.json()

    async def until(
        self, url: str, done: Callable[[Any], bool], what: str, seconds: float = 30.0
    ) -> Any:
        """Read `url` until `done` says yes, as the page's own polling does."""
        give_up = asyncio.get_running_loop().time() + seconds
        while True:
            models_api._STATUS.clear()
            found = await self.get(url)
            if done(found):
                return found
            if asyncio.get_running_loop().time() > give_up:
                raise AssertionError(f"{what} never happened: {json.dumps(found)[:4000]}")
            await asyncio.sleep(0.05)

    async def an_editor(self) -> str:
        """What a reviewer does on the AI models page before the first draft:
        install the software, check and download a model, give it the Editor
        role. The model's id."""
        await self.post(f"{MODELS}/runtimes/demo/install", {"version": "1.8"}, status=202)
        await self.until(
            MODELS,
            lambda page: (
                any(r["name"] == "demo" and r["installed"] == "1.8" for r in page["runtimes"])
                and not any(
                    job["kind"] == "runtime_install" and job["state"] in jobs.ACTIVE
                    for job in page["jobs"]
                )
            ),
            "the install",
        )
        card = await self.post(f"{MODELS}/preflight", {"name": EDITOR})
        added = await self.post(
            f"{MODELS}/models",
            {"name": EDITOR, "revision": card["revision"], "accept_warnings": True},
            status=202,
        )
        await self.until(
            MODELS,
            lambda page: any(
                m["id"] == added["id"] and m["state"] == "installed" for m in page["models"]
            ),
            "the download",
        )
        role = await self.client.put(f"{MODELS}/roles/editor", json={"model_id": added["id"]})
        assert role.status_code == 200, role.text
        return str(added["id"])

    async def footage(self) -> tuple[str, dict[str, list[tuple[float, float]]]]:
        """A library with a folder of three analysed drone clips. The library's
        id, and each clip's shots in seconds, by clip id."""
        shots: dict[str, list[tuple[float, float]]] = {}
        async with self.factory() as db:
            lib = Library(name="GELCO", root_path="/srv/media/GELCO")
            db.add(lib)
            await db.flush()
            for n in range(1, 4):
                name = f"DJI_20261103051509_063{n}_D.MP4"
                clip = Asset(
                    library_id=lib.id,
                    relative_path=f"{FOLDER}/{name}",
                    filename=name,
                    extension="mp4",
                    media_type="video",
                    size_bytes=1,
                    mtime=SHOT,
                    captured_at=SHOT + timedelta(hours=n),
                    duration_s=22.0,
                    fps=29.97,
                    width=3840,
                    height=2160,
                    availability="online",
                )
                db.add(clip)
                await db.flush()
                for idx, (start, end) in enumerate(WINDOWS):
                    db.add(
                        ShotWindow(
                            asset_id=clip.id,
                            idx=idx,
                            start_ms=start,
                            end_ms=end,
                            move=("rise", "orbit", "push-in")[idx],
                            smooth=0.9,
                            energy="low",
                            light="golden hour",
                            framing={"view": "aerial"},
                            subjects={"golf course": 0.8},
                        )
                    )
                shots[str(clip.id)] = [(start / 1000, end / 1000) for start, end in WINDOWS]
            await db.commit()
            return str(lib.id), shots


@pytest.fixture()
async def review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Review]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'ui.db').as_posix()}"
    for name, value in {
        "FRAMEFOUND_SETUP_TOKEN": TEST_SETUP_TOKEN,
        "FRAMEFOUND_SECRET_KEY": "cuts-demo-secret",
        "FRAMEFOUND_DATABASE_URL": url,
        "FRAMEFOUND_MEDIA_ROOT": str(tmp_path / "media"),
        "FRAMEFOUND_DATA_DIR": str(tmp_path / "data"),
        "FRAMEFOUND_MODELS_DIR": str(tmp_path / "models"),
        "FRAMEFOUND_LOCAL_AI_DEMO": "1",
        "FRAMEFOUND_MIN_FREE_GB": "0",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    from framefound.main import create_app

    app = create_app()
    # The database is the test's; nothing that stands in for the outside world is.
    app.dependency_overrides[get_session] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        setup = await client.post(
            "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
        )
        assert setup.status_code == 201, setup.text
        yield Review(client, factory)
        assert demo.stop()
    await engine.dispose()


def _inside(piece: dict[str, Any], shots: dict[str, list[tuple[float, float]]]) -> bool:
    """Whether a piece of a cut lies wholly inside one analysed shot of its clip."""
    return any(
        start <= piece["in_s"] < piece["out_s"] <= end for start, end in shots[piece["asset_id"]]
    )


async def test_a_draft_runs_from_the_form_to_a_cut_in_demo_mode_with_nothing_handed_in(
    review: Review,
) -> None:
    library_id, shots = await review.footage()

    # Before any model has the role, the form says where to go and nothing can be asked.
    page = await review.get(CUTS)
    assert (page["editor"]["mode"], page["editor"]["note"]) == ("demo", demo.DEMO_NOTE)
    assert page["can_draft"] is False and "AI models page" in page["editor"]["why_not"]
    refused = await review.client.post(
        f"{CUTS}/drafts",
        json={
            "brief": BRIEF,
            "scope": {"kind": "folder", "library_id": library_id, "folder": FOLDER},
        },
    )
    assert refused.status_code == 409, refused.text
    assert (await review.get(CUTS))["drafts"] == []

    editor_id = await review.an_editor()
    page = await review.get(CUTS)
    editor = page["editor"]
    assert (editor["model_id"], editor["runs"], editor["why_not"]) == (editor_id, "now", None)
    assert page["can_draft"] is True and page["cuts"] == []
    # The form's picker offers the folder, with every clip analysed.
    # The year folder it sits in is offered after it, holding the same clips.
    offered = await review.get(f"{CUTS}/folders")
    assert [(f["path"], f["clips"], f["analysed"]) for f in offered] == [
        (FOLDER, 3, 3),
        ("2026", 3, 3),
    ]

    # Draft: accepted at once, done by the demo's own lane, read as the page reads it.
    asked = await review.post(
        f"{CUTS}/drafts",
        {
            "brief": BRIEF,
            "scope": {"kind": "folder", "library_id": library_id, "folder": FOLDER},
        },
        status=202,
    )
    assert asked["state"] in ("queued", "running") and asked["shots_offered"] == 9
    draft = await review.until(
        f"{CUTS}/drafts/{asked['id']}", lambda d: d["state"] not in ("queued", "running"), "a cut"
    )
    assert (draft["state"], draft["message"], draft["problems"]) == ("done", "", [])
    # Made up, and said to be: no model judged anything.
    assert draft["demo"] is True and draft["model_name"] == EDITOR.rsplit("/", 1)[-1]
    assert draft["cut_id"] is not None

    # The cut: version 1, a local draft by the demo's runtime, every piece
    # inside a shot it was offered, about the length the brief named.
    cut = await review.get(f"{CUTS}/{draft['cut_id']}")
    [version] = cut["versions"]
    assert (version["version"], version["source"], version["label"]) == (1, "local", "Local draft")
    assert version["demo"] is True and version["sent_at"] is None
    made_by = version["provenance"]
    assert (made_by["model"], made_by["runtime"]) == (EDITOR, "demo")
    assert made_by["prompt_version"] == cut_editor.PROMPT_VERSION
    pieces = version["pieces"]
    assert len(pieces) == version["piece_count"] == cut_editor.Constraints(30.0).about
    assert all(_inside(piece, shots) for piece in pieces)
    assert {piece["note"] for piece in pieces} == {cut_editor.DEMO_REASON}
    assert 27.0 <= version["duration_s"] <= 33.0
    # What it was chosen from is kept, with the chosen ones marked.
    offered = version["draft"]["candidates"]
    assert len(offered) == 9 and sum(1 for shot in offered if shot["chosen"]) == len(pieces)

    # The list shows it as one family; it is not yet footage that was used.
    [family] = (await review.get(CUTS))["cuts"]
    assert (family["id"], family["versions"], family["has_local_draft"]) == (
        draft["cut_id"],
        1,
        True,
    )
    assert family["newest"]["demo"] is True and family["sent"] is False

    # The Premiere file is built from it, and the request to Claude says what it is.
    sent = await review.post(f"{CUTS}/{draft['cut_id']}/send", {"profile": ""})
    assert sent["cuts"] == len(pieces) and sent["xml"].lstrip().startswith("<?xml")
    assert sent["sent_at"] is not None
    request = await review.get(f"{CUTS}/{draft['cut_id']}/review-request")
    assert "a demonstration that judged nothing" in request["text"]
    assert f'parent="{draft["cut_id"]}"' in request["text"]

    # Draft again: the next version of the same cut, the first one kept.
    again = await review.post(
        f"{CUTS}/drafts",
        {
            "brief": "The same, but only twenty seconds.",
            "scope": {"kind": "folder", "library_id": library_id, "folder": FOLDER},
            "revises": draft["cut_id"],
        },
        status=202,
    )
    second = await review.until(
        f"{CUTS}/drafts/{again['id']}", lambda d: d["state"] not in ("queued", "running"), "v2"
    )
    assert second["state"] == "done", second
    cut = await review.get(f"{CUTS}/{second['cut_id']}")
    assert [v["version"] for v in cut["versions"]] == [1, 2]
    assert cut["versions"][1]["parent_id"] == draft["cut_id"]
    assert uuid.UUID(cut["root_id"]) == uuid.UUID(draft["cut_id"])
    assert 18.0 <= cut["versions"][1]["duration_s"] <= 22.0
    [family] = (await review.get(CUTS))["cuts"]
    assert (family["versions"], family["sent"]) == (2, True)
