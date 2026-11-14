"""The shot search, the shot lines and cut history, outside the connector
(framefound/nle/shotlist.py).

They moved out of framefound/mcp so a page or a job can show a local model
the same lines Claude reads and record its cut the same way, on an install
without the optional `mcp` package. What the connector prints is pinned in
test_mcp_shots.py and test_mcp_looks.py, which did not change; that the
function and the tool agree line for line is test_shotlist_connector.py.
Nothing here needs the connector.
"""

import argparse
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Cut, CutPiece, Library, Look, ShotWindow
from framefound.nle import shotlist

BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
SERVER = Path(__file__).resolve().parents[1]

# Every module that must load where the connector's package is missing.
# Later steps add theirs (the draft job, the cuts API).
NEUTRAL = (
    "framefound.nle.shotlist",
    "framefound.nle.roughcut",
    "framefound.ops.shots",
    "framefound.ai.cut_editor",
    "framefound.nle.drafts",
    "framefound.nle.draft_chat",
    "framefound.api.v1.cuts",
)


def _smooth(slow: float, normal: float, fast: float) -> dict[str, float]:
    return {"25": slow, "50": slow, "100": normal, "200": fast, "400": fast, "800": fast}


def _hidden_connector(code: str) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter where `import mcp` fails, as it
    does on an install without the connector's extra (the VM gate's)."""
    prelude = "import sys\nsys.modules['mcp'] = None\n"
    # A fixed command line of our own, run with this interpreter.
    return subprocess.run(  # noqa: S603 - no outside input in the arguments
        [sys.executable, "-c", prelude + code],
        cwd=SERVER,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """A catalogue with no app and no connector: a library of drone clips in
    folders whose names share a beginning, each with shot windows."""
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'shotlist.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "shotlist-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    clips = [
        # path, captured (minutes after BASE), windows (start, end, move, smooth@100)
        ("Courses/2026/Pete/DJI_20261103051509_0639_D.MP4", 30, [(2000, 14000, "orbit", 0.95)]),
        ("Courses/2026/Pete/Drone/DJI_0700.MP4", 10, [(0, 6000, "push-in", 0.80)]),
        ("Courses/2026/Pete Dye/DJI_0800.MP4", 20, [(1000, 9000, "orbit", 0.70)]),
        ("Courses/2026/Brookside/DJI_0900.MP4", 0, [(0, 4000, "static", 0.99)]),
    ]
    ids: dict[str, uuid.UUID] = {}
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path / "gelco"))
        db.add(library)
        await db.flush()
        for path, minutes, windows in clips:
            name = path.rsplit("/", 1)[1]
            asset = Asset(
                library_id=library.id,
                relative_path=path,
                filename=name,
                extension="mp4",
                media_type="video",
                size_bytes=10_000,
                mtime=BASE,
                fps=59.94,
                duration_s=40.0,
                availability="online",
                captured_at=BASE.replace(minute=15 + minutes),
            )
            db.add(asset)
            await db.flush()
            ids[name] = asset.id
            for idx, (start, end, move, smooth) in enumerate(windows):
                db.add(
                    ShotWindow(
                        asset_id=asset.id,
                        idx=idx,
                        start_ms=start,
                        end_ms=end,
                        move=move,
                        direction="right" if move == "orbit" else None,
                        speed=0.08,
                        smooth=smooth,
                        smooth_by_speed=_smooth(smooth - 0.03, smooth, smooth),
                        energy="low",
                        energy_score=0.1,
                        source_fps=59.94,
                        features={},
                        light="golden hour",
                        look={"sky": "direct"},
                        framing={"view": "aerial"},
                        subjects={"green": 2.1, "bunker": 1.6},
                    )
                )
        await db.commit()

    yield {"factory": factory, "ids": ids, "library_id": library.id, "db_url": db_url}
    await engine.dispose()
    get_settings.cache_clear()


def _names(shots: list[shotlist.Shot]) -> list[str]:
    return [asset.filename for _, asset, _ in shots]


# --- where it may be used ------------------------------------------------------


def test_the_shot_list_and_the_cut_writer_load_without_the_connector_installed() -> None:
    """The reason they moved. Asked of a fresh interpreter in which `mcp`
    cannot be imported at all, so a stray import anywhere below these
    modules fails the test instead of quietly loading the connector."""
    done = _hidden_connector(
        "try:\n"
        "    import mcp\n"
        "except ImportError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('the connector was not hidden')\n"
        f"for name in {NEUTRAL!r}:\n"
        "    __import__(name)\n"
        "held = sorted(m for m in sys.modules if m.startswith('framefound.mcp'))\n"
        "print(','.join(held))\n"
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""


def test_the_shots_command_still_parses_without_the_connector() -> None:
    """`python -m framefound.ops.shots --help`, run where `mcp` is missing."""
    done = _hidden_connector(
        "import runpy\n"
        "sys.argv = ['framefound.ops.shots', 'import-cut', '--help']\n"
        "runpy.run_module('framefound.ops.shots', run_name='__main__')\n"
    )

    assert done.returncode == 0, done.stderr
    assert "--library" in done.stdout and "xml" in done.stdout


# --- which shots ---------------------------------------------------------------


async def test_a_folder_prefix_does_not_match_a_sibling_with_a_longer_name(
    env: dict[str, Any],
) -> None:
    """ "2026/Pete" is that folder and the folders inside it; "2026/Pete
    Dye" is another course. find_shots' own `folder` (letters anywhere in
    the path) is left meaning what it always meant."""
    async with env["factory"]() as db:
        under = await shotlist.select_shots(
            db, shotlist.ShotFilter(folder_prefix="Courses/2026/Pete")
        )
        anywhere = await shotlist.select_shots(db, shotlist.ShotFilter(folder="pete"))
        with pytest.raises(shotlist.ShotError, match=r"\.\."):
            await shotlist.select_shots(db, shotlist.ShotFilter(folder_prefix="Courses/../Pete"))

    assert sorted(_names(under)) == ["DJI_0700.MP4", "DJI_20261103051509_0639_D.MP4"]
    assert "DJI_0800.MP4" in _names(anywhere) and len(anywhere) == 3


async def test_the_shots_are_in_the_order_asked_for_and_only_those_clips(
    env: dict[str, Any],
) -> None:
    ids = env["ids"]
    async with env["factory"]() as db:
        smooth = await shotlist.select_shots(db, shotlist.ShotFilter())
        by_time = await shotlist.select_shots(db, shotlist.ShotFilter(order="time"))
        two = await shotlist.select_shots(
            db, shotlist.ShotFilter(asset_ids=[ids["DJI_0800.MP4"], ids["DJI_0900.MP4"]])
        )
        orbits = await shotlist.select_shots(db, shotlist.ShotFilter(moves=["orbit"]))

    assert _names(smooth) == [
        "DJI_0900.MP4",
        "DJI_20261103051509_0639_D.MP4",
        "DJI_0700.MP4",
        "DJI_0800.MP4",
    ]
    assert _names(by_time) == [
        "DJI_0900.MP4",
        "DJI_0700.MP4",
        "DJI_0800.MP4",
        "DJI_20261103051509_0639_D.MP4",
    ]
    assert sorted(_names(two)) == ["DJI_0800.MP4", "DJI_0900.MP4"]
    assert sorted(_names(orbits)) == ["DJI_0800.MP4", "DJI_20261103051509_0639_D.MP4"]


async def test_the_scan_limit_is_the_callers_to_lower(env: dict[str, Any]) -> None:
    """The connector passes its own SCAN_LIMIT, which a test lowers to prove
    the score filter runs before the cut; the function must honour it."""
    async with env["factory"]() as db:
        cut_short = await shotlist.scan_shots(db, shotlist.ShotFilter(), scan_limit=1)
        whole = await shotlist.scan_shots(db, shotlist.ShotFilter())

    assert (cut_short.scanned, len(cut_short.shots)) == (1, 1)
    assert (whole.scanned, len(whole.shots)) == (4, 4)


async def test_a_filter_that_cannot_be_read_names_the_choices(env: dict[str, Any]) -> None:
    with pytest.raises(shotlist.ShotError, match="orbit"):
        shotlist.normalise_move("barrel roll")
    with pytest.raises(shotlist.ShotError, match="golden hour"):
        shotlist.normalise_light("moonlight")
    with pytest.raises(shotlist.ShotError, match="400"):
        shotlist.speed_of(33)
    with pytest.raises(shotlist.ShotError, match="'yesterday' is not an ISO date"):
        shotlist.parse_when("yesterday")
    async with env["factory"]() as db:
        with pytest.raises(shotlist.ShotError, match="0 to 100"):
            await shotlist.scan_shots(db, shotlist.ShotFilter(min_score=101))
        with pytest.raises(shotlist.ShotError, match="at_speed"):
            await shotlist.scan_shots(db, shotlist.ShotFilter(at_speed=33))

    assert shotlist.normalise_move("orbits") == "orbit"
    assert shotlist.normalise_move("dolly in") == "push-in"
    assert shotlist.normalise_light("cloudy") == ("sky", "overcast")
    assert shotlist.normalise_light("sunset") == ("light", "golden hour")
    assert shotlist.parse_when("2026-11-03") == datetime(2026, 11, 3, tzinfo=UTC)


async def test_a_capture_bound_with_no_zone_reads_as_utc(env: dict[str, Any]) -> None:
    """A neutral caller may hand a naive datetime; it means what the
    connector's ISO text without a zone means."""
    async with env["factory"]() as db:
        later = await shotlist.select_shots(
            db, shotlist.ShotFilter(captured_after=datetime(2026, 11, 3, 5, 40))
        )

    assert _names(later) == ["DJI_20261103051509_0639_D.MP4"]


