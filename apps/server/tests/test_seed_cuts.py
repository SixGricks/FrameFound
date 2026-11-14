"""The seed's cuts part (`infrastructure/scripts/seed_dev.py` `seed_cuts`, L5 design 8.9).

A reviewer opening the seeded catalogue's Cuts page sees every state without
asking for anything first: a rough cut in two versions (a local draft with
its reasons and what the check changed, and a revision kept beside it), an
imported timeline, and a draft that failed with what the Editor wrote. These
tests run the script as a reviewer does and read the result through the real
API, then seed again as a review catalogue is seeded and check that nothing
is added twice and nothing a reviewer deleted comes back.

They also hold the seed to two promises it makes to its neighbours: it makes
no job (the AI models page's list is the demo seed's own, which
`test_local_models_demo.py` pins), and the shots finished work has used are
the five the imported timeline used (Compare's seeded comparison counts them).

Starting a program or reaching a real address fails the test.
"""

import runpy
import subprocess
import sys
import threading
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

from framefound.ai import cut_editor
from framefound.api.v1 import local_models as models_api
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    AppSetting,
    Cut,
    CutDraft,
    CutPiece,
    Library,
    LocalAiJob,
    ShotWindow,
)
from framefound.local_models import ask, demo
from framefound.logging import configure_logging
from framefound.nle import drafts, roughcut, shotlist
from framefound.storage import stores

SEED = Path(__file__).resolve().parents[3] / "infrastructure" / "scripts" / "seed_dev.py"
CUTS = "/api/v1/cuts"
ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
TEASER = "Pete Dye — 30 second teaser"
QUICK_30 = "Pete Dye Quick 30"
NOTHING_ADDED = "Cuts: everything it needs is already in this catalogue; nothing was added."
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
        models_api._STATUS.clear()
        models_api._LOOKING.clear()

    clear()
    assert demo.stop(), "a demo lane left by an earlier test did not stop"
    yield
    # Reading the Cuts page in demo mode starts the demo's lane; it must not outlive the test.
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


async def _empty_catalogue(tmp_path: Path) -> Path:
    root = tmp_path / "ff-ui"
    root.mkdir()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return root


@asynccontextmanager
async def _review(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    """The real app on the seeded catalogue, signed in as its first administrator."""
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "seed-cuts-secret")
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
            # The page's reads started the demo's lane; it ends before its database does.
            assert demo.stop()
    finally:
        await engine.dispose()


async def _get(client: AsyncClient, url: str) -> Any:
    answer = await client.get(url)
    assert answer.status_code == 200, answer.text
    return answer.json()


async def _rows(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """How many of each thing the cuts part writes, and of the jobs it must not."""
    tables = (ShotWindow, Cut, CutPiece, CutDraft, LocalAiJob)
    async with factory() as db:
        return {
            table.__tablename__: int(await db.scalar(select(func.count()).select_from(table)) or 0)
            for table in tables
        }


async def _used(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, int]]:
    """The GELCO shots that count as used by finished work, as (clip, start)."""
    async with factory() as db:
        gelco = await db.scalar(select(Library.id).where(Library.name == "GELCO"))
        shots = await shotlist.select_shots(db, shotlist.ShotFilter(library_id=gelco))
    return sorted((asset.relative_path, window.start_ms) for window, asset, used in shots if used)


def _spans(version: dict[str, Any]) -> list[tuple[str, float, float]]:
    """A version's pieces as (clip, in, out), in timeline order."""
    return [(piece["asset_id"], piece["in_s"], piece["out_s"]) for piece in version["pieces"]]


