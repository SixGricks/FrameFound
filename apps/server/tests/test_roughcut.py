"""The cut writer outside the connector (framefound/nle/roughcut.py).

It was the body of the connector's `build_rough_cut` tool and moved so a
local model's draft is written by the same code as Claude's cuts, on an
install without the optional `mcp` package. That the tool still answers
exactly as before (the same XML, the same sentences) is
test_roughcut_connector.py; this file needs no connector at all.
"""

import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Cut, CutPiece, Library, PathMapping
from framefound.nle import roughcut, shotlist
from framefound.nle.roughcut import CutError, CutIn

BASE = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """A library with a 59.94 drone opener, a long 29.97 walk-through, a
    photograph, and one path profile for the Mac."""
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'roughcut.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "roughcut-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    clips = [
        # filename, media type, fps, seconds, audio
        ("DJI_0100.MP4", "video", 59.94, 20.0, None),
        ("Walkthrough.MP4", "video", 29.97, 120.0, "aac"),
        ("front.jpg", "image", None, None, None),
    ]
    ids: dict[str, uuid.UUID] = {}
    async with factory() as db:
        library = Library(name="Intel", root_path="/media/intel")
        db.add(library)
        await db.flush()
        for name, kind, fps, seconds, audio in clips:
            asset = Asset(
                library_id=library.id,
                relative_path=f"Brookside/{name}",
                filename=name,
                extension=name.rsplit(".", 1)[1].lower(),
                media_type=kind,
                fps=fps,
                duration_s=seconds,
                width=3840 if kind == "video" else 6000,
                height=2160 if kind == "video" else 4000,
                audio_codec=audio,
                size_bytes=10_000,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            ids[name] = asset.id
        db.add(
            PathMapping(
                library_id=library.id,
                profile_name="Mac Mini",
                platform="macos",
                mapped_prefix="/Volumes/Intel",
            )
        )
        await db.commit()

    yield {"factory": factory, "ids": ids, "library_id": library.id}
    await engine.dispose()
    get_settings.cache_clear()


def _sequence(xml: str) -> ET.Element:
    root = ET.fromstring(xml.split("\n", 2)[2])  # noqa: S314 - our own output
    sequence = root.find("./sequence")
    assert sequence is not None
    return sequence


def _timebase(xml: str) -> tuple[str, str]:
    rate = _sequence(xml).find("./rate")
    assert rate is not None
    return rate.findtext("timebase") or "", rate.findtext("ntsc") or ""


async def _cuts(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count(Cut.id)))).scalar_one())


async def test_a_cut_is_written_and_kept_in_the_session_it_is_given(
    env: dict[str, Any],
) -> None:
    """One session for the whole thing: the clips are read and the history
    row written in the caller's, and the answer names the row."""
    ids = env["ids"]
    cuts = [
        CutIn(id=str(ids["Walkthrough.MP4"]), in_s=10, out_s=14, note="Front door"),
        # The short id the shot lines print.
        CutIn(id=shotlist.short_id(ids["front.jpg"])),
        CutIn(id=str(ids["DJI_0100.MP4"]), in_s=18),
    ]
    async with env["factory"]() as db:
        cut = await roughcut.build_rough_cut(
            db, "  Brookside 30 ", cuts, profile="Mac Mini", source="local", by="test"
        )
        rows = (await db.execute(select(Cut))).scalars().all()
        pieces = (await db.execute(select(CutPiece).order_by(CutPiece.position))).scalars().all()

    assert cut.recorded and cut.note == "3 cuts, 10.0s. Recorded in cut history."
    # The out point a video leaves open runs to the clip's end, not past it.
    assert cut.duration_s == pytest.approx(4 + 4 + 2)
    assert cut.unmapped == [] and cut.filename == "Brookside 30.xml"
    [row] = rows
    # A cut that revises nothing is the first of a family of its own.
    assert cut.cut_id == str(row.id) and cut.version == 1 and row.root_id is None
    assert row.title == "Brookside 30" and row.source == "local" and row.client == "Intel"
    assert [(p.filename, p.in_ms, p.out_ms) for p in pieces] == [
        ("Walkthrough.MP4", 10_000, 14_000),
        ("front.jpg", 0, 4_000),
        ("DJI_0100.MP4", 18_000, 20_000),
    ]
    urls = [p.text for p in _sequence(cut.xml).iter("pathurl")]
    assert urls[0] == "file://localhost/Volumes/Intel/Brookside/Walkthrough.MP4"