async def test_min_score_keeps_only_shots_a_model_scored_that_high(env: dict[str, Any]) -> None:
    ids = env["ids"]
    async with env["factory"]() as db:
        db.add(
            Look(
                asset_id=ids["DJI_0800.MP4"],
                start_ms=1000,
                end_ms=9000,
                score=82,
                key="google/gemma-4-E4B-it@abc123#look-1",
                model="google/gemma-4-E4B-it",
            )
        )
        await db.commit()
        kept = await shotlist.scan_shots(db, shotlist.ShotFilter(min_score=80))
        ranked = await shotlist.select_shots(db, shotlist.ShotFilter(order="score"))
        none = await shotlist.scan_shots(db, shotlist.ShotFilter(min_score=90))

    assert _names(kept.shots) == ["DJI_0800.MP4"]
    window, asset, _ = kept.shots[0]
    assert shotlist.score_word(kept.look_of(window, asset)) == " score 82"
    assert _names(ranked)[0] == "DJI_0800.MP4"
    # Nothing scanned, and why: windows are there and one is scored.
    assert (none.scanned, none.any_window, none.any_scored) == (0, True, True)


# --- how a shot reads ----------------------------------------------------------


async def test_a_shot_line_is_the_clip_then_the_window_as_find_shots_prints_it(
    env: dict[str, Any],
) -> None:
    orbit = env["ids"]["DJI_20261103051509_0639_D.MP4"]
    async with env["factory"]() as db:
        [(window, asset, used)] = await shotlist.select_shots(
            db, shotlist.ShotFilter(asset_ids=[orbit])
        )

    assert used == []
    assert shotlist.shot_line(asset, window) == (
        f"{shotlist.short_id(orbit)} DJI_0639 "
        "2.0-14.0s orbit right smooth .95 energy low golden green,bunker"
    )
    assert shotlist.shot_line(asset, window, 50, used=True).endswith(
        "smooth@50% .92 energy low golden green,bunker ->24.0s used"
    )
    assert shotlist.clip_name("Rev 2 of the clubhouse walk-through.mov") == (
        "Rev_2_of_the_clubhouse_…"
    )
    assert shotlist.score(0.9) == ".90" and shotlist.score(None) == ""