async def test_a_seeded_catalogue_shows_every_state_of_the_cuts_page_and_a_reseed_undoes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = await _empty_catalogue(tmp_path)

    # The plain seed, with no flag: the cuts are not asked for separately.
    _run_seed(root, monkeypatch)
    said = capsys.readouterr().out.splitlines()
    [cuts_line] = [line for line in said if line.startswith("Cuts:")]
    assert cuts_line == (
        "Cuts: added 15 shot windows with their stills; the imported timeline Pete Dye Quick 30; "
        "the Pete Dye teaser in two versions (a local draft of 6 shots, and a revision of it); "
        "a draft that failed."
    )
    # Nothing of the builder's own logging among the seed's lines.
    assert not [line for line in said if "mcp.rough_cut" in line]

    async with _review(monkeypatch) as (client, factory):
        page = await _get(client, CUTS)
        # The Editor is the demo seed's, and a draft can be asked for.
        assert page["editor"]["mode"] == "demo" and page["can_draft"] is True
        assert page["editor"]["model_name"] == demo.EDITOR_MODEL.rsplit("/", 1)[-1]

        # The draft that failed: its sentence, what the Editor wrote, and a way to clear it.
        [failed] = page["drafts"]
        assert (failed["state"], failed["message"]) == ("failed", drafts.NOT_A_FORM)
        assert failed["answer_text"].startswith("Here is how I would cut a calm minute")
        assert failed["scope"]["label"] == "GELCO › LedgeRock" and failed["shots_offered"] == 6
        assert failed["demo"] is True and failed["can_clear"] is True
        assert failed["model_name"] == page["editor"]["model_name"]

        # One line per family: the teaser with its two versions, the timeline alone.
        families = {family["title"]: family for family in page["cuts"]}
        assert set(families) == {TEASER, QUICK_30}
        teaser, quick = families[TEASER], families[QUICK_30]
        assert (teaser["versions"], teaser["has_local_draft"], teaser["sent"]) == (2, True, False)
        assert (teaser["newest"]["version"], teaser["newest"]["label"]) == (2, "Claude")
        assert (quick["versions"], quick["newest"]["label"]) == (1, "Imported timeline")
        assert (quick["newest"]["piece_count"], quick["has_local_draft"]) == (5, False)

        # The teaser: version 1 a local draft by the demo's Editor, marked made up,
        # with a reason on every shot and the two things the check changed.
        cut = await _get(client, f"{CUTS}/{teaser['id']}")
        first, second = cut["versions"]
        assert (first["version"], first["source"], first["demo"]) == (1, "local", True)
        assert first["provenance"]["model"] == demo.EDITOR_MODEL
        assert first["provenance"]["prompt_version"] == cut_editor.PROMPT_VERSION
        assert first["sent_at"] is None and first["can_delete"] is True
        assert len(first["pieces"]) == 6 and all(piece["note"] for piece in first["pieces"])
        assert 27.0 <= first["duration_s"] <= 33.0
        made_from = first["draft"]
        assert made_from["brief"].startswith("Thirty seconds of smooth aerials")
        assert made_from["scope"]["folder"] == "Pete Dye"
        named, ran_past = made_from["problems"]
        assert named == "Cut 4 named shot 9, which is not on the list."
        assert ran_past.startswith("Cut 6 ran past the end of its shot and was pulled back to ")
        # What it was chosen from: the folder's six shots, a piece taken from each.
        assert (made_from["shots_offered"], made_from["shots_in_scope"]) == (6, 6)
        assert all(shot["chosen"] for shot in made_from["candidates"])

        # Version 2 revises it and is kept beside it: the opening and the close
        # as they were, one shot moved up, one trimmed, one added, one left
        # out. So comparing the two has a shot of every kind to show.
        assert (second["version"], second["source"], second["demo"]) == (2, "mcp", False)
        assert second["parent_id"] == first["id"] and second["draft"] is None
        older, newer = _spans(first), _spans(second)
        assert len(newer) == 6 and newer[0] == older[0] and newer[-1] == older[-1]
        assert len(set(older) & set(newer)) == 4  # the same stretch of the same clip
        [trimmed] = [span for span in newer if span[:2] == older[1][:2]]
        assert trimmed[2] == pytest.approx(older[1][2] - 1.5)
        assert newer.index(older[2]) < newer.index(trimmed)  # was after it, now before
        left_out = older[4]
        assert not [span for span in newer if span[0] == left_out[0] and span[1] >= left_out[1]]
        # Every shot of both versions has its picture, and it opens.
        for version in (first, second):
            for piece in version["pieces"]:
                picture = await client.get(piece["still_url"])
                assert picture.status_code == 200
                assert picture.headers["content-type"] == "image/jpeg"

        # What Claude would be asked about the draft says what the draft is.
        review = await _get(client, f"{CUTS}/{first['id']}/review-request")
        assert "a demonstration that judged nothing" in review["text"]
        assert f'parent="{first["id"]}"' in review["text"]

        # The form's folder picker offers the seeded folders, every clip analysed.
        folders = {folder["path"]: folder for folder in await _get(client, f"{CUTS}/folders")}
        assert set(folders) == {"LedgeRock", "Pete Dye", "Hudson National"}
        assert all(folder["clips"] == folder["analysed"] for folder in folders.values())

        # Promises to the neighbours. No job was made: every job is the demo
        # seed's own. And the shots that count as used are the imported
        # timeline's five, though the revision is finished work too.
        async with factory() as db:
            kinds = set((await db.scalars(select(LocalAiJob.kind))).all())
        assert kinds == {"runtime_install", "download", "test"}
        used = await _used(factory)
        assert len(used) == 5 and all(path.startswith("Pete Dye/") for path, _start in used)
        before = await _rows(factory)
        assert before["cuts"] == 3 and before["cut_drafts"] == 2

    # Seeded again, as the review catalogue is: nothing added.
    _run_seed(root, monkeypatch, "--local-ai-only")
    assert NOTHING_ADDED in capsys.readouterr().out.splitlines()
    async with _review(monkeypatch) as (client, factory):
        assert await _rows(factory) == before

        # What a reviewer does: delete the revision, clear the failed draft,
        # delete the imported timeline.
        for url in (
            f"{CUTS}/{second['id']}",
            f"{CUTS}/drafts/{failed['id']}",
            f"{CUTS}/{quick['id']}",
        ):
            gone = await client.delete(url)
            assert gone.status_code == 204, gone.text
        after = await _rows(factory)
        assert (after["cuts"], after["cut_drafts"]) == (1, 1)

    # None of the three comes back: not from the plain seed, and the timeline
    # not from Compare's either, which asks for the same one.
    _run_seed(root, monkeypatch, "--local-ai-only")
    assert NOTHING_ADDED in capsys.readouterr().out.splitlines()
    _run_seed(root, monkeypatch, "--local-ai-only", "--compare")
    said = capsys.readouterr().out.splitlines()
    assert NOTHING_ADDED in said
    [compare] = [line for line in said if line.startswith("Compare: added ")]
    assert "the cut " not in compare and "shot windows" not in compare
    async with _review(monkeypatch) as (client, factory):
        now = await _rows(factory)
        assert (now["cuts"], now["cut_drafts"], now["shot_windows"]) == (1, 1, 15)
        page = await _get(client, CUTS)
        assert page["drafts"] == [] and [family["title"] for family in page["cuts"]] == [TEASER]
        assert page["cuts"][0]["versions"] == 1


