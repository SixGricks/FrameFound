"""Compare on your footage, through the API (api/v1/local_models_compare.py; design L2 6).

Through the real app and its routes, on file-backed SQLite. The catalogue is
built by the test: a library, two clips with shot windows and their stills
on disk, photographs with previews, decided tags and a finished cut. The
models are L1's fixtures shrunk to a few kilobytes and placed in a library
under `tmp_path`; the model server is `fake.FakeModelServer` behind the real
`ask()`; the clock is a `Clock` the test moves — all handed in through the
one `get_deps` override. The queue is a list: `wake` records that it was
called, and a test runs the lane itself (`jobs.run_next`) when it wants the
work done. Starting a process or reaching a real address fails the test.

The last section is demo mode (design L2 S7): there nothing is handed in at
all — the demo builds its own stand-ins and runs its lane inside the app —
and the dev seed is run as a developer runs it.
"""

import asyncio
import contextlib
import io
import json
import runpy
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.api.v1 import local_models as l1_api
from framefound.api.v1 import local_models_compare as api
from framefound.auth import service
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    Asset,
    AssetTag,
    AuditLog,
    Comparison,
    ComparisonAnswer,
    ComparisonItem,
    ComparisonModel,
    Cut,
    CutPiece,
    Derivative,
    Library,
    LocalAiJob,
    LocalModel,
    ShotWindow,
    Tag,
)
from framefound.local_models import (
    UNAVAILABLE,
    ask,
    compare_job,
    comparisons,
    fake,
    jobs,
    library,
    preflight,
    registry,
    runtimes,
    sampling,
)
from framefound.local_models import demo as l1_demo
from framefound.local_models.ask import Answer
from framefound.local_models.deps import Deps
from framefound.local_models.kinds import MlxVlm, RuntimeKind
from framefound.local_models.settings_store import LocalModelsConfig, load_config, save_config
from framefound.looking import demo as looks_demo
from framefound.looking import prompt
from framefound.processing import local_ai_tasks
from framefound.processing import shots as shot_files
from framefound.storage import stores

Factory = async_sessionmaker[AsyncSession]

URL = "/api/v1/local-models/compare"
ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
READER = {"email": "reader@example.com", "password": "another-strong-password"}
FIRST = "mlx-community/gemma-4-e4b-it-4bit"
SECOND = "mlx-community/demo-large-4bit"
TEXT_ONLY = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
VERSION = "0.7.12"
NOON = datetime(2026, 11, 12, 12, 0)
BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)

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
    monkeypatch.setattr(jobs, "PROGRESS_EVERY_S", 0.0)

    def clear() -> None:
        get_settings.cache_clear()
        stores._CACHE.clear()
        l1_api._STATUS.clear()
        l1_api._LOOKING.clear()

    clear()
    yield
    clear()
    fake.FakeModelServer.answer_for("LookAnswer", None)


