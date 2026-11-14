"""Catalogue backups for a native install.

The same archive `manage.sh backup` and the containers' backup service
write — `catalog.dump`, `manifest.json`, `checksums.sha256`, the settings
file as `env` — so an archive made on either kind of install restores on the
other, and the System page's "newest backup" check reads these unchanged.

Archives land in `<data>/backups`. That is the same disk as the database on
a one-disk Mac, so a second copy goes off the machine: to
`FRAMEFOUND_BACKUP_COPY_DIR` when that is set, otherwise to the bulk
folder's `backups` when there is a bulk folder (storage/stores.py). A backup
that only exists beside what it backs up is half a backup. Each archive
holds the settings file, and with it the key that unseals every stored
secret: written 600 here, and worth remembering on the NAS.

It also holds the record of where the bulky kinds live
(`<data>/.framefound-stores.json`, as `stores.json`). The data folder is
otherwise rebuildable, and that record is the one thing in it that is not:
a bulk folder named on the Storage page and every move since exist nowhere
else. A catalogue restored without it says every proxy and slideshow video
is ready while FrameFound looks for them on this Mac's own disk.
"""

import contextlib
import hashlib
import json
import shutil
import tarfile
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from framefound import __version__
from framefound.config import Settings
from framefound.native import database, proc
from framefound.native.layout import Layout
from framefound.storage import stores

MAX_AGE_S = 24 * 3600
PATTERN = "framefound-*.tar.gz"
# The stores file's name inside an archive: not hidden, so it is seen when
# an archive is opened by hand.
STORES_IN_ARCHIVE = "stores.json"


class BackupError(RuntimeError):
    pass


def folder(env: dict[str, str], layout: Layout) -> Path:
    return Path(env.get("FRAMEFOUND_DATA_DIR") or layout.data) / "backups"


def _storage(env: dict[str, str], layout: Layout) -> Settings:
    """The storage settings as the install's file gives them. This module
    works from that file, not from the environment of whatever process
    happens to call it, so the values are passed in rather than read."""

    def path(key: str) -> Path | None:
        value = (env.get(key) or "").strip()
        return Path(value) if value else None

    return Settings(
        data_dir=path("FRAMEFOUND_DATA_DIR") or layout.data,
        bulk_dir=path("FRAMEFOUND_BULK_DIR"),
        backup_copy_dir=path("FRAMEFOUND_BACKUP_COPY_DIR"),
    )


def copy_dir(env: dict[str, str], layout: Layout) -> Path | None:
    """Where each archive is copied off this machine, or None for nowhere:
    the `backups` kind of storage/stores.py."""
    return stores.kind_root("backups", _storage(env, layout))


def copy_problem(env: dict[str, str], layout: Layout) -> str | None:
    """Why the copy cannot be made right now, or None. For the bulk
    folder's `backups` that is the marker test every store gets: the empty
    folder an unmounted share leaves behind must not collect the "off-machine"
    copies on this Mac's own disk."""
    return stores.kind_problem("backups", _storage(env, layout))


