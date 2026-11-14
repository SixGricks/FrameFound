"""The pictures a model is shown (framefound/looking/pictures.py).

Pictures are small images drawn with Pillow under `tmp_path`, at the paths
the app uses: a shot's saved stills, sampled frames, a photograph's preview
and thumbnail, a clip's poster. Nothing here runs ffmpeg or a model, and a
test proves nothing is grabbed from a playable copy.
"""

import io
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Derivative, Frame, Library
from framefound.looking import pictures
from framefound.looking.scope import Item
from framefound.processing import shots as shot_files
from framefound.storage import stores

BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
SERVER = Path(__file__).resolve().parents[1]


def _picture(path: Path, shade: tuple[int, int, int], size: tuple[int, int] = (64, 36)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "WEBP" if path.suffix == ".webp" else "JPEG"
    Image.new("RGB", size, shade).save(path, fmt)
    return path


def _shade(jpeg: bytes) -> tuple[int, int, int]:
    """The colour of a one-colour picture, give or take JPEG."""
    with Image.open(io.BytesIO(jpeg)) as image:
        red, green, blue = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
    return red, green, blue


def _near(jpeg: bytes, shade: tuple[int, int, int]) -> bool:
    return all(abs(a - b) <= 6 for a, b in zip(_shade(jpeg), shade, strict=True))


@dataclass
class Catalogue:
    factory: Any
    data: Path
    library: uuid.UUID

    def session(self) -> AsyncSession:
        session: AsyncSession = self.factory()
        return session

    async def asset(self, name: str, media_type: str, derived: dict[str, str]) -> uuid.UUID:
        """An asset with these derived files ready: kind -> file name."""
        async with self.session() as db:
            asset = Asset(
                library_id=self.library,
                relative_path=f"Shoot/{name}",
                filename=name,
                extension=name.rsplit(".", 1)[-1],
                media_type=media_type,
                size_bytes=1000,
                mtime=BASE,
            )
            db.add(asset)
            await db.flush()
            for kind, file in derived.items():
                db.add(
                    Derivative(
                        asset_id=asset.id,
                        kind=kind,
                        relative_path=f"derivatives/{str(asset.id)[:2]}/{asset.id}/{file}",
                        media_format=file.rsplit(".", 1)[-1],
                        status="ready",
                    )
                )
            await db.commit()
            return asset.id

    def derived(self, asset_id: uuid.UUID, file: str) -> Path:
        return self.data / f"derivatives/{str(asset_id)[:2]}/{asset_id}/{file}"

    def still(self, asset_id: uuid.UUID, moment: int, shade: tuple[int, int, int]) -> Path:
        return _picture(self.data / shot_files.still_relpath(asset_id, moment), shade)

    async def frame(
        self, asset_id: uuid.UUID, moment: int, shade: tuple[int, int, int] | None
    ) -> Path:
        """A sampled frame row, with its file unless `shade` is None."""
        relative = f"frames/{asset_id}/{moment:09d}.jpg"
        async with self.session() as db:
            db.add(Frame(asset_id=asset_id, ts_ms=moment, relative_path=relative))
            await db.commit()
        path = self.data / relative
        return _picture(path, shade) if shade is not None else path

    async def shown(self, item: Item, stills: int = 3) -> list[bytes]:
        async with self.session() as db:
            return await pictures.for_item(db, item, stills=stills, data_dir=self.data)


def _shot(asset_id: uuid.UUID, start: int, end: int) -> Item:
    return Item(asset_id, "drone.mp4", "video", start, end, None)


def _whole(asset_id: uuid.UUID, media_type: str = "image") -> Item:
    return Item(asset_id, "front.jpg", media_type, None, None, None)


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    get_settings.cache_clear()
    stores._CACHE.clear()


@pytest.fixture()
async def cat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Catalogue]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'pictures.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "pictures-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path / "gelco"))
        db.add(library)
        await db.commit()
    (tmp_path / "data").mkdir()
    yield Catalogue(factory, tmp_path / "data", library.id)
    await engine.dispose()


# --- shots ---------------------------------------------------------------------------


async def test_a_shot_is_shown_its_start_middle_and_end(cat: Catalogue) -> None:
    clip = await cat.asset("drone.mp4", "video", {})
    start, middle, end = shot_files.window_moments(0, 9000)
    cat.still(clip, start, (200, 30, 30))
    cat.still(clip, middle, (30, 200, 30))
    cat.still(clip, end, (30, 30, 200))

    shown = await cat.shown(_shot(clip, 0, 9000))

    assert [
        _near(p, s)
        for p, s in zip(shown, [(200, 30, 30), (30, 200, 30), (30, 30, 200)], strict=True)
    ] == [True] * 3
    assert pictures.moments_for(0, 9000, 3) == [start, middle, end]