class Clock:
    """The Mac's wall clock, naive, moved by the test."""

    def __init__(self, at: datetime = NOON) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _jpeg(path: Path, shade: tuple[int, int, int] = (90, 120, 150), size=(64, 36)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = io.BytesIO()
    Image.new("RGB", size, shade).save(out, "JPEG")
    path.write_bytes(out.getvalue())
    return path


class Site:
    """A Mac running FrameFound with a small catalogue, as the API sees it."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: AsyncClient, app: Any
    ) -> None:
        self.tmp = tmp_path
        self.data = tmp_path / "data"
        self.monkeypatch = monkeypatch
        self.client = client
        self.app = app
        self.factory: Factory
        self.hub = fake.FakeHub.from_fixtures(sleep=lambda _seconds: None)
        for repo in (FIRST, SECOND, TEXT_ONLY):
            self.hub.shrink(repo)
        self.server = fake.FakeModelServer(sleep=lambda _seconds: None)
        self.clock = Clock()
        self.kinds: dict[str, RuntimeKind] = {"mlx-vlm": MlxVlm()}
        self.woken: list[bool] = []
        self.queue_up = True
        self.library = uuid.uuid4()
        self.tags: dict[str, uuid.UUID] = {}
        self.made = 0

    def deps(self) -> Deps:
        return Deps(
            settings=get_settings(),
            hub_for=self.hub.with_token,
            fetch_json=lambda _url, **_more: {},
            server=self.server,
            kinds=self.kinds,
            sleep=lambda _seconds: None,
            now=self.clock,
            system="Darwin",
            machine="arm64",
        )

    def wake(self, _settings: Any = None) -> bool:
        self.woken.append(self.queue_up)
        return self.queue_up

    def unconfigured(self) -> None:
        """The containers: no runtimes folder."""
        self.monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR")
        self.kinds = {}
        get_settings.cache_clear()

    async def drain(self) -> None:
        """What the lane does when woken: run jobs until none is queued."""
        for _ in range(30):
            if not await jobs.run_next(self.factory, self.deps()):
                return
        raise AssertionError("the lane never ran out of work")

    # --- the models -----------------------------------------------------------------

    async def install(self, repo: str = FIRST) -> str:
        """A model checked and added as the AI models page would; its files in the library."""
        found = preflight.preflight(
            self.hub,
            repo,
            config=LocalModelsConfig(),
            kinds={"mlx-vlm": MlxVlm()},
            installed={"mlx-vlm": VERSION},
            free_bytes=None,
            min_free_bytes=0,
        )
        async with self.factory() as db:
            model = await registry.add_model(db, found, None)
            model.state = registry.INSTALLED
            await db.commit()
            files = registry.snapshot(model)
        library.download(self.hub, files, settings=get_settings())
        return str(model.id)

    async def needs(self, model_id: str, memory_gb: float) -> None:
        async with self.factory() as db:
            await db.execute(
                update(LocalModel)
                .where(LocalModel.id == uuid.UUID(model_id))
                .values(memory_gb=memory_gb)
            )
            await db.commit()

    # --- the catalogue ---------------------------------------------------------------

    def _asset(self, relative_path: str, media_type: str) -> Asset:
        self.made += 1
        return Asset(
            library_id=self.library,
            relative_path=relative_path,
            filename=relative_path.rsplit("/", 1)[-1],
            extension=relative_path.rsplit(".", 1)[-1].lower(),
            media_type=media_type,
            size_bytes=1000,
            mtime=BASE,
            availability="online",
            captured_at=BASE + timedelta(minutes=self.made),
        )

    async def video(self, relative_path: str, shots: int) -> uuid.UUID:
        async with self.factory() as db:
            asset = self._asset(relative_path, "video")
            db.add(asset)
            await db.flush()
            for idx in range(shots):
                start, end = idx * 4000, (idx + 1) * 4000
                db.add(
                    ShotWindow(
                        asset_id=asset.id,
                        idx=idx,
                        start_ms=start,
                        end_ms=end,
                        move="orbit",
                        direction="right",
                        speed=0.1,
                        smooth=0.93,
                        energy="low",
                        light="golden",
                        look={"sky": "overcast"},
                        subjects={"barn": 0.9},
                    )
                )
                for moment in set(shot_files.window_moments(start, end)):
                    _jpeg(self.data / shot_files.still_relpath(asset.id, moment))
            await db.commit()
            return asset.id

    async def photo(self, relative_path: str) -> uuid.UUID:
        async with self.factory() as db:
            asset = self._asset(relative_path, "image")
            db.add(asset)
            await db.flush()
            relative = f"derivatives/{str(asset.id)[:2]}/{asset.id}/preview.jpeg"
            db.add(
                Derivative(
                    asset_id=asset.id,
                    kind="preview",
                    relative_path=relative,
                    media_format="jpeg",
                    status="ready",
                )
            )
            _jpeg(self.data / relative, (200, 180, 160), (800, 600))
            await db.commit()
            return asset.id

    async def rule(self, asset_id: uuid.UUID, name: str, source: str) -> None:
        async with self.factory() as db:
            if name not in self.tags:
                tag = Tag(name=name, slug=name.lower().replace(" ", "-"))
                db.add(tag)
                await db.flush()
                self.tags[name] = tag.id
            db.add(AssetTag(asset_id=asset_id, tag_id=self.tags[name], source=source))
            await db.commit()

    async def cut(self, title: str, source: str, pieces: list[tuple[uuid.UUID, int, int]]) -> str:
        async with self.factory() as db:
            cut = Cut(title=title, client="GELCO", source=source, piece_count=len(pieces))
            db.add(cut)
            await db.flush()
            for position, (asset_id, in_ms, out_ms) in enumerate(pieces):
                db.add(
                    CutPiece(
                        cut_id=cut.id,
                        position=position,
                        asset_id=asset_id,
                        in_ms=in_ms,
                        out_ms=out_ms,
                    )
                )
            await db.commit()
            return str(cut.id)

    async def shoot(self) -> dict[str, Any]:
        """Brookside: two clips of eight shots in one folder, four photographs,
        tags decided on them, and the finished cut that used two shots."""
        async with self.factory() as db:
            db.add(Library(id=self.library, name="GELCO", root_path=str(self.tmp / "gelco")))
            await db.commit()
        used = await self.video("Brookside/2026/Drone/DJI_0001.MP4", 8)
        beside = await self.video("Brookside/2026/Drone/DJI_0002.MP4", 8)
        photos = [await self.photo(f"Brookside/2026/Stills/IMG_000{n}.JPG") for n in range(4)]
        await self.rule(used, "barn", "manual")
        await self.rule(used, "sunset", "rejected")
        await self.rule(photos[0], "barn", "confirmed")
        await self.rule(beside, "pond", "suggested")  # waiting for a person: not truth
        cut = await self.cut("Brookside Quick 30", "import", [(used, 0, 4000), (used, 4000, 8000)])
        draft = await self.cut("A local draft", "local", [(beside, 0, 4000)])
        return {"used": used, "beside": beside, "photos": photos, "cut": cut, "draft": draft}

    # --- reading ------------------------------------------------------------------------

    async def events(self) -> list[str]:
        async with self.factory() as db:
            rows = await db.scalars(select(AuditLog.event).order_by(AuditLog.id))
            return [event for event in rows if event.startswith("local_models.compare")]

    async def rows(self, table: Any) -> int:
        async with self.factory() as db:
            return int(await db.scalar(select(func.count()).select_from(table)) or 0)

    async def reader(self) -> AsyncClient:
        async with self.factory() as db:
            await service.create_user(db, READER["email"], READER["password"], "user")
            await db.commit()
        client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://t")
        signed_in = await client.post("/api/v1/auth/login", json=READER)
        assert signed_in.status_code == 200, signed_in.text
        return client

    async def start(self, model_ids: list[str], **body: Any) -> dict[str, Any]:
        sent = {"source": {"kind": "cuts", "cut_ids": [body.pop("cut")]}, "size": 10, **body}
        answer = await self.client.post(URL, json={**sent, "model_ids": model_ids})
        assert answer.status_code == 201, answer.text
        found: dict[str, Any] = answer.json()
        return found

    async def one(self, comparison_id: str, client: AsyncClient | None = None) -> dict[str, Any]:
        answer = await (client or self.client).get(f"{URL}/{comparison_id}")
        assert answer.status_code == 200, answer.text
        found: dict[str, Any] = answer.json()
        return found

    async def items(self, comparison_id: str) -> dict[str, Any]:
        answer = await self.client.get(f"{URL}/{comparison_id}/items")
        assert answer.status_code == 200, answer.text
        found: dict[str, Any] = answer.json()
        return found


@pytest.fixture()
async def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Site]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'catalogue.db').as_posix()}"
    for folder in ("data", "models", "runtimes"):
        (tmp_path / folder).mkdir()
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "compare-page-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("FRAMEFOUND_RUNTIMES_DIR", str(tmp_path / "runtimes"))
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "0")
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
    app.dependency_overrides[get_session] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        made = Site(tmp_path, monkeypatch, client, app)
        made.factory = factory
        app.dependency_overrides[l1_api.get_deps] = made.deps
        monkeypatch.setattr(local_ai_tasks, "wake", made.wake)
        setup = await client.post(
            "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
        )
        assert setup.status_code == 201, setup.text
        runner = fake.FakeRunner(
            {
                "uv venv": (0, ""),
                "uv pip install": (0, ""),
                "uv pip freeze": (0, ""),
                "python": lambda argv: (0, f"{Path(argv[0]).parents[2].name}\n"),
            },
            make_venvs=True,
        )
        runtimes.install(MlxVlm(), VERSION, tmp_path / "runtimes", run=runner)
        runtimes.activate(tmp_path / "runtimes", "mlx-vlm", VERSION)
        yield made
    await engine.dispose()


def message(answer: httpx.Response) -> str:
    found: str = answer.json()["error"]["message"]
    return found


def look(**values: Any) -> str:
    answer: dict[str, Any] = {
        "caption": "A slow orbit around a red barn.",
        "score": 72,
        "reason": "Steady and well framed.",
        "matches": [{"tag": "barn", "confidence": 0.9}, {"tag": "sunset", "confidence": 0.5}],
        "new_tags": [{"tag": "silo", "confidence": 0.7}],
    }
    answer.update(values)
    return json.dumps(answer)


def pictures_of(site: Site, comparison_id: str) -> list[str]:
    folder = site.data / sampling.FOLDER / comparison_id
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# --- the whole click-path ---------------------------------------------------------------


async def test_the_whole_click_path_from_choosing_a_sample_to_deleting_it(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    second = await site.install(SECOND)
    text_only = await site.install(TEXT_ONLY)
    fake.FakeModelServer.answer_for("LookAnswer", lambda _body: look())

    page = (await site.client.get(URL)).json()
    assert page["mode"] == "ready" and page["can_manage"] is True
    assert page["prompt_version"] == prompt.PROMPT_VERSION
    assert page["limits"] == {"min_size": 10, "max_size": 200, "default_size": 60, "max_models": 6}
    choices = {model["id"]: model for model in page["models"]}
    assert choices[first]["can_compare"] is True and choices[first]["reason"] is None
    assert choices[first]["fits"] == "day" and choices[first]["seconds_hint"] is None
    assert choices[text_only]["can_compare"] is False
    assert choices[text_only]["reason"] == "It does not read pictures."
    assert page["comparisons"] == []

    # Where a sample can come from: the finished cut (never the draft), and folders.
    cuts = (await site.client.get(f"{URL}/sources/cuts")).json()
    assert [cut["title"] for cut in cuts] == ["Brookside Quick 30"]
    assert cuts[0] | {"id": None, "created_at": None} == {
        "id": None,
        "title": "Brookside Quick 30",
        "client": "GELCO",
        "source": "import",
        "created_at": None,
        "pieces": 2,
        "clips": 1,
        "clips_with_shots": 1,
        "duration_s": 0.0,
    }
    folders = (await site.client.get(f"{URL}/sources/folders", params={"q": "drone"})).json()
    assert [(f["path"], f["clips"], f["clips_with_shots"], f["shots"]) for f in folders] == [
        ("Brookside/2026/Drone", 2, 2, 16)
    ]
    assert folders[0]["library_name"] == "GELCO" and folders[0]["photos"] == 0
    wider = (await site.client.get(f"{URL}/sources/folders", params={"q": "BROOKSIDE"})).json()
    assert [(f["path"], f["photos"], f["clips"]) for f in wider] == [
        ("Brookside", 4, 2),
        ("Brookside/2026", 4, 2),
        ("Brookside/2026/Drone", 0, 2),
        ("Brookside/2026/Stills", 4, 0),
    ]
    # The filename is not matched.
    assert (await site.client.get(f"{URL}/sources/folders", params={"q": "DJI"})).json() == []

    # What the sample would be: written nowhere.
    sample = {"source": {"kind": "cuts", "cut_ids": [shoot["cut"]]}, "size": 10}
    shown = (await site.client.post(f"{URL}/preview", json=sample)).json()
    assert shown["ok"] is True
    assert shown["label"] == "Shots used in Brookside Quick 30, and the clips beside them"
    assert (shown["what"], shown["size"], shown["stills"]) == ("shots", 10, 3)
    assert (shown["items"], shown["shots"], shown["photos"], shown["clips"]) == (10, 10, 0, 2)
    assert shown["pool"] == {"photos": 0, "shots": 16, "clips": 2}
    assert shown["truth"]["tags"] == {"footage": 1, "yes": 1, "no": 1}
    assert (shown["truth"]["used"]["used"], shown["truth"]["used"]["unused"]) == (2, 8)
    assert [cut["title"] for cut in shown["truth"]["used"]["cuts"]] == ["Brookside Quick 30"]
    assert shown["left_out"] == [
        {"why": "over", "count": 6, "sentence": "6 more were not needed: the sample is 10."}
    ]
    assert shown["notes"] == [
        "Only 2 of these shots were used: read the score figures as a hint.",
        api.PER_CLIP,
    ]
    assert await site.rows(Comparison) == 0 and not (site.data / sampling.FOLDER).exists()

    # Start.
    made = await site.start([first], cut=shoot["cut"], name="Brookside Rev 2 shots", seed=7)
    cid = made["id"]
    assert site.woken == [True]
    assert made["name"] == "Brookside Rev 2 shots" and made["seed"] == 7
    assert (made["size"], made["what"], made["stills"], made["current"]) == (10, "shots", 3, True)
    assert made["counts"] == {
        "photos": 0,
        "shots": 10,
        "clips": 2,
        "used": 2,
        "unused": 8,
        "footage_with_tags": 1,
        "yes": 1,
        "no": 1,
    }
    assert made["counted_cuts"] == [
        {"id": shoot["cut"], "title": "Brookside Quick 30", "source": "import"}
    ]
    assert made["created_by"] == ADMIN["email"] and made["vocabulary_size"] == 1
    [entry] = made["entries"]
    assert (entry["state"], entry["message"], entry["done"], entry["total"]) == (
        "queued",
        comparisons.WAITING_TURN,
        0,
        10,
    )
    assert entry["measures"]["tags"] is None and entry["measures"]["scores"] is None
    assert entry["can_cancel"] is True and entry["can_carry_on"] is False
    assert len(pictures_of(site, cid)) == 30  # three stills a shot, frozen

    await site.drain()
    after = await site.one(cid)
    [entry] = after["entries"]
    assert (entry["state"], entry["done"], entry["failed"]) == ("done", 10, 0)
    assert (entry["runtime"], entry["runtime_label"]) == ("mlx-vlm", "MLX-VLM")
    assert entry["roles"] == [] and entry["installed"] is True
    assert after["over"] == 10 and after["answers_stored"] == 10
    figures = entry["measures"]
    assert figures["form"] == {"asked": 10, "answered": 10, "second_tries": 0, "failed": {}}
    assert [t["floor"] for t in figures["tags"]] == [0.0, 0.6, 0.85]
    assert figures["tags"][0] | {"proposed": 0, "unjudged": 0, "unjudged_new": 0} == {
        "floor": 0.0,
        "footage": 1,
        "yes_total": 1,
        "yes_found": 1,
        "no_total": 1,
        "no_repeated": 1,
        "proposed": 0,
        "unjudged": 0,
        "unjudged_new": 0,
    }
    assert figures["tags"][1]["no_repeated"] == 0  # it was only half sure of the sunset
    assert figures["scores"]["pairs"] == 16 and figures["scores"]["same"] == 16
    assert figures["scores"]["distinct"] == 1 and figures["scores"]["few"] is True
    assert figures["shots"]["answered"] == 10 and figures["photos"]["answered"] == 0

    items = await site.items(cid)
    assert items["total"] == 10
    by_used = [item for item in items["items"] if item["used"]]
    assert len(by_used) == 2 and by_used[0]["used_in"] == ["Brookside Quick 30"]
    one = by_used[0]
    assert one["filename"] == "DJI_0001.MP4" and one["folder"] == "Brookside/2026/Drone"
    assert one["library"] == "GELCO" and one["in_catalogue"] is True and one["counted"] is True
    assert one["truth"] == {"yes": ["barn"], "no": ["sunset"]}
    assert [p["n"] for p in one["pictures"]] == [0, 1, 2]
    answer = one["answers"][entry["id"]]
    # Strongest first; "sunset" is not a word the vocabulary had (nobody said yes to it).
    assert [(t["name"], t["ruling"], t["in_vocabulary"]) for t in answer["tags"]] == [
        ("barn", "yes", True),
        ("silo", None, False),
        ("sunset", "no", False),
    ]
    assert answer["score"] == 72 and answer["raw"] is None and answer["ok"] is True
    picture = await site.client.get(one["pictures"][1]["url"])
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/jpeg"
    assert picture.headers["cache-control"] == api.PICTURE_CACHE

    # The list now has it, and the builder's hint knows how long the model takes.
    page = (await site.client.get(URL)).json()
    [listed] = page["comparisons"]
    assert (listed["id"], listed["state"], listed["shots"], listed["size"]) == (
        cid,
        "finished",
        10,
        10,
    )
    assert listed["models"] == [
        {
            "entry_id": entry["id"],
            "name": "gemma-4-e4b-it-4bit",
            "state": "done",
            "done": 10,
            "failed": 0,
        }
    ]
    assert {m["id"]: m for m in page["models"]}[first]["seconds_hint"] is not None

    # Add a model that only fits overnight: it waits and says until when.
    await site.needs(second, 10.0)
    added = await site.client.post(f"{URL}/{cid}/models", json={"model_ids": [second]})
    assert added.status_code == 202, added.text
    assert [e["state"] for e in added.json()["entries"]] == ["done", "queued"]
    await site.drain()
    waiting = (await site.one(cid))["entries"][1]
    assert waiting["state"] == "waiting" and waiting["fits"] == "night"
    assert waiting["message"].startswith("Waiting for the overnight window (starts 21:30)")
    tonight = datetime(2026, 11, 12, 21, 30).astimezone(UTC)  # the Mac's 21:30, in UTC
    assert waiting["not_before"] == tonight.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert (await site.client.get(URL)).json()["comparisons"][0]["state"] == "waiting"

    # A decision is recorded; it changes no role.
    decided = await site.client.put(
        f"{URL}/{cid}", json={"verdict": "chosen", "entry_id": entry["id"], "note": "Steady."}
    )
    assert decided.status_code == 200, decided.text
    verdict = decided.json()["verdict"]
    assert (verdict["verdict"], verdict["entry_id"], verdict["note"]) == (
        "chosen",
        entry["id"],
        "Steady.",
    )
    assert verdict["by"] == ADMIN["email"] and verdict["at"] is not None
    renamed = await site.client.put(f"{URL}/{cid}", json={"name": "  Rev 2,   by day "})
    assert renamed.json()["name"] == "Rev 2, by day"
    assert renamed.json()["verdict"]["note"] == "Steady."  # a field left out is left alone
    listed = (await site.client.get(URL)).json()["comparisons"][0]
    assert (listed["verdict"], listed["verdict_model"]) == ("chosen", "gemma-4-e4b-it-4bit")

    # Compare again on the same sample: the same footage, a new comparison.
    again = await site.client.post(
        URL, json={"source": {"kind": "comparison", "comparison_id": cid}, "model_ids": [first]}
    )
    assert again.status_code == 201, again.text
    again_id = again.json()["id"]
    assert again.json()["source_kind"] == "comparison" and again.json()["size"] == 10
    spans = [(i["filename"], i["start_s"]) for i in (await site.items(cid))["items"]]
    assert [(i["filename"], i["start_s"]) for i in (await site.items(again_id))["items"]] == spans

    # Delete: refused while a model waits; cancelled, then gone with its pictures.
    refused = await site.client.delete(f"{URL}/{cid}")
    assert refused.status_code == 409 and message(refused) == comparisons.MODEL_RUNNING
    cancelled = await site.client.post(f"{URL}/{cid}/models/{waiting['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    stopped = cancelled.json()["entries"][1]
    assert (stopped["state"], stopped["message"]) == ("stopped", comparisons.CANCELLED)
    assert stopped["can_carry_on"] is True
    gone = await site.client.delete(f"{URL}/{cid}")
    assert gone.status_code == 204
    assert pictures_of(site, cid) == []
    assert (await site.client.get(f"{URL}/{cid}")).status_code == 404
    assert len(pictures_of(site, again_id)) == 30  # the other comparison's are untouched

    assert await site.events() == [
        "local_models.compare_started",
        "local_models.compare_model_added",
        "local_models.compare_decided",
        "local_models.compare_renamed",
        "local_models.compare_started",
        "local_models.compare_cancelled",
        "local_models.compare_deleted",
    ]
    async with site.factory() as db:
        started = await db.scalar(
            select(AuditLog).where(AuditLog.event == "local_models.compare_started").limit(1)
        )
        assert started is not None
        assert started.detail["source_kind"] == "cuts" and started.detail["items"] == 10
        assert started.detail["models"][0].startswith(f"{FIRST}@")


# --- refusals, each in its words -----------------------------------------------------------


async def test_a_sample_that_cannot_be_made_is_refused_with_the_reason(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)

    async def refused(body: dict[str, Any], path: str = "/preview") -> tuple[int, str]:
        answer = await site.client.post(URL + path, json=body)
        return answer.status_code, message(answer)

    folder = {"kind": "folder", "library_id": str(site.library), "folder": "Brookside"}
    assert await refused({"source": {"kind": "a hat"}}) == (400, api.NO_SOURCE)
    assert await refused({"source": {"kind": "listing", "listing_id": "nope"}}) == (400, api.BAD_ID)
    assert await refused({"source": {**folder, "library_id": str(uuid.uuid4())}}) == (
        404,
        api.NO_LIBRARY,
    )
    assert await refused({"source": {"kind": "listing", "listing_id": str(uuid.uuid4())}}) == (
        404,
        api.NO_LISTING,
    )
    assert await refused({"source": {"kind": "cuts", "cut_ids": [str(uuid.uuid4())]}}) == (
        404,
        api.NO_CUT,
    )
    assert await refused(
        {"source": {"kind": "comparison", "comparison_id": str(uuid.uuid4())}}
    ) == (
        404,
        api.NO_EARLIER,
    )
    assert await refused({"source": {**folder, "folder": "Brookside/../Elsewhere"}}) == (
        400,
        "A folder is a path inside the library; it cannot use . or .. to point elsewhere.",
    )
    assert await refused({"source": folder, "size": 5}) == (
        400,
        "A sample is between 10 and 200 items.",
    )
    assert await refused({"source": folder, "stills": True}) == (400, api.BAD_STILLS)
    assert await refused({"source": {"kind": "cuts", "cut_ids": [shoot["draft"]]}}) == (
        400,
        "A local draft is a draft, not finished work, so it cannot say which shots were used.",
    )

    # An empty sample is the owner still choosing: the preview says why, and Start refuses it.
    empty = {"source": {**folder, "folder": "Somewhere/Else"}}
    shown = await site.client.post(f"{URL}/preview", json=empty)
    assert shown.status_code == 200
    assert shown.json()["ok"] is False
    assert shown.json()["notes"][0].startswith(api.NOTHING_HERE)
    status, said = await refused({**empty, "model_ids": [first]}, "")
    assert status == 400 and said.startswith(api.NOTHING_HERE)

    # Photographs only, one with a decided tag: no score sentence, no per-clip one.
    photos = await site.client.post(f"{URL}/preview", json={"source": folder, "what": "photos"})
    assert photos.json()["photos"] == 4 and photos.json()["notes"] == []
    assert await site.rows(Comparison) == 0


async def test_starting_and_adding_refuse_each_model_problem_in_its_words(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    text_only = await site.install(TEXT_ONLY)
    sample = {"source": {"kind": "cuts", "cut_ids": [shoot["cut"]]}, "size": 10}

    async def start(**more: Any) -> tuple[int, str]:
        answer = await site.client.post(URL, json={**sample, **more})
        return answer.status_code, message(answer)

    assert await start() == (400, api.CHOOSE_A_MODEL)
    assert await start(model_ids=[]) == (400, api.CHOOSE_A_MODEL)
    assert await start(model_ids=[str(uuid.uuid4()) for _ in range(7)]) == (400, api.AT_MOST_SIX)
    assert await start(model_ids=[str(uuid.uuid4())]) == (404, "That model is no longer installed.")
    assert await start(model_ids=[first, text_only]) == (
        409,
        "gemma-4-12B-it-OptiQ-4bit does not read pictures.",
    )
    assert await start(model_ids=[first], name="x" * 201) == (400, comparisons.NAME_TOO_LONG)
    assert await start(model_ids=[first], seed="seven") == (400, api.BAD_SEED)
    assert await site.rows(Comparison) == 0 and site.woken == []

    # A model ticked twice is one column; a name left blank is the sample and the date.
    made = await site.start([first, first], cut=shoot["cut"], name="  ")
    assert len(made["entries"]) == 1
    assert made["name"].startswith("Shots used in Brookside Quick 30")
    cid = made["id"]

    async def add(ids: list[str]) -> tuple[int, str]:
        answer = await site.client.post(f"{URL}/{cid}/models", json={"model_ids": ids})
        return answer.status_code, message(answer)

    assert await add([first]) == (
        409,
        f"{comparisons.build_of(await _model(site, first))} is already in this comparison.",
    )
    assert await add([text_only]) == (409, "gemma-4-12B-it-OptiQ-4bit does not read pictures.")
    assert await add([]) == (400, api.CHOOSE_A_MODEL)
    async with site.factory() as db:
        await db.execute(
            update(Comparison)
            .where(Comparison.id == uuid.UUID(cid))
            .values(prompt_version="look-0")
        )
        await db.commit()
    assert await add([first]) == (409, comparisons.older("look-0"))
    page = await site.one(cid)
    assert page["current"] is False and page["can_add_models"] is False
    assert page["entries"][0]["can_carry_on"] is False
    assert await site.rows(ComparisonModel) == 1


async def test_a_model_whose_program_is_not_installed_is_refused_before_anything_starts(
    site: Site,
) -> None:
    """A comparison runs the model now, so a model whose program is missing is
    not offered (its row says why, in the words its turn would have stopped
    with) and Start, Add and Carry on refuse it before a job is queued."""
    shoot = await site.shoot()
    first = await site.install(FIRST)
    second = await site.install(SECOND)
    fake.FakeModelServer.answer_for("LookAnswer", lambda _body: look())
    made = await site.start([first], cut=shoot["cut"])
    await site.drain()
    cid, eid = made["id"], made["entries"][0]["id"]
    woken = list(site.woken)

    shutil.rmtree(site.tmp / "runtimes" / "mlx-vlm")
    said = (
        "MLX-VLM is not installed. Install it under Software that runs models on the "
        "AI models page."
    )
    page = (await site.client.get(URL)).json()
    choices = {m["id"]: m for m in page["models"]}
    for model_id in (first, second):
        offered = choices[model_id]
        assert (offered["can_compare"], offered["reason"], offered["program_missing"]) == (
            False,
            said,
            True,
        )
    named = f"{comparisons.name_of(await _model(site, second))}: {said}"

    started = await site.client.post(
        URL,
        json={
            "source": {"kind": "cuts", "cut_ids": [shoot["cut"]]},
            "size": 10,
            "model_ids": [second],
        },
    )
    assert (started.status_code, message(started)) == (409, named)
    added = await site.client.post(f"{URL}/{cid}/models", json={"model_ids": [second]})
    assert (added.status_code, message(added)) == (409, named)
    again = await site.client.post(f"{URL}/{cid}/models/{eid}/run", json={})
    assert again.status_code == 409
    assert message(again) == f"{comparisons.name_of(await _model(site, first))}: {said}"
    assert await site.rows(Comparison) == 1 and await site.rows(ComparisonModel) == 1
    assert site.woken == woken

    # A model held back for another reason is not sent to install a program.
    text_only = await site.install(TEXT_ONLY)
    page = (await site.client.get(URL)).json()
    held = {m["id"]: m for m in page["models"]}[text_only]
    assert (held["reason"], held["program_missing"]) == ("It does not read pictures.", False)


async def _model(site: Site, model_id: str) -> LocalModel:
    async with site.factory() as db:
        found = await registry.get_model(db, uuid.UUID(model_id))
        assert found is not None
        return found


async def test_changing_a_column_refuses_what_its_state_does_not_allow(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    made = await site.start([first], cut=shoot["cut"])
    cid, eid = made["id"], made["entries"][0]["id"]

    async def call(method: str, path: str, body: Any = None) -> tuple[int, str]:
        answer = await site.client.request(method, f"{URL}/{cid}{path}", json=body)
        return answer.status_code, message(answer)

    assert await call("POST", f"/models/{eid}/run", {}) == (409, comparisons.ALREADY_RUNNING)
    assert await call("DELETE", f"/models/{eid}") == (409, comparisons.STILL_RUNNING)
    assert await call("POST", f"/models/{eid}/run", {"retry_failed": "yes"}) == (
        400,
        api.BAD_RETRY,
    )
    await site.drain()
    assert await call("POST", f"/models/{eid}/run", {}) == (409, comparisons.ANSWERED_ALL)
    assert await call("POST", f"/models/{eid}/cancel") == (409, comparisons.NOT_RUNNING)
    assert await call("PUT", "", {"verdict": "chosen"}) == (400, comparisons.SAY_WHICH)
    assert await call("PUT", "", {"verdict": "best"}) == (400, comparisons.NO_SUCH_DECISION)
    assert await call("PUT", "", {"verdict": "chosen", "entry_id": str(uuid.uuid4())}) == (
        400,
        comparisons.NOT_HERE,
    )
    assert await call("PUT", "", {"name": ""}) == (400, comparisons.GIVE_NAME)
    assert await call("PUT", "", {"note": "n" * 2001}) == (400, comparisons.NOTE_TOO_LONG)
    nobody = str(uuid.uuid4())
    assert await call("POST", f"/models/{nobody}/run", {}) == (404, comparisons.NOT_HERE)
    assert await call("GET", f"/items/{nobody}/pictures/0") == (404, api.NO_SUCH_ITEM)
    for wrong in (nobody, "not-an-id"):
        answer = await site.client.get(f"{URL}/{wrong}")
        assert answer.status_code == 404 and message(answer) == api.NO_SUCH_COMPARISON

    # Taking out the chosen column clears the decision and keeps the note.
    await site.client.put(f"{URL}/{cid}", json={"verdict": "chosen", "entry_id": eid, "note": "x"})
    removed = await site.client.delete(f"{URL}/{cid}/models/{eid}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["entries"] == []
    assert removed.json()["verdict"] | {"at": None} == {
        "verdict": "",
        "entry_id": None,
        "note": "x",
        "at": None,
        "by": None,
    }
    assert await site.rows(ComparisonAnswer) == 0
    assert (await site.events())[-1] == "local_models.compare_model_removed"


# --- states, and their ways out -------------------------------------------------------------


async def test_a_bad_answer_is_a_finding_and_can_be_asked_again(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    told: list[int] = []

    def answers(_body: dict[str, Any]) -> str:
        told.append(1)
        # The first item gets prose twice (the question and its repair turn).
        return "It is a barn, I think." if len(told) <= 2 else look()

    fake.FakeModelServer.answer_for("LookAnswer", answers)
    made = await site.start([first], cut=shoot["cut"])
    cid, eid = made["id"], made["entries"][0]["id"]
    await site.drain()
    entry = (await site.one(cid))["entries"][0]
    assert (entry["state"], entry["done"], entry["failed"]) == ("done", 9, 1)
    assert entry["measures"]["form"]["failed"] == {"bad_answer": 1}
    assert entry["can_retry_failed"] is True and entry["can_carry_on"] is False
    failed = next(i for i in (await site.items(cid))["items"] if not i["answers"][eid]["ok"])
    assert failed["answers"][eid]["problem"] == "bad_answer"
    assert failed["answers"][eid]["raw"] == "It is a barn, I think."
    assert failed["counted"] is False

    again = await site.client.post(f"{URL}/{cid}/models/{eid}/run", json={"retry_failed": True})
    assert again.status_code == 202, again.text
    assert again.json()["entries"][0]["state"] == "queued"
    await site.drain()
    entry = (await site.one(cid))["entries"][0]
    assert (entry["state"], entry["done"], entry["failed"]) == ("done", 10, 0)
    assert (await site.events())[-1] == "local_models.compare_resumed"


async def test_a_worker_that_died_reads_as_stopped_with_carry_on(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    made = await site.start([first], cut=shoot["cut"])
    cid, eid = made["id"], made["entries"][0]["id"]
    # The worker took the turn, answered nothing, and was killed.
    async with site.factory() as db:
        await db.execute(
            update(LocalAiJob).values(
                state="running",
                started_at=datetime(2026, 11, 12, 9, 0, tzinfo=UTC),
                beat_at=datetime(2026, 11, 12, 9, 0, tzinfo=UTC),
            )
        )
        await db.execute(
            update(ComparisonModel)
            .where(ComparisonModel.id == uuid.UUID(eid))
            .values(state="running")
        )
        await db.commit()
    entry = (await site.one(cid))["entries"][0]
    assert (entry["state"], entry["message"]) == ("stopped", comparisons.STOPPED)
    assert entry["can_carry_on"] is True and entry["can_cancel"] is False
    carried = await site.client.post(f"{URL}/{cid}/models/{eid}/run", json={})
    assert carried.status_code == 202, carried.text
    await site.drain()
    entry = (await site.one(cid))["entries"][0]
    assert (entry["state"], entry["done"]) == ("done", 10)


async def test_a_model_removed_later_keeps_its_column(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    made = await site.start([first], cut=shoot["cut"])
    cid, eid = made["id"], made["entries"][0]["id"]
    await site.drain()
    before = (await site.one(cid))["entries"][0]
    async with site.factory() as db:
        await registry.delete_model(db, uuid.UUID(first))
        await db.commit()
    after = (await site.one(cid))["entries"][0]
    assert after["installed"] is False and after["fits"] is None and after["roles"] is None
    assert after["measures"] == before["measures"] and after["done"] == 10
    assert after["name"] == before["name"] == "gemma-4-e4b-it-4bit"
    assert after["can_carry_on"] is False and after["can_retry_failed"] is False
    assert (await site.client.get(URL)).json()["models"] == []
    refused = await site.client.post(f"{URL}/{cid}/models/{eid}/run", json={})
    assert refused.status_code == 409 and message(refused) == comparisons.NOT_INSTALLED


async def test_a_saved_comparison_reads_the_same_after_the_catalogue_changed(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    fake.FakeModelServer.answer_for("LookAnswer", lambda _body: look())
    made = await site.start([first], cut=shoot["cut"])
    cid = made["id"]
    await site.drain()
    figures, items = await site.one(cid), await site.items(cid)

    # A ruling taken back, the clips re-analysed (their windows rebuilt), the
    # stills regenerated elsewhere, the clip beside deleted.
    async with site.factory() as db:
        await db.execute(delete(AssetTag))
        await db.execute(delete(ShotWindow))
        await db.execute(delete(Asset).where(Asset.id == shoot["beside"]))
        await db.commit()
    for still in (site.data / "shots").rglob("*.jpeg"):
        still.unlink()

    again = await site.one(cid)
    assert again["entries"][0]["measures"] == figures["entries"][0]["measures"]
    assert again["counts"] == figures["counts"]
    shown = await site.items(cid)
    gone = [item for item in shown["items"] if item["in_catalogue"] is False]
    assert gone and all(item["filename"] == "DJI_0002.MP4" for item in gone)

    def kept(found: dict[str, Any]) -> list[dict[str, Any]]:
        return [{k: v for k, v in i.items() if k != "in_catalogue"} for i in found["items"]]

    assert kept(shown) == kept(items)
    picture = await site.client.get(gone[0]["pictures"][0]["url"])
    assert picture.status_code == 200

    # What a model wrote is shown only where it is worth reading: a failure or
    # a second try, never beside an answer that came right the first time.
    async with site.factory() as db:
        await db.execute(update(ComparisonAnswer).values(raw="{...}"))
        await db.commit()
    assert all(
        answer["raw"] is None
        for item in (await site.items(cid))["items"]
        for answer in item["answers"].values()
    )


async def test_the_picture_route_serves_only_this_comparisons_files(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    one = await site.start([first], cut=shoot["cut"])
    other = await site.start([first], cut=shoot["cut"])
    item = (await site.items(one["id"]))["items"][0]
    there = f"{URL}/{one['id']}/items/{item['id']}/pictures"
    assert (await site.client.get(f"{there}/2")).status_code == 200
    for wrong in (
        f"{URL}/{other['id']}/items/{item['id']}/pictures/0",
        f"{there}/3",
        f"{there}/-1",
        f"{there}/1.0",
        f"{there}/%2E%2E",
    ):
        answer = await site.client.get(wrong)
        assert answer.status_code == 404, wrong
    # Digits to `str.isdigit`, not to `int`: the sentence, never a 500.
    for wrong in (f"{there}/%C2%B2", f"{there}/%E2%91%A0", f"{there}/{'1' * 5000}"):
        answer = await site.client.get(wrong)
        assert answer.status_code == 404 and message(answer) == api.NO_SUCH_PICTURE, wrong
    sampling.picture_path(uuid.UUID(one["id"]), item["position"], 0, get_settings()).unlink()
    missing = await site.client.get(f"{there}/0")
    assert missing.status_code == 404 and message(missing) == api.NO_SUCH_PICTURE


# --- who may, and where ------------------------------------------------------------------


async def test_a_reader_can_look_but_not_change(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    made = await site.start([first], cut=shoot["cut"])
    cid, eid = made["id"], made["entries"][0]["id"]
    async with site.factory() as db:
        await db.execute(
            update(ComparisonModel)
            .where(ComparisonModel.id == uuid.UUID(eid))
            .values(state="failed", message="It would not load.", detail="/Volumes/Models/x: no")
        )
        await db.commit()
    reader = await site.reader()
    try:
        page = (await reader.get(URL)).json()
        assert page["can_manage"] is False and len(page["comparisons"]) == 1
        one = await site.one(cid, reader)
        assert one["can_manage"] is False and one["can_add_models"] is False
        entry = one["entries"][0]
        assert entry["detail"] is None and entry["message"] == "It would not load."
        assert not any(entry[k] for k in ("can_cancel", "can_carry_on", "can_retry_failed"))
        assert (await site.one(cid))["entries"][0]["detail"] == "/Volumes/Models/x: no"
        item = (await reader.get(f"{URL}/{cid}/items")).json()["items"][0]
        assert (await reader.get(item["pictures"][0]["url"])).status_code == 200
        preview = await reader.post(
            f"{URL}/preview", json={"source": {"kind": "cuts", "cut_ids": [shoot["cut"]]}}
        )
        assert preview.status_code == 200
        for method, path, body in (
            ("POST", "", {"model_ids": [first]}),
            ("POST", f"/{cid}/models", {"model_ids": [first]}),
            ("POST", f"/{cid}/models/{eid}/run", {}),
            ("POST", f"/{cid}/models/{eid}/cancel", None),
            ("DELETE", f"/{cid}/models/{eid}", None),
            ("PUT", f"/{cid}", {"name": "Mine now"}),
            ("DELETE", f"/{cid}", None),
        ):
            answer = await reader.request(method, URL + path, json=body)
            assert answer.status_code == 403, path
    finally:
        await reader.aclose()
    assert await site.events() == ["local_models.compare_started"]


async def test_on_a_server_without_runtimes_every_write_is_refused_plainly(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    made = await site.start([first], cut=shoot["cut"])
    cid, eid = made["id"], made["entries"][0]["id"]
    site.unconfigured()
    page = (await site.client.get(URL)).json()
    assert (page["mode"], page["note"], page["can_manage"]) == ("unavailable", UNAVAILABLE, False)
    assert page["models"] == [] and [c["id"] for c in page["comparisons"]] == [cid]
    assert (await site.one(cid))["can_manage"] is False
    somebody = str(uuid.uuid4())
    for method, path, body in (
        ("POST", "", {"source": {"kind": "cuts", "cut_ids": [shoot["cut"]]}, "model_ids": [first]}),
        ("POST", f"/{cid}/models", {"model_ids": [first]}),
        ("POST", f"/{cid}/models/{eid}/run", {}),
        ("POST", f"/{cid}/models/{eid}/cancel", None),
        ("DELETE", f"/{cid}/models/{eid}", None),
        ("PUT", f"/{cid}", {"name": "Renamed"}),
        ("DELETE", f"/{cid}", None),
        ("DELETE", f"/{somebody}", None),
    ):
        answer = await site.client.request(method, URL + path, json=body)
        assert answer.status_code == 409, path
        assert message(answer) == UNAVAILABLE, path
    assert await site.events() == ["local_models.compare_started"]


async def test_a_queue_that_is_down_leaves_no_rows_and_no_pictures(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    second = await site.install(SECOND)
    site.queue_up = False
    answer = await site.client.post(
        URL,
        json={"source": {"kind": "cuts", "cut_ids": [shoot["cut"]]}, "model_ids": [first]},
    )
    assert answer.status_code == 503 and message(answer) == l1_api.QUEUE_DOWN
    for table in (Comparison, ComparisonItem, ComparisonModel, LocalAiJob):
        assert await site.rows(table) == 0, table
    folder = site.data / sampling.FOLDER
    assert not folder.exists() or list(folder.iterdir()) == []

    site.queue_up = True
    made = await site.start([first], cut=shoot["cut"])
    cid, eid = made["id"], made["entries"][0]["id"]
    await site.drain()
    site.queue_up = False
    added = await site.client.post(f"{URL}/{cid}/models", json={"model_ids": [second]})
    assert added.status_code == 503 and message(added) == l1_api.QUEUE_DOWN
    assert await site.rows(ComparisonModel) == 1 and await site.rows(LocalAiJob) == 1

    async with site.factory() as db:
        await db.execute(
            update(ComparisonAnswer)
            .where(ComparisonAnswer.entry_id == uuid.UUID(eid))
            .values(ok=False, problem="timeout", message="It took too long to answer.")
        )
        await db.execute(
            update(ComparisonModel)
            .where(ComparisonModel.id == uuid.UUID(eid))
            .values(detail="the runtime's last lines")
        )
        await db.commit()
    before = (await site.one(cid))["entries"][0]
    items_before = await site.items(cid)
    async with site.factory() as db:
        finished = await comparisons.last_finished_for(db, uuid.UUID(first))
    assert finished is not None and before["failed"] == 10

    retried = await site.client.post(f"{URL}/{cid}/models/{eid}/run", json={"retry_failed": True})
    assert retried.status_code == 503 and message(retried) == l1_api.QUEUE_DOWN
    # "Nothing was started" is true: the failed answers, the column's state,
    # its details and when it finished are all as they were.
    assert (await site.one(cid))["entries"][0] == before
    assert await site.items(cid) == items_before
    assert await site.rows(ComparisonAnswer) == 10
    async with site.factory() as db:
        assert await comparisons.last_finished_for(db, uuid.UUID(first)) == finished
    assert before["can_retry_failed"] is True
    assert await site.rows(LocalAiJob) == 1
    assert await site.events() == ["local_models.compare_started"]


class _LostConnection(Exception):
    """The database went away at the worst moment."""


async def test_a_start_whose_commit_fails_leaves_no_pictures_behind(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    armed: list[bool] = []
    real_add, real_commit = comparisons.add_models, AsyncSession.commit

    async def add_then_arm(*args: Any, **kwargs: Any) -> Any:
        made = await real_add(*args, **kwargs)
        armed.append(True)  # the pictures are frozen; the next commit is lost
        return made

    async def commit(self: AsyncSession) -> None:
        if armed:
            armed.clear()
            raise _LostConnection()
        await real_commit(self)

    monkeypatch.setattr(comparisons, "add_models", add_then_arm)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    # The app answers 500; the test transport may hand the error on as well.
    with contextlib.suppress(_LostConnection):
        answer = await site.client.post(
            URL,
            json={"source": {"kind": "cuts", "cut_ids": [shoot["cut"]]}, "model_ids": [first]},
        )
        assert answer.status_code == 500
    for table in (Comparison, ComparisonItem, ComparisonModel, LocalAiJob):
        assert await site.rows(table) == 0, table
    folder = site.data / sampling.FOLDER
    assert not folder.exists() or list(folder.iterdir()) == []


async def test_deleting_answers_204_even_when_a_picture_cannot_be_removed(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    made = await site.start([first], cut=shoot["cut"])
    await site.drain()

    def in_use(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("the file is being served")

    monkeypatch.setattr(sampling, "drop_pictures", in_use)
    gone = await site.client.delete(f"{URL}/{made['id']}")
    assert gone.status_code == 204
    assert await site.rows(Comparison) == 0
    assert (await site.events())[-1] == "local_models.compare_deleted"


async def test_a_column_answered_to_the_end_but_left_open_offers_carry_on(site: Site) -> None:
    shoot = await site.shoot()
    first = await site.install(FIRST)
    made = await site.start([first], cut=shoot["cut"])
    cid, eid = made["id"], made["entries"][0]["id"]
    await site.drain()
    assert (await site.one(cid))["entries"][0]["can_carry_on"] is False  # done: nothing to do
    # The worker died after the last answer, before it closed the column.
    async with site.factory() as db:
        await db.execute(update(LocalAiJob).values(state="failed", message=jobs.STOPPED))
        await db.execute(
            update(ComparisonModel)
            .where(ComparisonModel.id == uuid.UUID(eid))
            .values(state="stopped", message=comparisons.STOPPED, finished_at=None)
        )
        await db.commit()
    entry = (await site.one(cid))["entries"][0]
    assert (entry["state"], entry["done"], entry["can_carry_on"]) == ("stopped", 10, True)
    carried = await site.client.post(f"{URL}/{cid}/models/{eid}/run", json={})
    assert carried.status_code == 202, carried.text
    asked = len(site.server.requests)
    await site.drain()
    entry = (await site.one(cid))["entries"][0]
    assert (entry["state"], entry["done"], entry["can_carry_on"]) == ("done", 10, False)
    assert entry["finished_at"] is not None
    assert len(site.server.requests) == asked


# --- demo mode and the seed (S7) ---------------------------------------------------------

SEED = Path(__file__).resolve().parents[3] / "infrastructure" / "scripts" / "seed_dev.py"


def _demo_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> str:
    """A review copy's environment, as CONTRIBUTING.md runs the API: demo mode on,
    no runtimes folder, everything under `root`."""
    url = f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}"
    for name, value in {
        "FRAMEFOUND_DATABASE_URL": url,
        "FRAMEFOUND_DATA_DIR": str(root / "data"),
        "FRAMEFOUND_MEDIA_ROOT": str(root / "media"),
        "FRAMEFOUND_MODELS_DIR": str(root / "models"),
        "FRAMEFOUND_LOCAL_AI_DEMO": "1",
        "FRAMEFOUND_MIN_FREE_GB": "0",
        "FRAMEFOUND_SETUP_TOKEN": TEST_SETUP_TOKEN,
        "FRAMEFOUND_SECRET_KEY": "compare-demo-secret",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    return url


@contextlib.asynccontextmanager
async def _review_app(url: str) -> AsyncIterator[tuple[AsyncClient, Any, Factory]]:
    """The real app over the review catalogue, signed in as its admin, with
    nothing of the demo handed in: only the database session is the test's."""
    engine = create_async_engine(url)
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
            assert setup.status_code == 201, setup.text
            yield client, app, factory
    finally:
        # The demo's lane runs on a thread of its own; it must not outlive the test.
        assert l1_demo.stop(), "the demo's lane did not stop"
        await engine.dispose()


@pytest.fixture()
def demo_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The demo as fast as the machine allows, from no lane and a fresh model server."""
    monkeypatch.setattr(ask, "_FAKE", None)
    monkeypatch.setattr(l1_demo, "SECONDS_PER_CHUNK", 0.0)
    monkeypatch.setattr(l1_demo, "ANSWER_SECONDS", 0.0)
    assert l1_demo.stop(), "a demo lane left by an earlier test did not stop"
    yield
    assert l1_demo.stop(), "the demo's lane did not stop"


async def _run_to_the_end(client: AsyncClient, comparison_id: str) -> dict[str, Any]:
    """Read the comparison as the page does until no column is still at work."""
    for _ in range(600):
        found = await client.get(f"{URL}/{comparison_id}")
        assert found.status_code == 200, found.text
        page: dict[str, Any] = found.json()
        if all(e["state"] not in ("queued", "running", "waiting") for e in page["entries"]):
            return page
        await asyncio.sleep(0.05)
    raise AssertionError("the demo's lane never finished the comparison")


async def _demo_start(
    client: AsyncClient, factory: Factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Brookside's shoot and the demo's own models in the review copy, then Start
    on the shots of its finished cut with the Looker, as the builder sends it."""
    site = Site(tmp_path, monkeypatch, client, None)
    site.factory = factory
    shoot = await site.shoot()
    await l1_demo.seed(factory, get_settings())

    page = (await client.get(URL)).json()
    assert page["mode"] == "demo" and page["note"] == l1_demo.DEMO_NOTE
    looker = next(m for m in page["models"] if m["repo_id"] == FIRST)
    assert looker["can_compare"] is True and looker["demo"] is True
    started = await client.post(
        URL,
        json={
            "source": {"kind": "cuts", "cut_ids": [shoot["cut"]]},
            "size": 10,
            "model_ids": [looker["id"]],
        },
    )
    assert started.status_code == 201, started.text
    made: dict[str, Any] = started.json()
    return made


async def test_the_demo_runs_a_comparison_with_no_fake_injected_by_the_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, demo_mode: None
) -> None:
    url = _demo_env(monkeypatch, tmp_path)
    await _schema(url)
    async with _review_app(url) as (client, app, factory):
        made = await _demo_start(client, factory, tmp_path, monkeypatch)
        # Nothing of the demo was handed in: no deps, no queue, no model server.
        assert set(app.dependency_overrides) == {get_session}
        page = await _run_to_the_end(client, made["id"])
        [entry] = page["entries"]
        assert (entry["state"], entry["done"], entry["failed"]) == ("done", 10, 0)
        assert entry["demo"] is True and entry["runtime"] == "demo"
        # The demo server reports this model a little under its estimate, as its
        # Test on the AI models page does; that figure is kept.
        assert entry["memory_gb"] == 5.2
        items = (await client.get(f"{URL}/{made['id']}/items")).json()["items"]

    assert len(items) == 10
    for item in items:
        answer = item["answers"][entry["id"]]
        seed = f"{FIRST}:{item['id']}"
        # The demo's look, not the fake server's random words: a stock sentence,
        # a reason that says nobody looked, and only the catalogue's own words.
        assert answer["ok"] is True and answer["attempts"] == 1
        assert answer["caption"] in looks_demo.SHOT_CAPTIONS
        assert answer["reason"] == looks_demo.REASON
        assert answer["tags"] and all(tag["in_vocabulary"] for tag in answer["tags"])
        assert {tag["name"] for tag in answer["tags"]} <= {"barn"}
        # Its time is invented per model and item, not the server's one figure.
        assert answer["seconds"] == looks_demo.figures_for(seed)[0]
    assert len({item["answers"][entry["id"]]["seconds"] for item in items}) > 1
    # Made up, but the same for the same item: asking again gives the same look.
    first = items[0]
    again = looks_demo.answer_for(f"{FIRST}:{first['id']}", ["barn"], shot=True)
    assert first["answers"][entry["id"]]["score"] == again.score