async def test_a_catalogue_seeded_before_the_cuts_part_gets_it_without_a_second_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The review catalogue was seeded with `--compare` before this part
    existed: it has the shot windows and the imported timeline (renamed, even)
    and no note of either."""
    root = await _empty_catalogue(tmp_path)
    _run_seed(root, monkeypatch)
    capsys.readouterr()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            family = (await db.scalars(select(Cut.id).where(Cut.source != "import"))).all()
            await db.execute(delete(CutPiece).where(CutPiece.cut_id.in_(family)))
            await db.execute(delete(Cut).where(Cut.id.in_(family)))
            await db.execute(delete(CutDraft))
            await db.execute(delete(AppSetting).where(AppSetting.key == demo.SEEDED_KEY))
            timeline = await db.scalar(select(Cut).where(Cut.source == "import"))
            assert timeline is not None
            timeline.title = "Quick 30, as delivered"
            await db.commit()

        _run_seed(root, monkeypatch, "--local-ai-only")
        [cuts_line] = [
            line for line in capsys.readouterr().out.splitlines() if line.startswith("Cuts:")
        ]
        # The windows and the timeline were there: only the cut and the draft are new.
        assert cuts_line == (
            "Cuts: added the Pete Dye teaser in two versions (a local draft of 6 shots, and a "
            "revision of it); a draft that failed."
        )
        async with factory() as db:
            sources = sorted((await db.scalars(select(Cut.source))).all())
            windows = await db.scalar(select(func.count()).select_from(ShotWindow))
            noted = await demo.seeded_parts(db)
        assert sources == ["import", "local", "mcp"] and windows == 15
        assert {"cuts_family", "cuts_import", "cuts_failed"} <= noted

        _run_seed(root, monkeypatch, "--local-ai-only")
        assert NOTHING_ADDED in capsys.readouterr().out.splitlines()
    finally:
        await engine.dispose()


async def test_a_catalogue_without_the_seeded_footage_is_told_what_was_not_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = await _empty_catalogue(tmp_path)

    # `--local-ai-only` on an empty catalogue: no GELCO library, so no footage.
    _run_seed(root, monkeypatch, "--local-ai-only")
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Cuts:")]
    assert lines == ["Cuts: not added: the cuts (this catalogue has no GELCO library)."]

    # A GELCO library whose clips have gone from the share: nothing to draft
    # from, each part says so, and nothing is noted, so it is tried again.
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            db.add(Library(name="GELCO", root_path=str(root / "media" / "gelco")))
            await db.commit()
        _run_seed(root, monkeypatch, "--local-ai-only")
        [line] = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Cuts:")]
        assert line.startswith("Cuts: not added: the imported timeline (")
        assert "the Pete Dye teaser (nothing to draft it from: " in line
        assert "the draft that failed (nothing to ask about: " in line
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(Cut)) == 0
            assert await db.scalar(select(func.count()).select_from(CutDraft)) == 0
            assert not {"cuts_family", "cuts_import", "cuts_failed"} & await demo.seeded_parts(db)
    finally:
        await engine.dispose()


async def test_the_cut_writers_log_lines_stay_out_of_the_seeds_own_in_any_order_of_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cut writer logs each cut it builds, and the seed holds those lines
    back. It once did so only when the writer's logger had not been used
    before the API was last built: a logger keeps the logging set-up it was
    first used under, the API sets logging up every time it is built, and
    the seed silenced only the newest. So this passed alone and failed after
    any test file that had drafted a cut and then built the app."""
    # What an earlier test file leaves behind: the writer's logger used once,
    # then logging set up again, as building the app does.
    configure_logging()
    roughcut.log.info("an earlier test built a cut")
    configure_logging()
    capsys.readouterr()

    root = await _empty_catalogue(tmp_path)
    _run_seed(root, monkeypatch)
    said = capsys.readouterr().out.splitlines()
    [cuts_line] = [line for line in said if line.startswith("Cuts:")]
    assert "the Pete Dye teaser in two versions" in cuts_line
    assert not [line for line in said if "mcp.rough_cut" in line]
    # Its logger is its own again afterwards: the app's cuts are still logged.
    roughcut.log.info("after the seed")
    assert "after the seed" in capsys.readouterr().out


