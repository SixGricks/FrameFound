"""Catalogue backups for a native install.

The same archive `manage.sh backup` and the containers' backup service
write — `catalog.dump`, `manifest.json`, `checksums.sha256`, the settings
file as `env` — so an archive made on either kind of install restores on the
other, and the System page's "newest backup" check reads these unchanged.

Archives land in `<data>/backups`. That is the same disk as the database on
a one-disk Mac, so `FRAMEFOUND_BACKUP_COPY_DIR` (a NAS folder) takes a second
copy; a backup that only exists beside what it backs up is half a backup.
Each archive holds the settings file, and with it the key that unseals every
stored secret: written 600 here, and worth remembering on the NAS.
"""

import hashlib
import json
import shutil
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from framefound import __version__
from framefound.native import database, proc
from framefound.native.layout import Layout

MAX_AGE_S = 24 * 3600
PATTERN = "framefound-*.tar.gz"


class BackupError(RuntimeError):
    pass


def folder(env: dict[str, str], layout: Layout) -> Path:
    return Path(env.get("FRAMEFOUND_DATA_DIR") or layout.data) / "backups"


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
        archive = target / f"framefound-{now.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
        pack(work, archive)
    prune(target, keep)
    copy_dir = (env.get("FRAMEFOUND_BACKUP_COPY_DIR") or "").strip()
    if copy_dir:
        copy_off_machine(archive, Path(copy_dir), keep)
    return archive


def copy_off_machine(archive: Path, directory: Path, keep: int) -> bool:
    """A second copy somewhere else. A share that is away is not an error —
    the next night's backup copies again — but it is said."""
    if not directory.is_dir():
        print(f"backup: {directory} is not reachable; the off-machine copy was skipped")
        return False
    partial = directory / (archive.name + ".partial")
    shutil.copyfile(archive, partial)
    partial.replace(directory / archive.name)
    prune(directory, keep)
    return True


def restore(layout: Layout, env: dict[str, str], archive: Path) -> None:
    """Replace the catalogue with `archive`'s. The caller stops the app first."""
    with tempfile.TemporaryDirectory() as tmp:
        database.restore(env, unpack(archive, Path(tmp)))