async def test_one_picture_per_shot_when_the_setting_says_so(cat: Catalogue) -> None:
    clip = await cat.asset("drone.mp4", "video", {})
    for moment, shade in zip(
        shot_files.window_moments(0, 9000),
        [(200, 30, 30), (30, 200, 30), (30, 30, 200)],
        strict=True,
    ):
        cat.still(clip, moment, shade)

    shown = await cat.shown(_shot(clip, 0, 9000), stills=1)

    assert len(shown) == 1 and _near(shown[0], (30, 200, 30))
    assert pictures.moments_for(0, 9000, 1) == [4500]


async def test_the_pictures_per_shot_are_three_or_one(cat: Catalogue) -> None:
    for wrong in (0, 2, 4, -1):
        with pytest.raises(ValueError):
            pictures.moments_for(0, 9000, wrong)


async def test_a_short_shot_is_one_picture_not_three_copies_of_it(cat: Catalogue) -> None:
    clip = await cat.asset("drone.mp4", "video", {})
    (moment,) = set(shot_files.window_moments(1000, 1400))
    cat.still(clip, moment, (120, 120, 40))

    assert pictures.moments_for(1000, 1400, 3) == [moment]
    assert len(await cat.shown(_shot(clip, 1000, 1400))) == 1


async def test_a_missing_still_is_a_frame_sampled_close_by_but_never_one_far_off(
    cat: Catalogue,
) -> None:
    clip = await cat.asset("drone.mp4", "video", {})
    start, middle, end = shot_files.window_moments(0, 9000)
    cat.still(clip, middle, (30, 200, 30))
    await cat.frame(clip, start + 400, (200, 30, 30))  # within 0.6 s of the start
    await cat.frame(clip, end - 2500, (30, 30, 200))  # far from the end

    shown = await cat.shown(_shot(clip, 0, 9000))

    assert len(shown) == 2
    assert _near(shown[0], (200, 30, 30)) and _near(shown[1], (30, 200, 30))


async def test_two_moments_that_come_to_the_same_file_are_one_picture(cat: Catalogue) -> None:
    clip = await cat.asset("drone.mp4", "video", {})
    start, middle, end = shot_files.window_moments(0, 1600)
    # One sampled frame within 0.6 s of all three moments.
    await cat.frame(clip, middle, (90, 90, 90))

    assert len({start, middle, end}) == 3
    assert len(await cat.shown(_shot(clip, 0, 1600))) == 1


async def test_a_broken_still_is_stepped_past_to_a_frame_that_reads(cat: Catalogue) -> None:
    clip = await cat.asset("drone.mp4", "video", {})
    middle = shot_files.window_moments(0, 9000)[1]
    broken = cat.data / shot_files.still_relpath(clip, middle)
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"\xff\xd8\xff\xe0 half a jpeg")
    await cat.frame(clip, middle + 200, (30, 200, 30))

    shown = await cat.shown(_shot(clip, 0, 9000), stills=1)

    assert len(shown) == 1 and _near(shown[0], (30, 200, 30))


