"""Location inference against a real database.

The task's correctness lives in two places that unit-testing the geo helpers
alone would miss: the time-window bisect that decides which anchors are even
considered, and the promise that a real coordinate is never overwritten.
"""

import math
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Frame, Library

BASE_TIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
BARN = (41.8781, -87.6298)
FAR_AWAY = (25.7617, -80.1918)


def _unit(*, tilt: float) -> list[float]:
    """A 512-d unit vector. `tilt` rotates it away from the reference vector,
    so cosine similarity against tilt=0 is exactly cos(tilt)."""
    vec = [0.0] * 512
    vec[0] = math.cos(tilt)
    vec[1] = math.sin(tilt)
    return vec


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Session:
    url = f"sqlite:///{(tmp_path / 'geo.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url.replace("sqlite:", "sqlite+aiosqlite:"))
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "geo-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()
    get_settings.cache_clear()


def _add(
    db: Session,
    library: Library,
    name: str,
    *,
    minutes: float,
    tilt: float,
    gps: tuple[float, float] | None,
) -> Asset:
    asset = Asset(
        library_id=library.id,
        relative_path=name,
        filename=name,
        extension="jpg",
        media_type="image",
        size_bytes=1000,
        mtime=BASE_TIME,
        captured_at=BASE_TIME + timedelta(minutes=minutes),
        gps_lat=gps[0] if gps else None,
        gps_lon=gps[1] if gps else None,
        availability="online",
    )
    db.add(asset)
    db.flush()
    db.add(
        Frame(
            asset_id=asset.id, ts_ms=0, relative_path=f"f/{name}.jpeg", embedding=_unit(tilt=tilt)
        )
    )
    db.commit()
    return asset


def _run(library_id: uuid.UUID) -> None:
    from framefound.processing.tasks import infer_locations

    infer_locations(str(library_id))


def test_a_close_match_in_time_and_look_is_filled(db: Session, tmp_path: Path) -> None:
    library = Library(name="L", root_path=str(tmp_path))
    db.add(library)
    db.commit()
    _add(db, library, "drone.jpg", minutes=0, tilt=0.0, gps=BARN)
    subject = _add(db, library, "dslr.jpg", minutes=5, tilt=0.1, gps=None)

    _run(library.id)

    db.expire_all()
    filled = db.get(Asset, subject.id)
    assert filled is not None
    assert filled.gps_lat == pytest.approx(BARN[0])
    assert filled.gps_source == "inferred"
    assert filled.gps_confidence and filled.gps_confidence > 0.35


def test_same_place_hours_later_is_not_filled(db: Session, tmp_path: Path) -> None:
    # Beyond the two-hour window the bisect must exclude the anchor outright,
    # however similar the two frames look.
    library = Library(name="L", root_path=str(tmp_path))
    db.add(library)
    db.commit()
    _add(db, library, "drone.jpg", minutes=0, tilt=0.0, gps=BARN)
    subject = _add(db, library, "later.jpg", minutes=300, tilt=0.0, gps=None)

    _run(library.id)

    db.expire_all()
    assert db.get(Asset, subject.id).gps_lat is None  # type: ignore[union-attr]


def test_same_minute_but_a_different_scene_is_not_filled(db: Session, tmp_path: Path) -> None:
    # Two crews shooting different subjects at the same moment must not
    # borrow each other's coordinates.
    library = Library(name="L", root_path=str(tmp_path))
    db.add(library)
    db.commit()
    _add(db, library, "drone.jpg", minutes=0, tilt=0.0, gps=BARN)
    subject = _add(db, library, "elsewhere.jpg", minutes=2, tilt=1.4, gps=None)

    _run(library.id)

    db.expire_all()
    assert db.get(Asset, subject.id).gps_lat is None  # type: ignore[union-attr]


def test_real_coordinates_are_never_overwritten(db: Session, tmp_path: Path) -> None:
    library = Library(name="L", root_path=str(tmp_path))
    db.add(library)
    db.commit()
    _add(db, library, "drone.jpg", minutes=0, tilt=0.0, gps=BARN)
    known = _add(db, library, "phone.jpg", minutes=1, tilt=0.05, gps=FAR_AWAY)

    _run(library.id)

    db.expire_all()
    still = db.get(Asset, known.id)
    assert still is not None
    assert still.gps_lat == pytest.approx(FAR_AWAY[0])
    assert still.gps_source != "inferred"