async def test_a_piece_keeps_its_reason(env: dict[str, Any]) -> None:
    """A note used to become a marker in the file and then be lost; it is
    kept on the piece, so the cut can be downloaded again with its markers.
    A note that looks like markup is kept as the text it is."""
    ids = env["ids"]
    reason = 'Opens on the door </name><evil x="1">'
    cuts = [
        CutIn(id=str(ids["Walkthrough.MP4"]), in_s=10, out_s=14, note=reason),
        CutIn(id=str(ids["front.jpg"])),
        CutIn(id=str(ids["DJI_0100.MP4"]), in_s=2, out_s=6, note="Lifts over the roof"),
    ]
    async with env["factory"]() as db:
        cut = await roughcut.build_rough_cut(db, "Brookside 30", cuts)
        notes = (
            (await db.execute(select(CutPiece.note).order_by(CutPiece.position))).scalars().all()
        )

    assert notes == [reason, "", "Lifts over the roof"]
    markers = [
        m.findtext("comment") or m.findtext("name") for m in _sequence(cut.xml).iter("marker")
    ]
    assert reason in markers and "Lifts over the roof" in markers


async def test_a_control_character_never_reaches_the_file_or_history(
    env: dict[str, Any],
) -> None:
    """A NUL or an escape in a note or title (a model's reason can carry one
    as valid JSON) made a file Premiere refuses to import, and a NUL is a
    value PostgreSQL refuses to store. They are left out of both, and a row
    written before that (SQLite took it) downloads as a file that opens."""
    ids = env["ids"]
    cuts = [
        CutIn(id=str(ids["Walkthrough.MP4"]), in_s=10, out_s=14, note="wide open\x00 door\x1b"),
        CutIn(id=str(ids["DJI_0100.MP4"]), in_s=2, out_s=6, note="tab\tkept"),
    ]
    async with env["factory"]() as db:
        cut = await roughcut.build_rough_cut(db, "Brookside\x07 30", cuts)
        notes = (
            (await db.execute(select(CutPiece.note).order_by(CutPiece.position))).scalars().all()
        )
        assert notes == ["wide open door", "tab\tkept"]
        row = await db.get(Cut, uuid.UUID(cut.cut_id or ""))
        assert row is not None and row.title == "Brookside 30"
        # As an older row may hold it.
        first = (
            await db.execute(
                select(CutPiece).where(CutPiece.cut_id == row.id, CutPiece.position == 0)
            )
        ).scalar_one()
        first.note = "kept\x00 before"
        await db.commit()
        again = await roughcut.rebuild(db, row, profile=None)

    assert cut.title == "Brookside 30" and cut.filename == "Brookside 30.xml"
    for xml, said in ((cut.xml, "wide open door"), (again.cut.xml, "kept before")):
        # Parsing is the check that matters: one NUL and nothing parses.
        comments = {m.findtext("comment") for m in _sequence(xml).iter("marker")}
        assert comments == {said, "tab\tkept"}


def test_every_file_the_writer_makes_opens_whatever_text_it_is_given() -> None:
    """The writer's own last step, for the selects reel, the bins and any
    caller that never passes through `build_rough_cut`: a control character
    in a clip's name or a note is left out, and text without one is written
    as it always was."""
    clip = roughcut.fcp7.Clip(
        name="DJI\x00_0100.MP4", path="/media/intel/DJI_0100.MP4", duration_s=20.0, fps=29.97
    )
    xml = roughcut.fcp7.build_sequence(
        "Brookside\x1b 30", [roughcut.fcp7.Cut(clip, 1.0, 3.0, note="opens\x00 wide")]
    )

    sequence = _sequence(xml)
    assert sequence.findtext("name") == "Brookside 30"
    assert {m.findtext("comment") for m in sequence.iter("marker")} == {"opens wide"}
    assert roughcut.fcp7.xml_text("tab\tline\nend\r é ✓") == "tab\tline\nend\r é ✓"