async def test_a_teaser_that_stops_part_way_leaves_no_half_family_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The writer commits each version as it writes it, and the family is
    noted only once both are there. A seed that stopped anywhere between
    would leave a version with no note, and the next seed would add a second
    "version 1" beside it on the review catalogue. Three places it can stop:
    before the revision is written, after the writer has kept the revision,
    and with both written but the note not yet made."""
    root = await _empty_catalogue(tmp_path)
    build, note = roughcut.build_rough_cut, demo.note_seeded
    writers_log = roughcut.log

    async def refused(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("parent"):
            raise RuntimeError("the revision was refused")
        return await build(*args, **kwargs)

    async def kept_then_failed(*args: Any, **kwargs: Any) -> Any:
        built = await build(*args, **kwargs)
        if kwargs.get("parent"):
            raise RuntimeError("the writer failed after keeping the revision")
        return built

    async def no_note(db: AsyncSession, name: str) -> None:
        if name == "cuts_family":
            raise RuntimeError("the note could not be made")
        await note(db, name)

    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for flags, module, name, stand_in, why in (
            ((), roughcut, "build_rough_cut", refused, "the revision was refused"),
            (("--local-ai-only",), roughcut, "build_rough_cut", kept_then_failed, "after keeping"),
            (("--local-ai-only",), demo, "note_seeded", no_note, "the note could not be made"),
        ):
            with monkeypatch.context() as patched:
                patched.setattr(module, name, stand_in)
                with pytest.raises(RuntimeError, match=why):
                    _run_seed(root, monkeypatch, *flags)
            capsys.readouterr()
            # The seed did not keep the writer's logger for itself on the way out.
            assert roughcut.log is writers_log
            # Whatever had been written and kept (a version and its pieces,
            # both versions, the request) is gone again. The timeline, which
            # is its own part and was noted, stays.
            rows = await _rows(factory)
            assert (rows["cuts"], rows["cut_pieces"], rows["cut_drafts"]) == (1, 5, 0), why
            async with factory() as db:
                assert (await db.scalars(select(Cut.source))).all() == ["import"]
                noted = await demo.seeded_parts(db)
            assert "cuts_import" in noted and not {"cuts_family", "cuts_failed"} & noted

        # The next seed adds the family whole, once.
        _run_seed(root, monkeypatch, "--local-ai-only")
        [cuts_line] = [
            line for line in capsys.readouterr().out.splitlines() if line.startswith("Cuts:")
        ]
        assert cuts_line == (
            "Cuts: added the Pete Dye teaser in two versions (a local draft of 6 shots, and a "
            "revision of it); a draft that failed."
        )
        async with factory() as db:
            cuts = (await db.scalars(select(Cut).where(Cut.title == TEASER))).all()
            drafts_now = sorted((await db.scalars(select(CutDraft.state))).all())
        assert sorted((cut.version, cut.source) for cut in cuts) == [(1, "local"), (2, "mcp")]
        [first] = [cut for cut in cuts if cut.version == 1]
        [second] = [cut for cut in cuts if cut.version == 2]
        assert second.parent_id == first.id
        assert drafts_now == ["done", "failed"]
    finally:
        await engine.dispose()


async def test_a_catalogue_whose_tables_are_older_is_told_to_migrate_and_nothing_is_noted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CONTRIBUTING.md promises that the seed says so when the review
    catalogue is behind the code. Here it is one migration behind (before
    0036): no table of drafts, a cut with no `sent_at`, a piece with no
    note. The reviewer is told what to run, not shown a traceback, and the
    cuts are added once the tables are brought up to date."""
    root = await _empty_catalogue(tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("DROP TABLE cut_drafts")
            await conn.exec_driver_sql("ALTER TABLE cuts DROP COLUMN sent_at")
            await conn.exec_driver_sql("ALTER TABLE cut_pieces DROP COLUMN note")

        # It ends without raising: `_run_seed` would hand the error on.
        _run_seed(root, monkeypatch)
        said = capsys.readouterr()
        [cuts_line] = [line for line in said.out.splitlines() if line.startswith("Cuts:")]
        assert cuts_line.startswith("Cuts: the catalogue's tables are older than this code (")
        assert "sent_at" in cuts_line
        assert cuts_line.endswith("Run `alembic upgrade head` against ui.db first.")
        assert "Traceback" not in said.out + said.err
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(Cut)) == 0
            assert not {"cuts_family", "cuts_import", "cuts_failed"} & await demo.seeded_parts(db)

        # What `alembic upgrade head` does to it.
        async with engine.begin() as conn:
            await conn.exec_driver_sql("ALTER TABLE cuts ADD COLUMN sent_at DATETIME")
            await conn.exec_driver_sql(
                "ALTER TABLE cut_pieces ADD COLUMN note VARCHAR(500) NOT NULL DEFAULT ''"
            )
            await conn.run_sync(Base.metadata.create_all)
        _run_seed(root, monkeypatch, "--local-ai-only")
        [cuts_line] = [
            line for line in capsys.readouterr().out.splitlines() if line.startswith("Cuts:")
        ]
        assert cuts_line == (
            "Cuts: added 15 shot windows with their stills; the imported timeline "
            "Pete Dye Quick 30; the Pete Dye teaser in two versions (a local draft of 6 shots, "
            "and a revision of it); a draft that failed."
        )
        rows = await _rows(factory)
        assert (rows["cuts"], rows["cut_drafts"]) == (3, 2)
    finally:
        await engine.dispose()
