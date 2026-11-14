"""The model library (local_models/library.py): downloading into it, copying a
model to this machine, removing, and the library as a kind of bulk storage
that can be away or moved.

Hugging Face is `fake.FakeHub` with its models shrunk to a few kilobytes of
true bytes; the NAS is a folder under `tmp_path`, connected when it carries
its marker. Nothing here touches a network or starts a process."""

import errno
import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from framefound.config import Settings, get_settings
from framefound.db.models import LocalModel
from framefound.local_models import fake, files, library, registry
from framefound.local_models.hub import HubError, Stopped
from framefound.native import doctor
from framefound.native import storage as storage_command
from framefound.storage import move_job, moves, stores

FIRST = "mlx-community/gemma-4-e4b-it-4bit"
SECOND = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
WEIGHTS = "model.safetensors"
QUICK: dict[str, Any] = {"settle_s": 0.0, "sleep": lambda _seconds: None}

_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_BACKUP_COPY_DIR",
    "FRAMEFOUND_MODELS_DIR",
    "FRAMEFOUND_DATA_DIR",
    "FRAMEFOUND_MIN_FREE_GB",
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _OURS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    get_settings.cache_clear()
    stores._CACHE.clear()


class Site:
    """A machine: its data folder, its models folder (with an ONNX cache in
    it that must never be touched), and a share at `nas`."""

    def __init__(self, tmp_path: Path) -> None:
        self.data = tmp_path / "data"
        self.models = tmp_path / "models"
        self.nas = tmp_path / "nas"
        for folder in (self.data, self.models, self.nas):
            folder.mkdir()
        self.clip = put(self.models, "clip/model.onnx", b"not ours")

    def settings(self, **more: Any) -> Settings:
        return Settings(data_dir=self.data, models_dir=self.models, min_free_gb=0, **more)

    def on_the_nas(self) -> Settings:
        """The library in the bulk folder on the share, which is connected."""
        stores.mark_bulk_root(self.nas)
        return self.settings(bulk_dir=self.nas)


@pytest.fixture()
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