# --- cut history ---------------------------------------------------------------


async def test_a_recorded_cut_marks_its_shots_used_and_keeps_what_each_was(
    env: dict[str, Any],
) -> None:
    ids = env["ids"]
    async with env["factory"]() as db:
        orbit = await db.get(Asset, ids["DJI_20261103051509_0639_D.MP4"])
        push = await db.get(Asset, ids["DJI_0700.MP4"])
        assert orbit is not None and push is not None
        cut = await shotlist.record_cut(
            db,
            "Pete Quick 30",
            [(orbit, 3.0, 9.0, 1.0), (push, 1.0, 4.0, 2.0)],
            client=None,
            profile=None,
            created_by=None,
        )
        pieces = (
            (
                await db.execute(
                    select(CutPiece).where(CutPiece.cut_id == cut.id).order_by(CutPiece.position)
                )
            )
            .scalars()
            .all()
        )
        flagged = await shotlist.select_shots(
            db, shotlist.ShotFilter(folder_prefix="Courses/2026/Pete")
        )
        unused = await shotlist.select_shots(
            db, shotlist.ShotFilter(folder_prefix="Courses/2026", exclude_used=True)
        )

    assert (cut.source, cut.client, cut.piece_count, cut.duration_s) == (
        "mcp",
        "GELCO",
        2,
        7.5,
    )
    assert [(p.in_ms, p.out_ms, p.shot["move"]) for p in pieces] == [
        (3000, 9000, "orbit"),
        (1000, 4000, "push-in"),
    ]
    assert {asset.filename: used for _, asset, used in flagged} == {
        "DJI_20261103051509_0639_D.MP4": ["Pete Quick 30"],
        "DJI_0700.MP4": ["Pete Quick 30"],
    }
    assert sorted(_names(unused)) == ["DJI_0800.MP4", "DJI_0900.MP4"]