def archives(directory: Path) -> list[Path]:
    """Newest first."""
    try:
        return sorted(directory.glob(PATTERN), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def newest_age_s(directory: Path, now: float | None = None) -> float | None:
    found = archives(directory)
    if not found:
        return None
    return (now if now is not None else time.time()) - found[0].stat().st_mtime


def is_due(directory: Path, now: float | None = None) -> bool:
    age = newest_age_s(directory, now)
    return age is None or age >= MAX_AGE_S


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest(schema: str | None, now: datetime) -> dict[str, object]:
    return {
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_version": __version__,
        "schema_version": schema or "unknown",
        "components": ["catalog_database", "configuration"],
        "excluded": ["thumbnails", "proxies", "transcript_sidecars", "model_weights"],
        "note": ("Derived media is rebuildable from originals; originals are never in backups."),
    }


def pack(work: Path, archive: Path) -> None:
    """Checksum `work`'s files and write them as `archive`, appearing only
    when complete — the health check never sees a half-written backup."""
    lines = [f"{_sha256(work / name)}  {name}\n" for name in ("catalog.dump", "manifest.json")]
    (work / "checksums.sha256").write_text("".join(lines))
    partial = archive.with_name(archive.name + ".partial")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(partial, "w:gz") as tar:
        for item in sorted(work.iterdir()):
            tar.add(item, arcname=f"./{item.name}")
    partial.chmod(0o600)
    partial.replace(archive)


def unpack(archive: Path, work: Path) -> Path:
    """Extract and verify `archive`; returns the dump inside."""
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(work, filter="data")  # refuses links and paths outside `work`
    sums = work / "checksums.sha256"
    if not sums.is_file() or not (work / "catalog.dump").is_file():
        raise BackupError(f"{archive.name} is not a FrameFound backup archive")
    for line in sums.read_text().splitlines():
        expected, _, name = line.partition("  ")
        if name and _sha256(work / name) != expected:
            raise BackupError(f"{archive.name} is damaged: {name} does not match its checksum")
    return work / "catalog.dump"


def prune(directory: Path, keep: int) -> list[Path]:
    removed = archives(directory)[max(keep, 1) :]
    for old in removed:
        old.unlink(missing_ok=True)
    return removed


def run(layout: Layout, env: dict[str, str], *, if_due: bool = False) -> Path | None:
    """Take a backup. With `if_due`, only when the newest is a day old."""
    target = folder(env, layout)
    if if_due and not is_due(target):
        return None
    now = datetime.now(UTC)
    keep = int(env.get("FRAMEFOUND_BACKUP_KEEP") or 14)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        try:
            database.dump(env, work / "catalog.dump")
        except proc.CommandFailed as err:
            raise BackupError(f"The database could not be dumped: {err}") from err
        (work / "manifest.json").write_text(
            json.dumps(manifest(database.schema_version(env), now), indent=2) + "\n"
        )
        (work / "schema_version").write_text((database.schema_version(env) or "unknown") + "\n")
        shutil.copyfile(layout.env_file, work / "env")
        record = _storage(env, layout).data_dir / stores.STORES_FILE
        if record.is_file():
            shutil.copyfile(record, work / STORES_IN_ARCHIVE)
        archive = target / f"framefound-{now.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
        pack(work, archive)
    prune(target, keep)
    elsewhere = copy_dir(env, layout)
    if elsewhere is not None:
        copy_off_machine(archive, elsewhere, keep, away=copy_problem(env, layout))
    return archive


def copy_off_machine(archive: Path, directory: Path, keep: int, away: str | None = None) -> bool:
    """A second copy somewhere else. A share that is away is not an error —
    the next night's backup copies again — but it is said. `away` is why the
    folder cannot be used when the caller already knows (`copy_problem`)."""
    if away is not None:
        print(f"backup: {away} The off-machine copy was skipped")
        return False
    if not directory.is_dir():
        print(f"backup: {directory} is not reachable; the off-machine copy was skipped")
        return False
    # The backup on this machine is already written; a share that fills up
    # or drops mid-copy must not turn it into a failed backup. Say why, clear
    # the half copy, and let the next backup copy again.
    partial = directory / (archive.name + ".partial")
    try:
        shutil.copyfile(archive, partial)
        partial.replace(directory / archive.name)
    except OSError as err:
        print(
            f"backup: the copy to {directory} failed ({err}); "
            f"the backup is on this machine only: {archive}"
        )
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        return False
    try:
        prune(directory, keep)
    except OSError as err:
        print(f"backup: older copies in {directory} could not be removed ({err})")
    return True


def restore(
    layout: Layout,
    env: dict[str, str],
    archive: Path,
    say: Callable[[str], None] = lambda _message: None,
) -> None:
    """Replace the catalogue with `archive`'s. The caller stops the app first."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        database.restore(env, unpack(archive, work))
        restore_stores(_storage(env, layout).data_dir, work / STORES_IN_ARCHIVE, say)


def restore_stores(data_dir: Path, saved: Path, say: Callable[[str], None]) -> bool:
    """Put back the record of where the bulky kinds live, when this install
    has none. True when it was put back.

    Only into a data folder with no record of its own — a new Mac, a data
    folder made again. A record already here says where the files are
    *now*; an older one over the top of it would send every kind moved since
    back to a folder it has left. So that one is kept, and a difference is
    said rather than settled by guessing."""
    if not saved.is_file():
        return False
    try:
        raw = json.loads(saved.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("it is not a JSON object")
        # A pause belongs to a move that was running when the backup was
        # taken, which this is not.
        raw.pop("paused", None)
        raw.pop("paused_at", None)
        body = json.dumps(raw, indent=2) + "\n"
    except (OSError, ValueError) as err:
        say(f"The backup's record of where the bulky files live could not be read ({err}).")
        return False
    record = data_dir / stores.STORES_FILE
    if record.is_file():
        if _places(record) != _places(saved):
            say(
                "This install's record of where the bulky files live differs from the "
                "backup's, and was kept. Check it with: framefound storage"
            )
        return False
    data_dir.mkdir(parents=True, exist_ok=True)
    partial = record.with_name(record.name + ".partial")
    partial.write_text(body, encoding="utf-8", newline="\n")
    partial.replace(record)
    say(
        "The record of where the bulky files live was restored with the catalogue. "
        "Check it with: framefound storage"
    )
    return True


def _places(record: Path) -> object:
    """What a stores file says about where things live (not who is paused)."""
    try:
        raw = json.loads(record.read_text(encoding="utf-8"))
        return (raw.get("bulk"), raw.get("kinds") or {}) if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None