def put(root: Path, relative: str, content: bytes = b"content") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def names(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def hub_with(*repos: str) -> fake.FakeHub:
    hub = fake.FakeHub.from_fixtures(sleep=lambda _seconds: None)
    for repo in repos:
        hub.shrink(repo)
    return hub


def model_of(
    hub: fake.FakeHub, repo: str = FIRST, *, runtime: str = "mlx-vlm", demo: bool = False
) -> library.ModelFiles:
    info = hub.info(repo)
    return library.ModelFiles(
        id=uuid.uuid4(),
        repo_id=info.repo_id,
        revision=info.revision,
        variant="",
        runtime=runtime,
        files=files.plan_files(info.files).keep,
        demo=demo,
    )


def message(raised: pytest.ExceptionInfo[BaseException]) -> str:
    return str(raised.value)


# --- downloading ---------------------------------------------------------------------


def test_a_model_is_installed_only_when_its_manifest_is_written(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    folder = library.library_dir(model, settings)
    publisher, name = FIRST.split("/")
    assert folder == site.models / "library" / publisher / name / model.revision

    def disk_full(_folder: Path, _manifest: files.Manifest) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(library, "write_manifest", disk_full)
    with pytest.raises(library.LibraryProblem, match="No space left"):
        library.download(hub, model, settings=settings)

    # Every file is there, checked, under its real name, and still it is
    # not a model: nothing says the set is whole.
    assert names(folder) == sorted(file.path for file in model.files)
    assert library.presence(model, settings) == library.Presence(False, False)
    assert files.folder_problem(folder) is not None

    monkeypatch.undo()
    phases: list[tuple[str, int, int]] = []
    library.download(hub, model, settings=settings, report=lambda *seen: phases.append(seen))

    manifest = files.read_manifest(folder)
    assert manifest is not None and manifest.repo_id == FIRST
    assert manifest.revision == model.revision and manifest.files == model.files
    assert files.folder_problem(folder) is None
    assert library.presence(model, settings) == library.Presence(True, True)
    total = sum(file.size for file in model.files)
    assert phases[-1] == ("downloading", total, total)
    # The second run fetched nothing: every file was in the library already.
    assert hub.fetches == len(model.files)
    # The download's scratch is gone once its files are in place.
    assert not library.scratch_dir(model.id, settings).exists()


def test_a_file_with_the_wrong_hash_is_thrown_away_and_named(site: Site) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    hub.lie_about(WEIGHTS)

    with pytest.raises(library.LibraryProblem) as refused:
        library.download(hub, model, settings=settings)

    assert message(refused).startswith(f"{WEIGHTS} is not the file Hugging Face lists")
    assert "Resume fetches it again" in message(refused) and refused.value.resumable
    folder = library.library_dir(model, settings)
    assert not (folder / WEIGHTS).exists()  # never under its real name
    assert not (library.scratch_dir(model.id, settings) / f"{WEIGHTS}.partial").exists()
    assert library.presence(model, settings).in_library is False

    library.download(hub, model, settings=settings)  # Resume: the lie was told once
    weights = next(file for file in model.files if file.path == WEIGHTS)
    assert files.check_file(folder / WEIGHTS, weights) is None
    assert files.folder_problem(folder) is None


def test_a_cancelled_download_keeps_what_it_has_and_resumes(site: Site) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    partial = library.scratch_dir(model.id, settings) / f"{WEIGHTS}.partial"

    asked = 0

    def cancel_half_way() -> bool:
        # Asked before each piece: yes once the weights' first piece is in.
        nonlocal asked
        asked += partial.exists()
        return asked >= 2

    with pytest.raises(Stopped):
        library.download(hub, model, settings=settings, should_stop=cancel_half_way)

    kept = partial.stat().st_size
    assert 0 < kept < next(file.size for file in model.files if file.path == WEIGHTS)
    assert library.presence(model, settings).in_library is False
    before = len(hub.calls)

    library.download(hub, model, settings=settings)

    resumed = [call for call in hub.calls[before:] if call[0] == "fetch"]
    # Only what was not already in the library is asked for, and the
    # weights from where they stopped.
    assert ("fetch", FIRST, model.revision, WEIGHTS, kept) in resumed
    assert library.presence(model, settings).in_library is True


def test_a_dropped_connection_keeps_what_arrived_and_resumes(site: Site) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    hub.fail_after(sum(file.size for file in model.files) // 2)

    with pytest.raises(HubError) as dropped:
        library.download(hub, model, settings=settings)

    assert dropped.value.problem == "unreachable"
    library.download(hub, model, settings=settings)
    assert files.folder_problem(library.library_dir(model, settings)) is None


def kept_bytes(model: library.ModelFiles, settings: Settings) -> int:
    """What a download has on disk: its files in the library and its parts in scratch."""
    folders = (library.library_dir(model, settings), library.scratch_dir(model.id, settings))
    return sum(
        path.stat().st_size
        for folder in folders
        if folder.is_dir()
        for path in folder.rglob("*")
        if path.is_file()
    )


def before_the_weights(model: library.ModelFiles) -> int:
    """The bytes of the files that are fetched before the weights."""
    paths = [file.path for file in model.files]
    return sum(file.size for file in model.files[: paths.index(WEIGHTS)])


def test_a_dropped_download_last_reports_what_had_arrived(site: Site) -> None:
    """The job writes "The connection dropped at X of Y GB" from the last
    report. Reports come at most every REPORT_EVERY_S, and this download is
    over in far less: only a report made as it fails can say how far it got.
    Nothing here waits for one."""
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    total = sum(file.size for file in model.files)
    weights = next(file.size for file in model.files if file.path == WEIGHTS)
    arrived = before_the_weights(model) + weights // 2
    hub.fail_after(arrived)
    said: list[tuple[str, int, int]] = []

    with pytest.raises(HubError):
        library.download(hub, model, settings=settings, report=lambda *seen: said.append(seen))

    # Half of the weights is on disk, and that is the figure the job is left with.
    partial = library.scratch_dir(model.id, settings) / f"{WEIGHTS}.partial"
    assert partial.stat().st_size == weights // 2 > 0
    assert kept_bytes(model, settings) == arrived < total
    assert said[-1] == ("downloading", arrived, total)

    # Resume counts on from there, and ends whole.
    resumed: list[tuple[str, int, int]] = []
    library.download(hub, model, settings=settings, report=lambda *seen: resumed.append(seen))
    assert resumed[0] == ("downloading", arrived, total)
    assert resumed[-1] == ("downloading", total, total)


def test_a_cancelled_download_last_reports_what_it_kept(site: Site) -> None:
    """ "Cancelled. Resume carries on from X GB." is written from the last report too."""
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    total = sum(file.size for file in model.files)
    partial = library.scratch_dir(model.id, settings) / f"{WEIGHTS}.partial"
    asked = 0

    def cancel_half_way() -> bool:
        # Asked before each piece: yes once the weights' first piece is in.
        nonlocal asked
        asked += partial.exists()
        return asked >= 2

    said: list[tuple[str, int, int]] = []
    with pytest.raises(Stopped):
        library.download(
            hub,
            model,
            settings=settings,
            report=lambda *seen: said.append(seen),
            should_stop=cancel_half_way,
        )

    kept = kept_bytes(model, settings)
    assert kept == before_the_weights(model) + partial.stat().st_size
    assert partial.stat().st_size > 0 and kept < total
    assert said[-1] == ("downloading", kept, total)


def test_a_refused_file_is_not_counted_in_the_last_report(site: Site) -> None:
    """A file that fails its check is thrown away: the figure the job is left
    with must not still include it."""
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    total = sum(file.size for file in model.files)
    hub.lie_about(WEIGHTS)
    said: list[tuple[str, int, int]] = []

    with pytest.raises(library.LibraryProblem):
        library.download(hub, model, settings=settings, report=lambda *seen: said.append(seen))

    kept = kept_bytes(model, settings)
    assert kept == before_the_weights(model) < total
    assert said[-1] == ("downloading", kept, total)


def test_a_partial_longer_than_the_file_is_started_again(site: Site) -> None:
    """Asked for "the rest" of a file that already holds more than the file,
    the server says there is none, and the check would refuse it for ever."""
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    put(library.scratch_dir(model.id, settings), "config.json.partial", b"x" * 100_000)

    library.download(hub, model, settings=settings)

    assert files.folder_problem(library.library_dir(model, settings)) is None


def test_a_settings_file_that_asks_for_code_is_refused_as_it_is_on_disk(site: Site) -> None:
    """The page said nothing; the file itself names a Python file to run."""
    settings = site.settings()
    hub = fake.FakeHub.from_fixtures(sleep=lambda _seconds: None)
    hub.contents[(FIRST, "config.json")] = json.dumps({"model_file": "run_me.py"}).encode()
    hub.shrink(FIRST)
    model = model_of(hub)

    with pytest.raises(library.LibraryProblem) as refused:
        library.download(hub, model, settings=settings)

    assert message(refused) == files.NEEDS_CODE and not refused.value.resumable
    assert files.read_manifest(library.library_dir(model, settings)) is None


def test_a_download_that_will_not_fit_is_refused_before_it_starts(site: Site) -> None:
    settings = site.settings().model_copy(update={"min_free_gb": 10.0**9})
    hub = hub_with(FIRST)
    model = model_of(hub)

    with pytest.raises(library.LibraryProblem) as refused:
        library.download(hub, model, settings=settings)

    assert "There is not enough room on this machine's own disk" in message(refused)
    assert "free" in message(refused) and hub.fetches == 0


def test_a_download_waits_out_a_move_of_the_library(site: Site) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    stores.pause(site.data, "models")
    waited: list[float] = []

    def the_move_finishes(seconds: float) -> None:
        waited.append(seconds)
        stores.resume(site.data, "models")

    library.download(hub, model, settings=settings, sleep=the_move_finishes)

    assert waited == [library.MOVE_POLL_S]
    assert library.presence(model, settings).in_library is True

    # A pause that never lifts is given up on, with what arrived kept.
    other = model_of(hub_with(SECOND), SECOND)
    stores.pause(site.data, "models")
    with pytest.raises(library.LibraryProblem) as refused:
        library.download(hub_with(SECOND), other, settings=settings, sleep=lambda _s: None)
    assert "being moved" in message(refused) and library.KEPT in message(refused)


def test_a_download_that_waits_out_a_move_carries_on_in_the_new_library(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move reaches its last pass after the first file is in place, copies
    it, and switches the library to the share while the download waits. Every
    later file, and the manifest, must go to the new library: the old folder
    is now "old copies", which Remove old copies may empty."""
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    old = library.library_dir(model, settings)
    share = site.nas / "models"
    real_put = stores.put_in_place
    placed: list[Path] = []

    def put_then_the_move_pauses(finished: Path, destination: Path) -> None:
        real_put(finished, destination)
        placed.append(destination)
        if len(placed) == 1:
            stores.pause(site.data, "models")

    def the_move_copies_and_switches(_seconds: float) -> None:
        shutil.copytree(site.models / "library", share / "library")
        stores.mark_store(share, stores.BULK_STORE_MARKER, kind="models")
        stores.switch(site.data, "models", share, previous=site.models)  # lifts the pause

    monkeypatch.setattr(stores, "put_in_place", put_then_the_move_pauses)
    library.download(hub, model, settings=settings, sleep=the_move_copies_and_switches)

    new = library.library_dir(model, settings)
    assert new.is_relative_to(share)
    assert names(new) == sorted([*(file.path for file in model.files), files.MANIFEST])
    assert files.folder_problem(new) is None
    assert library.presence(model, settings).in_library is True
    # The old folder holds only what was there before the switch, and is no model.
    assert names(old) == [placed[0].relative_to(old).as_posix()]
    assert files.read_manifest(old) is None


def test_a_full_disk_here_is_a_sentence_not_a_crash(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This machine's own disk fills while a download is fetched, or while a
    role's copy is written: the lane gets a LibraryProblem it can show and
    resume, never a bare OSError, and nothing reads as whole."""
    settings = site.on_the_nas()
    hub = hub_with(FIRST)
    model = model_of(hub)
    real_fetch = hub.fetch

    def fills_the_disk(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(hub, "fetch", fills_the_disk)
    with pytest.raises(library.LibraryProblem) as fetching:
        library.download(hub, model, settings=settings)
    assert "this machine's disk" in message(fetching) and "No space left" in message(fetching)
    assert library.KEPT in message(fetching) and fetching.value.resumable

    monkeypatch.setattr(hub, "fetch", real_fetch)
    library.download(hub, model, settings=settings)
    here = library.local_dir(model, settings)
    real_manifest = library.write_manifest

    def full_here(folder: Path, manifest: files.Manifest) -> None:
        if folder.is_relative_to(site.models):
            raise OSError(errno.ENOSPC, "No space left on device")
        real_manifest(folder, manifest)

    monkeypatch.setattr(library, "write_manifest", full_here)
    with pytest.raises(library.LibraryProblem) as copying:
        library.place(model, settings=settings)
    assert "this machine's disk" in message(copying) and library.ROLE_STAYS in message(copying)
    assert "model library could not be read" not in message(copying)
    assert library.presence(model, settings) == library.Presence(True, False)

    monkeypatch.undo()
    # Writing the copy itself: also this machine's, not the library's.
    real_open = Path.open

    def no_room(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if "w" in mode and path.is_relative_to(here):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_room)
    with pytest.raises(library.LibraryProblem) as writing:
        library.place(model, settings=settings)
    assert "this machine's disk" in message(writing) and writing.value.resumable
    assert library.presence(model, settings).on_this_machine is False


def test_nothing_is_written_to_a_library_that_is_away(site: Site) -> None:
    settings = site.settings(bulk_dir=site.nas)  # the share is not mounted: no marker
    hub = hub_with(FIRST)
    model = model_of(hub)

    with pytest.raises(library.LibraryProblem) as refused:
        library.download(hub, model, settings=settings)

    assert message(refused) == f"{stores.away('models', settings)} {library.KEPT}"
    assert str(site.nas) not in message(refused)  # for anyone signed in: no paths
    assert list(site.nas.iterdir()) == [] and hub.fetches == 0
    assert not library.scratch_dir(model.id, settings).exists()


def test_the_demo_download_reads_as_whole_with_its_small_files(site: Site) -> None:
    """The demo's hub writes a few kilobytes against gigabytes listed; the
    demo's library must still read as whole, and nothing the size of the
    real model is asked of the disk."""
    settings = site.settings().model_copy(update={"min_free_gb": 10.0**9})
    hub = fake.FakeHub.from_fixtures(sleep=lambda _seconds: None)
    hub.small_files(64)
    model = model_of(hub, runtime="demo", demo=True)

    library.download(hub, model, settings=settings)

    folder = library.library_dir(model, settings)
    manifest = files.read_manifest(folder)
    assert manifest is not None and manifest.demo is True
    assert files.folder_problem(folder) is None
    assert library.presence(model, settings).in_library is True


# --- this machine's copy -----------------------------------------------------------


def test_giving_a_role_copies_the_model_to_this_machine_and_checks_it(site: Site) -> None:
    settings = site.on_the_nas()
    hub = hub_with(FIRST)
    model = model_of(hub)
    library.download(hub, model, settings=settings)
    in_library = library.library_dir(model, settings)
    assert in_library.is_relative_to(site.nas / "models" / "library")
    assert library.copies_to_this_machine(settings)
    assert library.presence(model, settings) == library.Presence(True, False)
    with pytest.raises(library.LibraryProblem, match="Copy it again"):
        library.serving_dir(model, need_local=True, settings=settings)
    # A Test of a model with no role may load it from the library.
    assert library.serving_dir(model, need_local=False, settings=settings) == in_library

    phases: list[str] = []
    library.place(model, settings=settings, report=lambda phase, *_n: phases.append(phase))

    here = library.local_dir(model, settings)
    assert here.is_relative_to(site.models / "local-ai")
    assert names(here) == names(in_library)
    assert all((here / p).read_bytes() == (in_library / p).read_bytes() for p in names(here))
    assert files.folder_problem(here) is None and phases and set(phases) == {"copying"}
    assert library.presence(model, settings) == library.Presence(True, True)
    assert library.serving_dir(model, need_local=True, settings=settings) == here

    # The copy here loses a file ("Copy again"), and the library's copy is
    # damaged (same size, other bytes): the copy is checked and refused, and
    # the folder here no longer claims to be whole at all.
    (here / WEIGHTS).unlink()
    assert library.presence(model, settings).on_this_machine is False
    weights = in_library / WEIGHTS
    weights.write_bytes(bytes(byte ^ 0xFF for byte in weights.read_bytes()))
    with pytest.raises(library.LibraryProblem) as refused:
        library.place(model, settings=settings)
    assert message(refused).startswith(f"{WEIGHTS} is not the file Hugging Face lists")
    assert library.presence(model, settings).on_this_machine is False
    assert not (here / files.MANIFEST).exists()
    assert not (here / WEIGHTS).exists() and not (here / f"{WEIGHTS}.partial").exists()
    assert library.drop_local(model, settings) > 0 and not here.exists()


def test_a_library_kept_inside_this_machines_copies_is_never_dropped_as_a_copy(
    site: Site,
) -> None:
    """Somebody pointed the library at the folder the copies live in. Dropping
    a copy must then never delete from it."""
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    stores.set_override(site.data, "models", site.models / "local-ai")
    stores.mark_store(site.models / "local-ai", stores.BULK_STORE_MARKER, kind="models")
    library.download(hub, model, settings=settings)
    held = names(site.models / "local-ai")

    assert library.drop_local(model, settings) == 0
    assert names(site.models / "local-ai") == held and held


def test_no_copy_is_made_when_the_library_is_on_this_machine(site: Site) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)

    with pytest.raises(library.LibraryProblem) as nothing:
        library.place(model, settings=settings)  # not downloaded yet
    assert message(nothing) == library.NOT_IN_LIBRARY and not nothing.value.resumable

    library.download(hub, model, settings=settings)
    library.place(model, settings=settings)

    assert not library.copies_to_this_machine(settings)
    assert library.local_dir(model, settings) == library.library_dir(model, settings)
    assert not (site.models / "local-ai").exists()
    assert library.presence(model, settings) == library.Presence(True, True)
    # "Drop the copy on this machine" never means the library.
    assert library.drop_local(model, settings) == 0
    assert files.folder_problem(library.library_dir(model, settings)) is None


# --- removing ------------------------------------------------------------------------


def test_removing_never_touches_anything_outside_the_models_own_folder(site: Site) -> None:
    settings = site.on_the_nas()
    hub = hub_with(FIRST, SECOND)
    model, other = model_of(hub), model_of(hub, SECOND)
    for each in (model, other):
        library.download(hub, each, settings=settings)
        library.place(each, settings=settings)
    folder = library.library_dir(model, settings)
    stray = put(folder, "notes.txt", b"the operator's own")
    beside = put(folder.parent, "README-by-hand.txt")
    half = put(library.scratch_dir(model.id, settings), "leftover.json.partial")
    root_file = put(library.library_root(settings), "index.txt")

    freed = library.remove(model, settings)

    assert freed >= sum(file.size for file in model.files) * 2  # the library and the copy
    assert names(folder) == ["notes.txt"] and stray.read_bytes() == b"the operator's own"
    assert beside.is_file() and root_file.is_file() and half.is_file()  # not its to know
    assert not library.local_dir(model, settings).exists()
    assert site.clip.read_bytes() == b"not ours"
    # The other model is whole, in both places.
    assert library.presence(other, settings) == library.Presence(True, True)
    assert library.presence(model, settings) == library.Presence(False, False)


def test_a_partial_download_is_removed_with_its_model(site: Site) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    partial = library.scratch_dir(model.id, settings) / f"{WEIGHTS}.partial"
    with pytest.raises(Stopped):
        library.download(hub, model, settings=settings, should_stop=lambda: partial.exists())

    library.remove(model, settings)

    assert not library.scratch_dir(model.id, settings).exists()
    assert library.scratch_root(settings).is_dir()  # only this model's own folder goes
    assert not library.library_dir(model, settings).exists()
    assert site.clip.is_file() and (site.models / "library").is_dir()


def test_a_name_that_could_leave_the_library_is_never_a_folder(site: Site) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    good = model_of(hub)
    for repo_id, revision, variant in (
        ("../escape", good.revision, ""),
        ("publisher/name/deeper", good.revision, ""),
        ("publisher/..", good.revision, ""),
        (FIRST, "..", ""),
        (FIRST, good.revision, "../../clip"),
    ):
        bad = library.ModelFiles(
            id=good.id,
            repo_id=repo_id,
            revision=revision,
            variant=variant,
            runtime="mlx-vlm",
            files=good.files,
        )
        with pytest.raises(library.LibraryProblem, match="folder name"):
            library.library_dir(bad, settings)
        with pytest.raises(library.LibraryProblem, match="folder name"):
            library.remove(bad, settings)
        assert library.presence(bad, settings) == library.Presence(False, False)
    assert site.clip.is_file()


# --- the library as a kind of bulk storage -----------------------------------------


def test_a_library_on_a_share_that_is_away_pauses_only_what_lives_there(site: Site) -> None:
    """A model with a role keeps working from this machine; adding, copying
    and removing wait, each saying why; every other kind carries on."""
    settings = site.on_the_nas()
    hub = hub_with(FIRST, SECOND)
    looker, other = model_of(hub), model_of(hub, SECOND)
    library.download(hub, looker, settings=settings)
    library.place(looker, settings=settings)
    library.download(hub, other, settings=settings)
    (site.nas / "models" / stores.BULK_STORE_MARKER).unlink()  # the library's share drops
    gone = stores.away("models", settings)
    assert gone is not None and "model library is not connected" in gone

    # What lives on this machine keeps working.
    assert library.presence(looker, settings) == library.Presence(None, True)
    here = library.serving_dir(looker, need_local=True, settings=settings)
    assert here == library.local_dir(looker, settings)
    # What lives only in the library waits, and says so — without paths.
    assert library.presence(other, settings) == library.Presence(None, False)
    with pytest.raises(library.LibraryProblem) as test_it:
        library.serving_dir(other, need_local=False, settings=settings)
    assert message(test_it) == f"{gone} {library.ONLY_IN_LIBRARY}"
    third = model_of(hub_with("mlx-community/demo-large-4bit"), "mlx-community/demo-large-4bit")
    for attempt, then in (
        (lambda: library.download(hub, third, settings=settings), library.KEPT),
        (lambda: library.place(other, settings=settings), library.ROLE_STAYS),
        (lambda: library.remove(other, settings), library.NOTHING_REMOVED),
    ):
        with pytest.raises(library.LibraryProblem) as waits:
            attempt()
        assert message(waits) == f"{gone} {then}" and waits.value.resumable
    assert library.free_bytes(settings) is None
    assert names(library.library_dir(other, settings))  # nothing removed

    # Only the library waits: the kinds beside it on the same share carry on.
    for kind in ("proxies", "renders", "exports", "basemaps", "backups"):
        assert stores.write_problem(kind, settings) is None, kind
    lines = {check.name: check for check in doctor.check_stores(settings)}
    assert not lines["model library"].ok and not lines["model library"].fatal
    assert "not connected" in lines["model library"].detail
    assert lines["slideshow store"].ok

    stores.mark_store(site.nas / "models", stores.BULK_STORE_MARKER, kind="models")
    assert library.presence(other, settings) == library.Presence(True, False)


def test_a_library_that_cannot_be_read_is_not_taken_for_an_empty_one(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share that hangs answers with an error, not "no such file": the
    model may be sitting there whole, and must not be offered for download."""
    settings = site.on_the_nas()
    hub = hub_with(FIRST)
    model = model_of(hub)
    library.download(hub, model, settings=settings)
    library.place(model, settings=settings)
    real = library.read_manifest

    def stalled(folder: Path) -> files.Manifest | None:
        if folder.is_relative_to(site.nas):
            raise OSError(errno.EIO, "Input/output error")
        return real(folder)

    monkeypatch.setattr(library, "read_manifest", stalled)

    assert library.presence(model, settings) == library.Presence(None, True)
    library.drop_local(model, settings)
    with pytest.raises(library.LibraryProblem, match="could not be read or written") as asked:
        library.serving_dir(model, need_local=False, settings=settings)
    assert "Input/output error" in message(asked) and asked.value.resumable
    with pytest.raises(library.LibraryProblem, match=library.ROLE_STAYS):
        library.place(model, settings=settings)


def test_naming_a_bulk_folder_leaves_an_existing_library_where_it_is(site: Site) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    library.download(hub, model, settings=settings)
    root = site.nas / "Framefound"

    staying = moves.adopt_bulk_root(root, settings=settings)

    assert "models" in staying
    assert stores.kind_root("models", settings) == site.models
    assert library.library_root(settings) == site.models / "library"
    assert library.presence(model, settings) == library.Presence(True, True)
    assert not library.copies_to_this_machine(settings)


def test_an_install_with_no_library_yet_starts_it_in_the_bulk_folder(site: Site) -> None:
    """The ONNX and Whisper caches in the models folder are not the library."""
    settings = site.settings()
    put(site.models, "library/google/emptied/.keep-not")  # a folder, not a model
    (site.models / "library/google/emptied/.keep-not").unlink()
    root = site.nas / "Framefound"

    staying = moves.adopt_bulk_root(root, settings=settings)

    assert "models" not in staying
    assert library.library_root(settings) == root / "models" / "library"
    assert library.copies_to_this_machine(settings)
    assert site.clip.read_bytes() == b"not ours"


def test_the_library_moves_like_any_other_kind(site: Site) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    model = model_of(hub)
    library.download(hub, model, settings=settings)
    old = library.library_dir(model, settings)
    target = site.nas / "Models"

    moved = moves.move("models", target, settings=settings, **QUICK)

    assert moved.files == len(model.files) + 1  # and its manifest
    assert library.library_root(settings) == target / "library"
    assert library.presence(model, settings) == library.Presence(True, False)
    assert not (target / "clip").exists()  # the caches beside the library stay put
    library.place(model, settings=settings)
    assert library.presence(model, settings) == library.Presence(True, True)

    # The old copies are offered for removal, and only the library's files go.
    assert stores.read_state(site.data).previous["models"] == site.models
    removed = moves.remove_old_copies("models", settings=settings)
    assert (removed.files, removed.kept) == (len(model.files) + 1, 0)
    assert not old.exists() and site.clip.read_bytes() == b"not ours"


def test_moving_the_library_off_this_machine_keeps_each_role_model_here(site: Site) -> None:
    """While the library was here a role model ran from it and nothing was
    copied. Once it is on the NAS a role model runs from its copy here, so
    the move gives it one before switching, and removing the old copies
    afterwards leaves that copy whole."""
    settings = site.settings()
    hub = hub_with(FIRST, SECOND)
    looker, other = model_of(hub), model_of(hub, SECOND)
    for each in (looker, other):
        library.download(hub, each, settings=settings)
    assert library.serving_dir(looker, need_local=True, settings=settings) == (
        library.library_dir(looker, settings)
    )
    said: list[str] = []

    storage_command.move(
        settings,
        "models",
        site.nas / "Models",
        said.append,
        sure=lambda _question: True,
        ask=lambda _settings, _folder: storage_command.Asked(0, None),
        roles=lambda _settings: [looker],
        **QUICK,
    )

    here = library.local_dir(looker, settings)
    assert here.is_relative_to(site.models / "local-ai")
    assert library.serving_dir(looker, need_local=True, settings=settings) == here
    # A model with no role is only in the library, as anywhere else.
    assert library.presence(other, settings) == library.Presence(True, False)
    assert not library.local_dir(other, settings).exists()

    removed = moves.remove_old_copies("models", settings=settings)

    assert removed.kept == 0 and not (site.models / "library" / "mlx-community").exists()
    assert files.folder_problem(here) is None
    assert library.presence(looker, settings) == library.Presence(True, True)
    (site.nas / "Models" / stores.BULK_STORE_MARKER).unlink()  # and the NAS goes away
    assert library.serving_dir(looker, need_local=True, settings=settings) == here


def test_a_role_model_that_cannot_be_kept_here_stops_the_move(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = site.settings()
    hub = hub_with(FIRST)
    looker = model_of(hub)
    library.download(hub, looker, settings=settings)
    real_manifest = library.write_manifest

    def full_here(folder: Path, manifest: files.Manifest) -> None:
        if folder.is_relative_to(site.models / "local-ai"):
            raise OSError(errno.ENOSPC, "No space left on device")
        real_manifest(folder, manifest)

    monkeypatch.setattr(library, "write_manifest", full_here)
    with pytest.raises(moves.MoveError) as stopped:
        moves.move(
            "models",
            site.nas / "Models",
            settings=settings,
            before_switch=lambda: move_job.keep_roles_here([looker], settings),
            **QUICK,
        )

    assert "was not moved" in message(stopped) and "No space left" in message(stopped)
    assert stores.kind_root("models", settings) == site.models
    assert not stores.read_state(site.data).is_paused("models")
    assert library.serving_dir(looker, need_local=True, settings=settings) == (
        library.library_dir(looker, settings)
    )
    # The half-made copy does not read as whole.
    publisher, name = FIRST.split("/")
    half = site.models / "local-ai" / publisher / name / looker.revision
    assert files.read_manifest(half) is None


# --- the row ---------------------------------------------------------------------------


def test_a_row_becomes_a_snapshot_a_thread_can_hold() -> None:
    hub = hub_with(FIRST)
    model = model_of(hub)
    row = LocalModel(
        id=model.id,
        repo_id=model.repo_id,
        revision=model.revision,
        variant="",
        runtime="mlx-vlm",
        files=[
            {"path": f.path, "size": f.size, "sha256": f.sha256, "blob_id": f.blob_id}
            for f in model.files
        ],
    )

    assert registry.snapshot(row) == model
    row.runtime = "demo"
    assert registry.snapshot(row).demo is True
    row.files = [{"path": "../climbs", "size": 1}]
    with pytest.raises(ValueError, match="cannot be read"):
        registry.snapshot(row)


def test_the_library_never_imports_a_database_or_a_queue() -> None:
    """The model server reads the library too, and has neither."""
    source = Path(library.__file__).read_text(encoding="utf-8")
    for module in ("framefound.db", "sqlalchemy", "celery", "framefound.processing"):
        assert module not in source, module