async def test_a_past_timeline_is_imported_into_history(
    env: dict[str, Any], tmp_path: Path
) -> None:
    """`ops/shots.py import-cut` writes through the same `record_cut`, now
    reached without the connector."""
    from framefound.ops import shots as ops_shots

    timeline = tmp_path / "Rev 2.xml"
    timeline.write_text(
        "<xmeml version='4'><sequence><name>Pete Rev 2</name><media><video><track>"
        "<clipitem><name>DJI_0700.MP4</name><start>0</start><in>60</in><out>240</out>"
        "<rate><timebase>60</timebase><ntsc>FALSE</ntsc></rate>"
        "<file id='f1'><name>DJI_0700.MP4</name>"
        "<pathurl>file://localhost/Volumes/GELCO/Courses/2026/Pete/Drone/DJI_0700.MP4</pathurl>"
        "</file></clipitem>"
        "<clipitem><name>Nowhere.MP4</name><start>180</start><in>0</in><out>60</out>"
        "<file id='f2'><name>Nowhere.MP4</name></file></clipitem>"
        "</track></video></media></sequence></xmeml>",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        xml=str(timeline), library="gelco", title=None, client="GELCO", date="2026-11-01"
    )

    assert await ops_shots.import_cut(args) == 0

    async with env["factory"]() as db:
        cut: Cut = (await db.execute(select(Cut))).scalar_one()
        piece: CutPiece = (await db.execute(select(CutPiece))).scalar_one()
    assert (cut.title, cut.source, cut.piece_count) == ("Pete Rev 2", "import", 1)
    assert cut.created_at.replace(tzinfo=UTC) == datetime(2026, 11, 1, tzinfo=UTC)
    assert (piece.in_ms, piece.out_ms, piece.shot["move"]) == (1000, 4000, "push-in")
    # An imported timeline is a first version, with no reasons and no model.
    assert (cut.version, cut.root_id, cut.parent_id, cut.model) == (1, None, None, None)
    assert piece.note == ""