async def test_only_an_answer_the_demo_server_gave_is_made_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, demo_mode: None
) -> None:
    # A model's own answer (demo false) is stored exactly as given, even in demo
    # mode; one the demo server gave (demo true) becomes the demo's look.
    real = prompt.LookAnswer(
        caption="A red barn from the north side.",
        score=64,
        reason="The barn is cut off at the top.",
        matches=[prompt.TagGuess(tag="barn", confidence=0.9)],
        new_tags=[prompt.TagGuess(tag="silo", confidence=0.7)],
    )
    asked: list[uuid.UUID] = []

    async def scripted(_db: AsyncSession, model_id: uuid.UUID, **_asked: Any) -> Answer:
        asked.append(model_id)
        return Answer(
            text=real.model_dump_json(),
            parsed=real,
            model_id=model_id,
            repo_id=FIRST,
            revision="475b9088d29754a3379866cf5aeb6b41acd313c2",
            runtime="demo",
            runtime_version="1.9",
            prompt_version=prompt.PROMPT_VERSION,
            seconds=7.25,
            load_seconds=0.0,
            memory_gb=4.4,
            memory_source="runtime",
            finish_reason="stop",
            ran_from="this_machine",
            attempts=1,
            demo=len(asked) % 2 == 0,  # every second answer came from the demo server
        )

    monkeypatch.setattr(compare_job, "ask", scripted)
    url = _demo_env(monkeypatch, tmp_path)
    await _schema(url)
    async with _review_app(url) as (client, _app, factory):
        made = await _demo_start(client, factory, tmp_path, monkeypatch)
        page = await _run_to_the_end(client, made["id"])
        [entry] = page["entries"]
        items = (await client.get(f"{URL}/{made['id']}/items")).json()["items"]

    assert len(asked) == 10 and entry["state"] == "done"
    for item in items:
        answer = item["answers"][entry["id"]]
        if item["position"] % 2 == 0:  # the first, third, … answer: demo false
            assert (answer["caption"], answer["score"], answer["reason"]) == (
                real.caption,
                real.score,
                real.reason,
            )
            assert [(t["name"], t["confidence"]) for t in answer["tags"]] == [
                ("barn", 0.9),
                ("silo", 0.7),
            ]
            assert answer["seconds"] == 7.25
        else:
            assert answer["caption"] in looks_demo.SHOT_CAPTIONS
            assert answer["reason"] == looks_demo.REASON
            assert answer["seconds"] == looks_demo.figures_for(f"{FIRST}:{item['id']}")[0]
    # The memory a model reported is kept either way.
    assert entry["memory_gb"] == 4.4