async def test_a_clip_whose_files_are_all_broken_has_no_picture_and_the_search_ends(
    cat: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = await cat.asset("drone.mp4", "video", {})
    middle = shot_files.window_moments(0, 9000)[1]
    for offset in range(0, 600, 50):  # twelve broken frames within 0.6 s
        frame = await cat.frame(clip, middle + offset, None)
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(b"not a picture")
    asked: list[int] = []
    real = pictures.shotlist.still_found

    async def counting(*args: Any, **kwargs: Any) -> Any:
        asked.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(pictures.shotlist, "still_found", counting)

    assert await cat.shown(_shot(clip, 0, 9000), stills=1) == []
    assert len(asked) == pictures.UNREADABLE_LIMIT


async def test_a_lookup_that_hands_back_a_broken_file_again_still_ends(
    cat: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = await cat.asset("drone.mp4", "video", {})
    broken = cat.data / "broken.jpeg"
    broken.write_bytes(b"not a picture")

    async def stubborn(*_args: Any, **_kwargs: Any) -> Any:
        return broken, 4500

    monkeypatch.setattr(pictures.shotlist, "still_found", stubborn)

    assert await cat.shown(_shot(clip, 0, 9000)) == []


async def test_a_shot_with_no_picture_on_disk_has_none_and_nothing_is_grabbed(
    cat: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = await cat.asset("drone.mp4", "video", {"proxy": "proxy.mp4", "poster": "poster.jpg"})
    _picture(cat.derived(clip, "poster.jpg"), (10, 10, 10))
    cat.derived(clip, "proxy.mp4").write_bytes(b"a playable copy")
    await cat.frame(clip, 30_000, (50, 50, 50))  # far away: not this shot
    await cat.frame(clip, 4_000, None)  # a row whose file is gone

    def grab(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a still was grabbed from the playable copy")

    monkeypatch.setattr(shot_files, "still_at", grab)
    monkeypatch.setattr(shot_files, "_grab", grab)
    before = sorted(cat.data.rglob("*"))

    assert await cat.shown(_shot(clip, 0, 9000)) == []
    # Not the poster either: a shot is shown only pictures of the shot.
    assert sorted(cat.data.rglob("*")) == before


# --- whole items ---------------------------------------------------------------------


async def test_a_photograph_is_shown_its_preview_else_its_thumbnail(cat: Catalogue) -> None:
    both = await cat.asset("front.jpg", "image", {"preview": "p.webp", "thumbnail": "t.webp"})
    _picture(cat.derived(both, "p.webp"), (200, 180, 160), (1600, 1200))
    _picture(cat.derived(both, "t.webp"), (20, 20, 20))
    thumb_only = await cat.asset("back.jpg", "image", {"preview": "p.webp", "thumbnail": "t.webp"})
    _picture(cat.derived(thumb_only, "t.webp"), (40, 160, 40))  # its preview file is gone
    neither = await cat.asset("side.jpg", "image", {"preview": "p.webp"})

    shown = await cat.shown(_whole(both))
    assert len(shown) == 1 and _near(shown[0], (200, 180, 160))
    shown = await cat.shown(_whole(thumb_only))
    assert len(shown) == 1 and _near(shown[0], (40, 160, 40))
    assert await cat.shown(_whole(neither)) == []


async def test_a_derived_file_that_is_not_ready_is_not_shown(cat: Catalogue) -> None:
    photo = await cat.asset("front.jpg", "image", {"preview": "p.webp"})
    _picture(cat.derived(photo, "p.webp"), (200, 180, 160))
    async with cat.session() as db:
        from sqlalchemy import update

        await db.execute(update(Derivative).values(status="failed"))
        await db.commit()

    assert await cat.shown(_whole(photo)) == []


async def test_a_whole_clip_is_shown_its_poster(cat: Catalogue) -> None:
    raw = await cat.asset("A001.braw", "video", {"poster": "poster.jpeg", "thumbnail": "t.webp"})
    _picture(cat.derived(raw, "poster.jpeg"), (150, 60, 200), (1920, 1080))
    _picture(cat.derived(raw, "t.webp"), (10, 10, 10))

    shown = await cat.shown(_whole(raw, "video"))

    assert len(shown) == 1 and _near(shown[0], (150, 60, 200))


async def test_a_whole_clip_with_only_its_thumbnail_is_shown_the_thumbnail(
    cat: Catalogue,
) -> None:
    # The scope counts such a clip as one that can be judged from a still and
    # hands it to the pass; it must not then turn out to have no picture.
    raw = await cat.asset("A002.braw", "video", {"thumbnail": "t.webp"})
    _picture(cat.derived(raw, "t.webp"), (40, 160, 40))

    shown = await cat.shown(_whole(raw, "video"))

    assert len(shown) == 1 and _near(shown[0], (40, 160, 40))


async def test_pictures_are_the_small_jpeg_claudes_reviewer_sends(cat: Catalogue) -> None:
    photo = await cat.asset("front.jpg", "image", {"preview": "p.webp"})
    _picture(cat.derived(photo, "p.webp"), (200, 180, 160), (2400, 1600))

    (shown,) = await cat.shown(_whole(photo))

    assert shown[:3] == b"\xff\xd8\xff"
    with Image.open(io.BytesIO(shown)) as image:
        assert max(image.size) <= 768


def test_reading_a_picture_never_raises() -> None:
    assert pictures.read_picture(None) is None
    assert pictures.read_picture(Path("no/such/file.jpg")) is None
    assert pictures.for_shot([None, Path("no/such/file.jpg")]) == []
    assert pictures.for_photo(Path("nowhere"), None, None) is None


def test_looking_at_pictures_loads_neither_the_connector_nor_the_decoder() -> None:
    # The local-AI lane imports these; the connector imports the optional
    # `mcp` package at import, and the decode path brings numpy.
    code = (
        "import sys\n"
        "import framefound.looking.pictures, framefound.looking.scope\n"
        "loaded = [m for m in sys.modules if m.split('.')[0] == 'mcp'\n"
        "          or m.startswith('framefound.mcp') or m == 'framefound.processing.shots']\n"
        "print(loaded)\n"
    )
    result = subprocess.run(  # noqa: S603 - this interpreter, fixed code
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=SERVER, check=True
    )
    assert result.stdout.strip() == "[]"