async def test_each_revision_is_the_next_version_of_the_same_family(
    env: dict[str, Any],
) -> None:
    """Nothing is overwritten: a cut that revises another is written beside
    it, numbered one above the highest in the family, whichever version it
    revises."""
    ids = env["ids"]
    async with env["factory"]() as db:
        orbit = await db.get(Asset, ids["DJI_20261103051509_0639_D.MP4"])
        assert orbit is not None

        async def write(title: str, parent: Cut | None = None) -> Cut:
            assert orbit is not None
            return await shotlist.record_cut(
                db,
                title,
                [(orbit, 3.0, 9.0, 1.0)],
                client=None,
                profile=None,
                created_by=None,
                source="local" if parent is None else "mcp",
                parent=parent,
            )

        first = await write("First try")
        second = await write("Reviewed", parent=first)
        # Another revision of version 1 after version 2 exists: version 3.
        third = await write("Second opinion", parent=first)
        fourth = await write("Reviewed again", parent=second)
        unrelated = await write("Another job")
        rows = {
            cut.title: (cut.root_id, cut.parent_id, cut.version)
            for cut in (await db.execute(select(Cut))).scalars()
        }

    assert rows == {
        "First try": (None, None, 1),
        "Reviewed": (first.id, first.id, 2),
        "Second opinion": (first.id, first.id, 3),
        "Reviewed again": (first.id, second.id, 4),
        "Another job": (None, None, 1),
    }
    assert (third.version, fourth.version, unrelated.version) == (3, 4, 1)


async def test_a_cut_keeps_its_reasons_and_which_build_drafted_it(env: dict[str, Any]) -> None:
    ids = env["ids"]
    async with env["factory"]() as db:
        orbit = await db.get(Asset, ids["DJI_20261103051509_0639_D.MP4"])
        push = await db.get(Asset, ids["DJI_0700.MP4"])
        assert orbit is not None and push is not None
        pieces = [(orbit, 3.0, 9.0, 1.0), (push, 1.0, 4.0, 1.0)]
        cut = await shotlist.record_cut(
            db,
            "Pete, first try",
            pieces,
            client=None,
            profile=None,
            created_by=None,
            source="local",
            notes=["Opens on the green", "x" * 900],
            provenance={
                "model": "mlx-community/gemma-4-12B-it-OptiQ-4bit",
                "model_revision": "a" * 40,
                "runtime": "mlx-vlm",
                "runtime_version": "0.4.0",
                "prompt_version": "editor-1",
            },
        )
        notes = (
            (
                await db.execute(
                    select(CutPiece.note)
                    .where(CutPiece.cut_id == cut.id)
                    .order_by(CutPiece.position)
                )
            )
            .scalars()
            .all()
        )

        # A note per piece or none; a name that is not provenance is not
        # quietly dropped. Neither writes anything.
        with pytest.raises(ValueError, match="1 notes for 2 pieces"):
            await shotlist.record_cut(
                db, "x", pieces, client=None, profile=None, created_by=None, notes=["one"]
            )
        with pytest.raises(ValueError, match="not provenance: modle"):
            await shotlist.record_cut(
                db,
                "x",
                pieces,
                client=None,
                profile=None,
                created_by=None,
                provenance={"modle": "typo"},
            )
        count = len((await db.execute(select(Cut.id))).all())

    assert notes == ["Opens on the green", "x" * 500]
    assert (cut.model, cut.model_revision, cut.variant) == (
        "mlx-community/gemma-4-12B-it-OptiQ-4bit",
        "a" * 40,
        "",
    )
    assert (cut.runtime, cut.runtime_version, cut.prompt_version) == (
        "mlx-vlm",
        "0.4.0",
        "editor-1",
    )
    assert (cut.version, cut.sent_at) == (1, None)
    assert count == 1