async def test_a_demo_model_the_server_reports_no_memory_for_stays_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, demo_mode: None
) -> None:
    # As for the seed's demo models, whose pages the running demo's hub does
    # not hold: its server answers with no memory figure. The answers are still
    # the demo's looks, but no figure is invented in place of the missing one —
    # the page would show it as one "the software reports".
    monkeypatch.setattr(l1_demo, "_measured", lambda _facts: None)
    url = _demo_env(monkeypatch, tmp_path)
    await _schema(url)
    async with _review_app(url) as (client, _app, factory):
        made = await _demo_start(client, factory, tmp_path, monkeypatch)
        page = await _run_to_the_end(client, made["id"])
        [entry] = page["entries"]
        items = (await client.get(f"{URL}/{made['id']}/items")).json()["items"]

    assert (entry["state"], entry["done"]) == ("done", 10)
    assert entry["memory_gb"] is None and entry["memory_source"] is None
    assert all(item["answers"][entry["id"]]["reason"] == looks_demo.REASON for item in items)


def _seed_script(root: Path, *more: str) -> dict[str, Any]:
    """`python infrastructure/scripts/seed_dev.py <root> …`, in this process (on a
    thread: the script runs its own event loop). Its globals, for its constants."""
    found: dict[str, Any] = {}
    failed: list[BaseException] = []
    argv = sys.argv

    def run() -> None:
        try:
            found.update(runpy.run_path(str(SEED), run_name="__main__"))
        except BaseException as err:  # noqa: BLE001 - handed back to the test to raise
            failed.append(err)

    sys.argv = [str(SEED), str(root), *more]
    try:
        worker = threading.Thread(target=run)
        worker.start()
        worker.join(300)
    finally:
        sys.argv = argv
    assert not worker.is_alive(), "the seed did not finish"
    if failed:
        raise failed[0]
    get_settings.cache_clear()
    return found