async def test_a_cut_that_must_be_recorded_fails_when_it_cannot_be(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """For a local draft the record is the product, so a failure to write it
    is the caller's to see; for anyone else the timeline still comes back.
    Either way the session is rolled back and still usable."""

    async def broken(db: AsyncSession, *_args: object, **_kwargs: object) -> Cut:
        # The cut row written, then a piece whose flush fails: the
        # transaction needs a rollback, as after a real failure part-way.
        db.add(Cut(title="half"))
        await db.flush()
        db.add(CutPiece(cut_id=None, filename="x"))
        await db.flush()
        raise AssertionError("unreachable")

    monkeypatch.setattr(shotlist, "record_cut", broken)
    cuts = [CutIn(id=str(env["ids"]["Walkthrough.MP4"]), in_s=1, out_s=3)]

    async with env["factory"]() as db:
        with pytest.raises(Exception, match="NOT NULL"):
            await roughcut.build_rough_cut(db, "Draft", cuts, must_record=True)
        assert await _cuts(db) == 0

        kept = await roughcut.build_rough_cut(db, "Draft", cuts)
        assert await _cuts(db) == 0

    assert kept.recorded is False and kept.cut_id is None
    assert kept.note == "1 cuts, 2.0s. 1 file(s) have no path for any profile (none given); " + (
        "they use the server's path and will import offline until relinked."
    )
    assert "<pathurl>file://localhost/media/intel/Brookside/Walkthrough.MP4" in kept.xml


async def test_nothing_is_recorded_when_asked_not_to(env: dict[str, Any]) -> None:
    async with env["factory"]() as db:
        cut = await roughcut.build_rough_cut(
            db,
            "Throwaway",
            [CutIn(id=str(env["ids"]["DJI_0100.MP4"]), in_s=0, out_s=2)],
            record=False,
            must_record=True,
        )
        assert await _cuts(db) == 0
    assert cut.recorded is False and cut.cut_id is None


async def test_a_request_that_cannot_be_a_cut_says_why(env: dict[str, Any]) -> None:
    """The sentences are the connector's, which Claude has always read; a
    refusal comes before anything is written."""
    ids = env["ids"]
    walk = str(ids["Walkthrough.MP4"])
    asks: list[tuple[list[CutIn], str | None, str]] = [
        ([], None, "Give at least one cut"),
        ([CutIn(id=walk)] * 201, None, "At most 200 cuts in one sequence"),
        (
            [CutIn(id=walk)],
            "iPad",
            "No path profile called 'iPad'. Profiles: Mac Mini "
            "(add one per library on the Libraries page).",
        ),
        ([CutIn(id="barn")], None, "'barn' is not an asset id"),
        ([CutIn(id="0000000000")], None, "No asset 0000000000"),
        ([CutIn(id=str(uuid.UUID(int=7)))], None, f"No asset {uuid.UUID(int=7)}"),
        (
            [CutIn(id=walk, in_s=200)],
            None,
            "Walkthrough.MP4: out_s (120) must be after in_s (200); the clip is 120s long",
        ),
    ]
    async with env["factory"]() as db:
        for cuts, profile, sentence in asks:
            with pytest.raises(CutError) as refused:
                await roughcut.build_rough_cut(db, "x", cuts, profile=profile)
            assert str(refused.value) == sentence
        assert await _cuts(db) == 0


async def test_a_short_id_that_fits_two_clips_asks_for_more_of_it(env: dict[str, Any]) -> None:
    async with env["factory"]() as db:
        for tail in (1, 2):
            db.add(
                Asset(
                    id=uuid.UUID(f"abcdef01-0000-4000-8000-00000000000{tail}"),
                    library_id=env["library_id"],
                    relative_path=f"twin{tail}.mp4",
                    filename=f"twin{tail}.mp4",
                    extension="mp4",
                    media_type="video",
                    size_bytes=1,
                    mtime=BASE,
                )
            )
        await db.commit()
        with pytest.raises(CutError, match="^'abcdef01' matches more than one asset; give more"):
            await roughcut.find_asset(db, "abcdef01")
        found = await roughcut.find_asset(db, "abcdef01-0000-4000-8000-000000000002")
    assert found.filename == "twin2.mp4"


async def test_the_timeline_takes_the_frame_rate_most_of_it_was_shot_at(
    env: dict[str, Any],
) -> None:
    """A two-second 59.94 opener before twenty seconds of 29.97: left alone
    the timeline runs at the opener's rate (the connector's behaviour, which
    must not change); given the majority's rate it runs at that."""
    ids = env["ids"]
    cuts = [
        CutIn(id=str(ids["DJI_0100.MP4"]), in_s=0, out_s=2),
        CutIn(id=str(ids["front.jpg"]), out_s=3),
        CutIn(id=str(ids["Walkthrough.MP4"]), in_s=30, out_s=50),
    ]
    spans = [(59.94, 2.0), (None, 3.0), (29.97, 20.0)]
    rate = roughcut.majority_rate(spans)
    assert rate == 29.97

    async with env["factory"]() as db:
        first = await roughcut.build_rough_cut(db, "Mixed", cuts, record=False)
        chosen = await roughcut.build_rough_cut(db, "Mixed", cuts, record=False, sequence_fps=rate)

    assert _timebase(first.xml) == ("60", "TRUE")
    assert _timebase(chosen.xml) == ("30", "TRUE")


def test_the_majority_rate_groups_near_rates_and_keeps_the_first_of_a_tie() -> None:
    assert roughcut.majority_rate([]) is None
    assert roughcut.majority_rate([(None, 4.0), (0.0, 2.0)]) is None
    # A probe's 29.97003 is the same rate as 29.97; together they outweigh 25.
    assert roughcut.majority_rate([(29.97003, 3.0), (25.0, 5.0), (29.97, 3.0)]) == 29.97003
    assert roughcut.majority_rate([(23.976, 4.0), (59.94, 4.0)]) == 23.976


# --- families: a cut that revises another ----------------------------------------


async def test_a_parent_that_does_not_exist_is_refused_before_anything_is_built(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mistyped parent must not quietly start a family of its own, and the
    refusal says where the ids are. Building is made to fail, so a check
    that came after it would show as that failure instead."""

    def no_building(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("built before the parent was checked")

    monkeypatch.setattr(roughcut.fcp7, "build_sequence", no_building)
    cuts = [CutIn(id=str(env["ids"]["Walkthrough.MP4"]), in_s=1, out_s=3)]
    async with env["factory"]() as db:
        for parent, record in (("deadbeef00", True), ("barn", True), (uuid.UUID(int=9), False)):
            with pytest.raises(CutError) as refused:
                await roughcut.build_rough_cut(db, "Draft", cuts, parent=parent, record=record)
            assert str(refused.value) == (
                f"No cut {str(parent)!r}. A local draft's id is in the review request about "
                "it; recent_cuts with verbose lists the ids of finished cuts."
            )
        # A provenance name that is no column is the caller's bug: raised,
        # never swallowed as a failure to record.
        with pytest.raises(ValueError, match="^not provenance: colour$"):
            await roughcut.build_rough_cut(db, "Draft", cuts, provenance={"colour": "warm"})
        assert await _cuts(db) == 0


async def test_a_revision_is_kept_beside_the_cut_it_revises(env: dict[str, Any]) -> None:
    """Claude's reviewed cut, and a local draft drafted again, are the next
    version of the same family; the first is never replaced, and revising an
    older version still numbers after the newest."""
    ids = env["ids"]
    walk = CutIn(id=str(ids["Walkthrough.MP4"]), in_s=10, out_s=14, note="Front door")
    drone = CutIn(id=str(ids["DJI_0100.MP4"]), in_s=2, out_s=6)
    made_by = {
        "model": "google/gemma-4-12B-it",
        "model_revision": "de9e183868050b34bf6b4272518500ffcebf15b3",
        "runtime": "mlx-vlm",
        "runtime_version": "0.7.12",
        "prompt_version": "editor-1",
    }
    async with env["factory"]() as db:
        first = await roughcut.build_rough_cut(
            db, "Brookside 30", [walk], source="local", provenance=made_by
        )
        assert first.cut_id is not None
        # Claude names it by the short id recent_cuts prints.
        second = await roughcut.build_rough_cut(
            db, "Brookside 30, reviewed", [walk, drone], parent=f" {first.cut_id[:8]} "
        )
        third = await roughcut.build_rough_cut(
            db, "Brookside 30", [drone], source="local", parent=uuid.UUID(first.cut_id)
        )
        # A blank parent revises nothing.
        apart = await roughcut.build_rough_cut(db, "Something else", [drone], parent="  ")
        assert third.cut_id is not None and apart.cut_id is not None
        newest = await roughcut.find_cut(db, third.cut_id)
        versions = await shotlist.family(db, newest)
        oldest = await roughcut.find_cut(db, first.cut_id)
        assert [c.id for c in await shotlist.family(db, oldest)] == [c.id for c in versions]
        alone = await shotlist.family(db, await roughcut.find_cut(db, apart.cut_id))

    assert [str(c.id) for c in versions] == [first.cut_id, second.cut_id, third.cut_id]
    assert [(first.version, second.version, third.version)] == [(1, 2, 3)]
    v1, v2, v3 = versions
    assert (v1.root_id, v1.parent_id, v1.title) == (None, None, "Brookside 30")
    assert (v2.root_id, v2.parent_id, v2.source) == (v1.id, v1.id, "mcp")
    assert (v3.root_id, v3.parent_id, v3.version) == (v1.id, v1.id, 3)
    assert "Kept as" not in first.note
    assert second.note == (
        "2 cuts, 8.0s. 2 file(s) have no path for any profile (none given); they use the "
        "server's path and will import offline until relinked. Recorded in cut history. "
        "Kept as version 2 beside the cut it revises."
    )
    assert third.note.endswith(" Kept as version 3 beside the cut it revises.")
    # Which build drafted it goes with the draft, not with the revisions.
    assert {name: getattr(v1, name) for name in made_by} == made_by
    assert v2.model is None and v3.model is None
    assert [(c.version, c.root_id) for c in alone] == [(1, None)] and apart.version == 1


# --- a cut again, from history -----------------------------------------------------


def _without_paths(xml: str) -> str:
    return re.sub(r"<pathurl>[^<]*</pathurl>", "<pathurl/>", xml)


async def test_a_cut_rebuilt_for_another_computer_differs_only_in_its_paths(
    env: dict[str, Any],
) -> None:
    """A cut is kept as pieces, so it downloads for the Mac today and the PC
    tomorrow. Rebuilt for the computer it was made for it is the very file
    that was made, markers and all; for another, only the paths change."""
    ids = env["ids"]
    async with env["factory"]() as db:
        db.add(
            PathMapping(
                library_id=env["library_id"],
                profile_name="DJ's PC",
                platform="windows",
                mapped_prefix="Y:\\",
            )
        )
        await db.commit()
        made = await roughcut.build_rough_cut(
            db,
            "Brookside 30",
            [
                CutIn(id=str(ids["Walkthrough.MP4"]), in_s=10, out_s=14, note="Front door"),
                CutIn(id=str(ids["front.jpg"]), out_s=3),
                CutIn(id=str(ids["DJI_0100.MP4"]), in_s=2, out_s=4, note="Over the roof"),
            ],
            profile="Mac Mini",
        )
        assert made.cut_id is not None
        cut = await roughcut.find_cut(db, made.cut_id)
        mac = await roughcut.rebuild(db, cut, profile="Mac Mini")
        pc = await roughcut.rebuild(db, cut, profile="DJ's PC")
        bare = await roughcut.rebuild(db, cut, profile=None)
        with pytest.raises(CutError, match="^No path profile called 'iPad'"):
            await roughcut.rebuild(db, cut, profile="iPad")
        # Rebuilding records nothing.
        assert await _cuts(db) == 1

    assert mac.cut.xml == made.xml and (mac.missing, mac.offline) == ([], [])
    assert pc.cut.xml != mac.cut.xml
    assert _without_paths(pc.cut.xml) == _without_paths(mac.cut.xml)
    assert all(
        p.text and p.text.startswith("file://localhost/Y%3A/Brookside/")
        for p in _sequence(pc.cut.xml).iter("pathurl")
    )
    assert (pc.cut.profile, pc.cut.unmapped, pc.cut.cuts) == ("DJ's PC", [], 3)
    assert bare.cut.unmapped == ["Walkthrough.MP4", "front.jpg", "DJI_0100.MP4"]


async def test_a_cut_rebuilt_from_history_runs_at_the_rate_most_of_it_was_shot_at(
    env: dict[str, Any],
) -> None:
    """Claude's call left the 59.94 opener to set its timeline, as it always
    has; the download made from history runs at the 29.97 most of it is."""
    ids = env["ids"]
    async with env["factory"]() as db:
        made = await roughcut.build_rough_cut(
            db,
            "Mixed",
            [
                CutIn(id=str(ids["DJI_0100.MP4"]), in_s=0, out_s=2),
                CutIn(id=str(ids["Walkthrough.MP4"]), in_s=30, out_s=50),
            ],
        )
        assert made.cut_id is not None
        again = await roughcut.rebuild(db, await roughcut.find_cut(db, made.cut_id), profile=None)
    assert _timebase(made.xml) == ("60", "TRUE")
    assert _timebase(again.cut.xml) == ("30", "TRUE")


async def test_a_deleted_clip_is_left_out_of_the_download_and_named(
    env: dict[str, Any],
) -> None:
    """A clip deleted from the catalogue cannot be linked, so it is left out
    and named; one only off the share is written (it relinks when the share
    is back) and named. With nothing left there is nothing to send."""
    ids = env["ids"]
    async with env["factory"]() as db:
        made = await roughcut.build_rough_cut(
            db,
            "Brookside 30",
            [
                CutIn(id=str(ids["Walkthrough.MP4"]), in_s=10, out_s=14),
                CutIn(id=str(ids["DJI_0100.MP4"]), in_s=2, out_s=4),
                CutIn(id=str(ids["front.jpg"]), out_s=3),
                CutIn(id=str(ids["DJI_0100.MP4"]), in_s=8, out_s=9),
            ],
        )
        assert made.cut_id is not None
        cut = await roughcut.find_cut(db, made.cut_id)
        # The drone clip deleted where the key is not enforced (its pieces
        # point at nothing); the photo off the share.
        drone = await db.get(Asset, ids["DJI_0100.MP4"])
        photo = await db.get(Asset, ids["front.jpg"])
        assert drone is not None and photo is not None
        await db.execute(delete(Asset).where(Asset.id == drone.id))
        photo.availability = "unmounted"
        await db.commit()
        rebuilt = await roughcut.rebuild(db, cut, profile=None)

        # The walk-through deleted where the key is enforced (SET NULL).
        for piece in (await db.execute(select(CutPiece))).scalars():
            if piece.filename == "Walkthrough.MP4":
                piece.asset_id = None
        await db.execute(delete(Asset).where(Asset.id == photo.id))
        await db.commit()
        with pytest.raises(roughcut.NothingToSend) as refused:
            await roughcut.rebuild(db, cut, profile=None)

    assert rebuilt.missing == ["DJI_0100.MP4"] and rebuilt.offline == ["front.jpg"]
    assert rebuilt.cut.cuts == 2 and rebuilt.cut.duration_s == pytest.approx(4 + 3)
    names = [n.text for n in _sequence(rebuilt.cut.xml).iter("name")]
    assert "front.jpg" in names and "DJI_0100.MP4" not in names
    assert isinstance(refused.value, CutError)
    assert str(refused.value) == (
        "Every clip in this cut has been deleted from the catalogue, so there is nothing to send."
    )