def test_the_nearest_anchor_in_the_window_wins(db: Session, tmp_path: Path) -> None:
    library = Library(name="L", root_path=str(tmp_path))
    db.add(library)
    db.commit()
    _add(db, library, "far.jpg", minutes=-100, tilt=0.0, gps=FAR_AWAY)
    _add(db, library, "near.jpg", minutes=-2, tilt=0.0, gps=BARN)
    subject = _add(db, library, "subject.jpg", minutes=0, tilt=0.0, gps=None)

    _run(library.id)

    db.expire_all()
    filled = db.get(Asset, subject.id)
    assert filled is not None
    assert filled.gps_lat == pytest.approx(BARN[0])


def test_an_asset_with_no_embedding_is_skipped(db: Session, tmp_path: Path) -> None:
    library = Library(name="L", root_path=str(tmp_path))
    db.add(library)
    db.commit()
    _add(db, library, "drone.jpg", minutes=0, tilt=0.0, gps=BARN)
    naked = Asset(
        library_id=library.id,
        relative_path="no-frames.jpg",
        filename="no-frames.jpg",
        extension="jpg",
        media_type="image",
        size_bytes=1000,
        mtime=BASE_TIME,
        captured_at=BASE_TIME,
        availability="online",
    )
    db.add(naked)
    db.commit()

    _run(library.id)  # must not raise

    db.expire_all()
    assert db.get(Asset, naked.id).gps_lat is None  # type: ignore[union-attr]


def test_inference_records_which_asset_it_borrowed_from(db: Session, tmp_path: Path) -> None:
    library = Library(name="L", root_path=str(tmp_path))
    db.add(library)
    db.commit()
    anchor = _add(db, library, "drone.jpg", minutes=0, tilt=0.0, gps=BARN)
    subject = _add(db, library, "dslr.jpg", minutes=3, tilt=0.05, gps=None)

    _run(library.id)

    db.expire_all()
    assert db.get(Asset, subject.id).gps_inferred_from == anchor.id  # type: ignore[union-attr]


def test_running_twice_changes_nothing_the_second_time(db: Session, tmp_path: Path) -> None:
    library = Library(name="L", root_path=str(tmp_path))
    db.add(library)
    db.commit()
    _add(db, library, "drone.jpg", minutes=0, tilt=0.0, gps=BARN)
    _add(db, library, "dslr.jpg", minutes=3, tilt=0.05, gps=None)

    _run(library.id)
    db.expire_all()
    first = {a.id: (a.gps_lat, a.gps_confidence) for a in db.execute(select(Asset)).scalars()}

    _run(library.id)
    db.expire_all()
    second = {a.id: (a.gps_lat, a.gps_confidence) for a in db.execute(select(Asset)).scalars()}

    assert first == second


def test_an_inferred_position_never_anchors_another_inference(db: Session, tmp_path: Path) -> None:
    """Found in deployment: a second run kept filling more assets, because
    everything filled by the first run had become an anchor. Chaining a guess
    off a guess carries no confidence penalty, so drift compounds silently.

    The hops are 70 minutes apart: close enough to clear the confidence floor
    against the link before, while the far one sits outside the two-hour
    window from the only real GPS in the library.
    """
    library = Library(name="L", root_path=str(tmp_path))
    db.add(library)
    db.commit()
    _add(db, library, "drone.jpg", minutes=0, tilt=0.0, gps=BARN)
    middle = _add(db, library, "hop1.jpg", minutes=70, tilt=0.02, gps=None)
    far = _add(db, library, "hop2.jpg", minutes=140, tilt=0.02, gps=None)

    _run(library.id)
    _run(library.id)  # a second pass must not extend the chain

    db.expire_all()
    assert db.get(Asset, middle.id).gps_lat == pytest.approx(BARN[0])  # type: ignore[union-attr]
    assert db.get(Asset, far.id).gps_lat is None, (  # type: ignore[union-attr]
        "an inferred position was used as an anchor"
    )
