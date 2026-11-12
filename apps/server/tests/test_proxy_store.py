"""A separate, movable proxy store (storage/stores.py), and everything that
has to respect it: generation, the scanner, the move command and the
"what is missing" check."""

import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Derivative, Library
from framefound.native import envfile, proxies
from framefound.native.layout import Layout
from framefound.ops import proxy_store
from framefound.processing import derivatives as deriv
from framefound.processing import shots
from framefound.scanner.scan import _walk
from framefound.storage import stores
from framefound.storage.stores import PROXY_STORE_MARKER
from framefound.storage.worker_roots import DATA_STORE_MARKER

PROXY = "derivatives/ab/abcd/proxy.mp4"
THUMB = "derivatives/ab/abcd/thumbnail.webp"


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def settings(data: Path, proxy: Path | None = None) -> Settings:
    return Settings(data_dir=data, proxy_dir=proxy)


def use(monkeypatch: pytest.MonkeyPatch, data: Path, proxy: Path | None) -> None:
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(data))
    monkeypatch.setenv("FRAMEFOUND_PROXY_DIR", str(proxy) if proxy else "")
    get_settings.cache_clear()


# --- where a derived file lives ---------------------------------------------


def test_only_proxies_count_as_proxies() -> None:
    assert stores.is_proxy(PROXY)
    assert stores.is_proxy("derivatives\\ab\\abcd\\proxy.mp4")
    assert not stores.is_proxy(THUMB)
    assert not stores.is_proxy("shots/ab/abcd/000001000.jpeg")
    assert not stores.is_proxy("renders/proxy.mp4")  # a render that happens to be named so


def test_without_a_proxy_store_everything_is_under_the_data_dir(tmp_path: Path) -> None:
    here = settings(tmp_path)
    assert stores.derivative_path(tmp_path, PROXY, here) == tmp_path / PROXY
    assert stores.proxy_store_problem(here) is None
    assert stores.separate_proxy_store(here) is None


def test_a_proxy_store_takes_proxies_and_nothing_else(tmp_path: Path) -> None:
    data, nas = tmp_path / "data", tmp_path / "nas"
    here = settings(data, nas)
    assert stores.derivative_path(data, PROXY, here) == nas / PROXY
    assert stores.derivative_path(data, THUMB, here) == data / THUMB