async def _counts(factory: Factory) -> dict[str, int]:
    tables = (
        ShotWindow,
        Cut,
        CutPiece,
        AssetTag,
        LocalModel,
        Comparison,
        ComparisonItem,
        ComparisonAnswer,
        LocalAiJob,
    )
    async with factory() as db:
        return {
            table.__tablename__: int(await db.scalar(select(func.count()).select_from(table)) or 0)
            for table in tables
        }


async def test_the_seed_makes_a_finished_comparison_with_figures_and_never_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    demo_mode: None,
) -> None:
    root = tmp_path / "ff-ui"
    root.mkdir()
    url = _demo_env(monkeypatch, root)
    await _schema(url)
    script = _seed_script(root, "--compare")
    said = capsys.readouterr().out
    name, small, night = script["SEEDED_COMPARISON"], script["SMALL_MODEL"], script["NIGHT_MODEL"]
    [added] = [line for line in said.splitlines() if line.startswith("Compare:")]
    assert added.startswith("Compare: added ")
    assert f'the comparison "{name}"' in added and small in added and night in added

    async with _review_app(url) as (client, _app, factory):
        page = (await client.get(URL)).json()
        [listed] = [c for c in page["comparisons"] if c["name"] == name]
        assert (listed["state"], listed["source_kind"], listed["shots"]) == (
            "finished",
            "folder",
            15,
        )
        assert [m["state"] for m in listed["models"]] == ["done", "done"]
        # Three models that read pictures can be compared, so a new comparison
        # can be built; one fits only overnight, so its column waits for the
        # window. The seed's GGUF build reads pictures too, but its program is
        # left uninstalled: it is offered once that is installed, not before
        # (its turn would stop at once).
        readers = {m["repo_id"]: m for m in page["models"] if m["can_compare"]}
        assert set(readers) == {FIRST, small, night}
        assert (readers[small]["fits"], readers[night]["fits"]) == ("day", "night")
        [gguf] = [m for m in page["models"] if m["repo_id"] == l1_demo.GGUF_MODEL]
        assert gguf["program_missing"] is True
        assert gguf["reason"] == (
            "Demo llama.cpp (not a real program) is not installed. Install it under Software "
            "that runs models on the AI models page."
        )

        one = await _one(client, listed["id"])
        assert one["counted_cuts"][0]["title"] == script["QUICK_30"]
        assert (one["counts"]["used"], one["counts"]["unused"]) == (5, 10)
        assert (one["counts"]["yes"], one["counts"]["no"]) == (9, 3)
        # Counted over the fourteen items both models answered (one answer of
        # demo-small's was not the form asked for).
        assert one["over"] == 14
        looker, small = one["entries"]
        for entry in (looker, small):
            assert entry["state"] == "done" and entry["measures"]["tags"]
            assert entry["measures"]["scores"]["pairs"] == 5 * 9
        assert small["measures"]["form"]["failed"] == {"bad_answer": 1}
        # Figures worth reading: the Looker set the used shots above the rest
        # far more often than not; demo-small scored nearly everything alike.
        assert looker["measures"]["scores"]["higher"] > 30
        assert small["measures"]["scores"]["distinct"] == 3
        items = (await client.get(f"{URL}/{listed['id']}/items")).json()
        assert items["total"] == 15
        assert sum(1 for item in items["items"] if item["used"]) == 5
        assert all(len(item["pictures"]) == 3 for item in items["items"])
        picture = await client.get(items["items"][0]["pictures"][0]["url"])
        assert picture.status_code == 200
        assert picture.headers["content-type"] == "image/jpeg"
        # A reviewer renames the comparison and the cut: they are still the seed's.
        renamed = await client.put(f"{URL}/{listed['id']}", json={"name": "Drone shots, renamed"})
        assert renamed.status_code == 200, renamed.text
        async with factory() as db:
            await db.execute(
                update(Cut).where(Cut.title == script["QUICK_30"]).values(title="Quick 30, v2")
            )
            await db.commit()
        before = await _counts(factory)

    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Without --compare the seed leaves Compare alone: the AI models page's
        # own seed (and the test that pins its three models) is untouched by it.
        _seed_script(root, "--local-ai-only")
        assert "Compare:" not in capsys.readouterr().out
        assert await _counts(factory) == before

        # Seeded again, as a review catalogue is: nothing is added twice.
        _seed_script(root, "--local-ai-only", "--compare")
        assert "nothing was added" in capsys.readouterr().out.splitlines()[-1]
        assert await _counts(factory) == before

        # A download an earlier run left unfinished (here the running API's lane
        # took it and could not find the page) is finished, not skipped.
        async with factory() as db:
            model = await db.scalar(select(LocalModel).where(LocalModel.repo_id == night))
            assert model is not None
            model.state, model.error = "incomplete", "Not found on Hugging Face."
            await db.commit()
        _seed_script(root, "--local-ai-only", "--compare")
        assert capsys.readouterr().out.splitlines()[-1] == f"Compare: added {night}."
        async with factory() as db:
            model = await db.scalar(select(LocalModel).where(LocalModel.repo_id == night))
            assert model is not None and model.state == "installed"
        again = await _counts(factory)
        assert again == {**before, "local_ai_jobs": before["local_ai_jobs"] + 1}

        # One the catalogue's own rules now refuse is said apart, and not added.
        async with factory() as db:
            await db.execute(delete(LocalAiJob).where(LocalAiJob.model_id == model.id))
            await db.execute(delete(LocalModel).where(LocalModel.id == model.id))
            config = await load_config(db)
            config.blocked_added = ["google"]
            await save_config(db, config)
            await db.commit()
        _seed_script(root, "--local-ai-only", "--compare")
        lines = [line for line in capsys.readouterr().out.splitlines() if "Compare:" in line]
        assert lines == [lines[-1]] and lines[-1].startswith(
            f"Compare: not added: {night} (the check refused it: "
        )
        async with factory() as db:
            assert await db.scalar(select(LocalModel.id).where(LocalModel.repo_id == night)) is None
    finally:
        await engine.dispose()


async def _one(client: AsyncClient, comparison_id: str) -> dict[str, Any]:
    answer = await client.get(f"{URL}/{comparison_id}")
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


async def _schema(url: str) -> None:
    """An empty catalogue's tables at `url`, made from the models as every test makes them."""
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest.mark.parametrize(
    ("name", "fits"), [("demo-small", "day"), ("demo-medium", "night")], ids=["by-day", "overnight"]
)
def test_each_compare_demo_model_passes_the_real_check_and_names_no_blocked_publisher(
    name: str, fits: str
) -> None:
    from framefound.local_models import policy

    pages = [fake.fixture(f"compare/{name}-4bit"), fake.fixture(f"compare/{name}")]
    hub = fake.FakeHub({page["id"]: page for page in pages})
    found = preflight.preflight(
        hub,
        f"mlx-community/{name}-4bit",
        config=LocalModelsConfig(),
        kinds={"mlx-vlm": MlxVlm()},
        installed={"mlx-vlm": VERSION},
        free_bytes=None,
        min_free_bytes=0,
    )
    # Allowed by the owner's rules as they stand, through a chain read to its end:
    # a converter's build of a maker's model, reading pictures; demo-small fits
    # by day, demo-medium only overnight (so "waits until 21:30" can be seen).
    assert found.ok and found.complete
    assert [node.publisher for node in found.chain] == ["mlx-community", "google"]
    assert "image" in found.takes and found.fits == fits
    # The policy's own list is the only place a blocked publisher is named.
    blocked = [word.casefold() for word in policy.DEFAULT_BLOCKED]
    for page in (f"{name}-4bit", name):
        text = (fake.DEMO_DATA / "compare" / f"{page}.json").read_text(encoding="utf-8")
        assert not [word for word in blocked if word in text.casefold()]
    # Kept out of the list L1's tests walk, which pins every model it holds.
    assert not any(name.startswith("compare/") for name in fake.fixture_names())