def test_a_blank_proxy_dir_setting_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FRAMEFOUND_PROXY_DIR=` in the env file must not become the current
    working directory."""
    monkeypatch.setenv("FRAMEFOUND_PROXY_DIR", "  ")
    assert Settings().proxy_dir is None


def test_a_proxy_store_without_its_marker_is_not_connected(tmp_path: Path) -> None:
    nas = tmp_path / "nas"
    nas.mkdir()  # the empty folder an unmounted share leaves behind
    here = settings(tmp_path / "data", nas)
    assert "not connected" in (stores.proxy_store_problem(here) or "")
    stores.mark_proxy_store(nas)
    assert stores.proxy_store_problem(here) is None


# --- generation ------------------------------------------------------------------


def test_nothing_is_created_in_a_store_that_is_away(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data, nas = tmp_path / "data", tmp_path / "nas"
    nas.mkdir()
    use(monkeypatch, data, nas)
    record = SimpleNamespace(relative_path=PROXY)

    with pytest.raises(deriv.ProxyStoreUnavailable):
        deriv._abs(data, record)  # type: ignore[arg-type]

    assert list(nas.iterdir()) == []  # not even the folders
    # Everything else is unaffected by the proxy store being away.
    assert deriv._abs(data, SimpleNamespace(relative_path=THUMB)) == data / THUMB  # type: ignore[arg-type]


def test_a_missing_proxy_store_pauses_like_a_full_disk() -> None:
    """The task wrappers already treat OutOfSpace as "wait, do not fail"."""
    assert issubclass(deriv.ProxyStoreUnavailable, deriv.OutOfSpace)


def test_the_shot_code_finds_the_proxy_in_its_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data, nas = tmp_path / "data", tmp_path / "nas"
    use(monkeypatch, data, nas)
    asset_id = uuid.uuid4()
    assert shots.proxy_path(data, asset_id) == nas / deriv.derivative_relpath(
        asset_id, "proxy", "mp4"
    )


# --- the scanner ---------------------------------------------------------------


@pytest.mark.parametrize("marker", [PROXY_STORE_MARKER, DATA_STORE_MARKER])
def test_the_scanner_never_catalogues_a_store(tmp_path: Path, marker: str) -> None:
    """A proxy store on the same share as the footage must not come back as
    footage — every proxy a second, worse copy of its own original."""
    (tmp_path / "2026").mkdir()
    (tmp_path / "2026" / "clip.mp4").write_bytes(b"x")
    store = tmp_path / "FrameFound Proxies"
    (store / "derivatives" / "ab" / "abcd").mkdir(parents=True)
    (store / "derivatives" / "ab" / "abcd" / "proxy.mp4").write_bytes(b"x")
    (store / marker).write_text("store")

    found = sorted(rel for rel, _stat in _walk(tmp_path, {"mp4"}, []))

    assert found == ["2026/clip.mp4"]


# --- framefound proxies move --------------------------------------------------


class FakeLaunchd:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def start(self, name: str) -> None:
        self.calls.append(("start", name))

    def stop(self, name: str) -> None:
        self.calls.append(("stop", name))


def _local_copy(argv: list[str], **_kwargs: object) -> None:
    """Stand in for rsync: copy the proxies the include filter would."""
    source, target = Path(argv[-2]), Path(argv[-1])
    for proxy in source.glob("*/*/proxy.*"):
        destination = target / proxy.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(proxy, destination)


def _install(tmp_path: Path) -> tuple[Layout, Path]:
    layout = Layout(home=tmp_path / "FrameFound", source=tmp_path / "src")
    data = layout.data
    (data / "derivatives" / "ab" / "abcd").mkdir(parents=True)
    (data / PROXY).write_bytes(b"proxy")
    (data / THUMB).write_bytes(b"thumb")
    envfile.write(layout.env_file, f"FRAMEFOUND_DATA_DIR={data}\nFRAMEFOUND_PROXY_DIR=\n")
    return layout, data


def test_moving_proxies_copies_marks_records_and_restarts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout, data = _install(tmp_path)
    nas = tmp_path / "nas" / "FrameFound Proxies"
    nas.parent.mkdir()
    monkeypatch.setattr(proxies.proc, "run", _local_copy)
    launchd, said = FakeLaunchd(), []

    proxies.move(layout, settings(data), nas, launchd, said.append)  # type: ignore[arg-type]

    assert (nas / PROXY).read_bytes() == b"proxy"
    assert not (nas / THUMB).exists()  # only proxies move
    assert (nas / PROXY_STORE_MARKER).is_file()
    assert envfile.read(layout.env_file)["FRAMEFOUND_PROXY_DIR"] == str(nas)
    # Nothing writes a proxy during the copy, and everything that reads the
    # setting is restarted after it.
    assert launchd.calls[0] == ("stop", "worker-media")
    assert ("start", "api") in launchd.calls and ("start", "worker-media") in launchd.calls
    # The originals of the move are left for the operator to remove.
    assert (data / PROXY).is_file()
    assert any("find" in line and "-delete" in line for line in said)


def test_a_move_that_did_not_copy_everything_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout, data = _install(tmp_path)
    nas = tmp_path / "nas"
    monkeypatch.setattr(proxies.proc, "run", lambda *a, **k: None)  # an rsync that copied nothing

    with pytest.raises(proxies.ProxyMoveError, match="0 of 1"):
        proxies.move(layout, settings(data), nas, FakeLaunchd(), lambda _m: None)  # type: ignore[arg-type]

    assert envfile.read(layout.env_file)["FRAMEFOUND_PROXY_DIR"] == ""
    assert not (nas / PROXY_STORE_MARKER).exists()


def test_proxies_are_never_moved_onto_a_drive_that_is_not_there(tmp_path: Path) -> None:
    """`/Volumes/Fast SSD/Proxies` with the SSD unplugged: creating the
    parents would put the "store" on the Mac's own disk."""
    layout, data = _install(tmp_path)
    with pytest.raises(proxies.ProxyMoveError, match="Connect the drive"):
        proxies.validate_target(tmp_path / "Fast SSD" / "Proxies", data)
    with pytest.raises(proxies.ProxyMoveError, match="full path"):
        proxies.validate_target(Path("Proxies"), data)


def test_a_store_that_is_away_cannot_be_copied_from(tmp_path: Path) -> None:
    layout, data = _install(tmp_path)
    old = tmp_path / "old-drive"
    old.mkdir()  # unplugged: folder there, marker not
    new = tmp_path / "new-drive"
    with pytest.raises(proxies.ProxyMoveError, match="--empty"):
        proxies.move(layout, settings(data, old), new, FakeLaunchd(), lambda _m: None)  # type: ignore[arg-type]
    # …but a new, empty store can be started in its place.
    proxies.start_empty(layout, settings(data, old), new, FakeLaunchd(), lambda _m: None)  # type: ignore[arg-type]
    assert envfile.read(layout.env_file)["FRAMEFOUND_PROXY_DIR"] == str(new)
    assert (new / PROXY_STORE_MARKER).is_file()


def test_moving_back_into_the_data_store_clears_the_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout, data = _install(tmp_path)
    nas = tmp_path / "nas"
    stores.mark_proxy_store(nas)
    (nas / "derivatives" / "cd" / "cdef").mkdir(parents=True)
    (nas / "derivatives" / "cd" / "cdef" / "proxy.mp4").write_bytes(b"p2")
    monkeypatch.setattr(proxies.proc, "run", _local_copy)

    proxies.move(layout, settings(data, nas), data, FakeLaunchd(), lambda _m: None)  # type: ignore[arg-type]

    assert envfile.read(layout.env_file)["FRAMEFOUND_PROXY_DIR"] == ""
    assert (data / "derivatives" / "cd" / "cdef" / "proxy.mp4").is_file()
    assert not (data / PROXY_STORE_MARKER).exists()


def test_the_copy_takes_only_proxies_and_can_resume(tmp_path: Path) -> None:
    argv = proxies.copy_command("rsync", tmp_path / "a", tmp_path / "b")
    assert argv[0] == "rsync" and "--partial" in argv
    # Order matters to rsync: directories, then proxies, then nothing else.
    assert argv.index("--include=*/") < argv.index("--include=proxy.*") < argv.index("--exclude=*")
    assert argv[-2].endswith("/derivatives/") and argv[-1].endswith("/derivatives/")


# --- what the catalogue lists but the store does not hold ------------------


@pytest.fixture()
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'c.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _video_with_proxy(db: AsyncSession, name: str) -> Derivative:
    library = Library(name=f"L-{name}", root_path="/media/x")
    db.add(library)
    await db.flush()
    asset = Asset(
        library_id=library.id,
        relative_path=f"{name}.mp4",
        filename=f"{name}.mp4",
        extension="mp4",
        media_type="video",
        size_bytes=1,
        mtime=datetime.now(UTC),
    )
    db.add(asset)
    await db.flush()
    record = Derivative(
        asset_id=asset.id,
        kind="proxy",
        relative_path=deriv.derivative_relpath(asset.id, "proxy", "mp4"),
        media_format="mp4",
        status="ready",
    )
    db.add(record)
    await db.commit()
    return record


async def test_missing_proxies_are_found_in_the_store_not_the_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db: AsyncSession
) -> None:
    data, nas = tmp_path / "data", tmp_path / "nas"
    stores.mark_proxy_store(nas)
    use(monkeypatch, data, nas)
    kept = await _video_with_proxy(db, "kept")
    lost = await _video_with_proxy(db, "lost")
    (nas / kept.relative_path).parent.mkdir(parents=True)
    (nas / kept.relative_path).write_bytes(b"p")

    total, absent = await proxy_store.missing(db, data)

    assert total == 2
    assert absent == [lost.asset_id]


async def test_an_unplugged_store_is_never_read_as_everything_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db: AsyncSession
) -> None:
    """Otherwise one command with the drive unplugged queues every proxy in
    the catalogue to be transcoded again."""
    data, nas = tmp_path / "data", tmp_path / "nas"
    nas.mkdir()
    use(monkeypatch, data, nas)
    await _video_with_proxy(db, "fine")

    with pytest.raises(proxy_store.StoreAway):
        await proxy_store.missing(db, data)


# --- the System page -----------------------------------------------------------


def test_the_system_page_does_not_measure_an_unplugged_proxy_store(tmp_path: Path) -> None:
    """`disk_usage` on the empty folder a missing drive leaves behind would
    report the Mac's own disk as a healthy proxy store."""
    from framefound.api.v1.system import _probe_proxy_store

    reason, usage = _probe_proxy_store(tmp_path)
    assert usage is None and "not connected" in (reason or "")
    stores.mark_proxy_store(tmp_path)
    reason, usage = _probe_proxy_store(tmp_path)
    assert reason is None and usage.total > 0
