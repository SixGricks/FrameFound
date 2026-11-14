"""Storage inspection and setup guidance (stage 1 of docs/roadmap.md).

Deliberately read-only. Mounting a filesystem needs CAP_SYS_ADMIN, and
granting that to an internet-facing web application would let it mount
arbitrary network paths as root — the escalation the threat model exists to
prevent. So this endpoint reports what is already mounted and *generates the
fstab line to paste*, rather than performing the mount itself. A scoped mount
helper is planned separately (ADR-0018).

The second half of this module is the other meaning of "storage": where the
bulky kinds of derived file live (storage/stores.py), and moving one of them
to another folder without a console (storage/moves.py). Nothing there mounts
anything either — a folder has to be on a drive the server can already see.
"""

import asyncio
import os
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from framefound.auth import service as auth_service
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, client_ip, require_admin
from framefound.config import Settings
from framefound.db.models import Asset, Derivative, Library, Scan, Slideshow
from framefound.ops import proxy_store
from framefound.storage import move_job, moves, stores
from framefound.storage.spec import MountSpec, MountSpecError, parse_mount_spec

log = structlog.get_logger()
router = APIRouter(prefix="/storage", tags=["storage"])

# Pseudo-filesystems that are never useful as media or cache storage.
IGNORED_FSTYPES = {
    "proc",
    "sysfs",
    "devtmpfs",
    "devpts",
    "tmpfs",
    "cgroup",
    "cgroup2",
    "overlay",
    "squashfs",
    "mqueue",
    "securityfs",
    "debugfs",
    "tracefs",
    "configfs",
    "fusectl",
    "pstore",
    "bpf",
    "autofs",
    "binfmt_misc",
    "hugetlbfs",
    "ramfs",
    "efivarfs",
    "nsfs",
}
NETWORK_FSTYPES = {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs"}


class Mount(BaseModel):
    path: str
    fstype: str
    is_network: bool
    total_gb: float | None
    free_gb: float | None
    writable: bool
    role: str  # media | cache | unused
    library_name: str | None = None
    asset_count: int | None = None


class StorageReport(BaseModel):
    media_root: str
    data_store: str
    mounts: list[Mount]
    hint: str
    # False where this server cannot see or add mounts (the Mac install has
    # no /proc and no mount helper; drives are connected in Finder there).
    # The page hides the add-a-drive form on it rather than offering a
    # Linux command that cannot work.
    drive_mounting: bool = True


def _read_mounts() -> list[tuple[str, str]]:
    """(mountpoint, fstype) pairs visible to this container."""
    try:
        lines = Path("/proc/self/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    seen: dict[str, str] = {}
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mountpoint, fstype = parts[1].replace("\\040", " "), parts[2]
        if fstype in IGNORED_FSTYPES or mountpoint.startswith(("/proc", "/sys", "/dev")):
            continue
        # Docker bind-mounts individual files (/etc/hosts, /etc/resolv.conf).
        # They are real mounts but never storage anyone would assign a role to.
        if not Path(mountpoint).is_dir():
            continue
        seen.setdefault(mountpoint, fstype)
    return sorted(seen.items())


@router.get("", response_model=StorageReport)
async def storage_report(_user: CurrentUser, db: DbDep, settings: SettingsDep) -> StorageReport:
    libraries = (await db.execute(select(Library))).scalars().all()
    counts: dict[uuid.UUID, int] = {
        library_id: count
        for library_id, count in (
            await db.execute(select(Asset.library_id, func.count()).group_by(Asset.library_id))
        ).all()
    }

    mounts: list[Mount] = []
    for mountpoint, fstype in _read_mounts():
        try:
            usage = shutil.disk_usage(mountpoint)
            total_gb: float | None = round(usage.total / 1024**3, 1)
            free_gb: float | None = round(usage.free / 1024**3, 1)
        except OSError:
            total_gb = free_gb = None

        library = next((lib for lib in libraries if lib.root_path.startswith(mountpoint)), None)
        role = "unused"
        if str(settings.data_dir).startswith(mountpoint):
            role = "cache"
        if library is not None or str(settings.media_root).startswith(mountpoint):
            role = "media"

        mounts.append(
            Mount(
                path=mountpoint,
                fstype=fstype,
                is_network=fstype in NETWORK_FSTYPES,
                total_gb=total_gb,
                free_gb=free_gb,
                # Media is intentionally mounted read-only; that is a feature,
                # so report it rather than treating it as a problem.
                writable=_is_writable(Path(mountpoint)),
                role=role,
                library_name=library.name if library else None,
                asset_count=counts.get(library.id) if library else None,
            )
        )

    return StorageReport(
        media_root=str(settings.media_root),
        data_store=str(settings.data_dir),
        mounts=mounts,
        hint=(
            "Media shares should be mounted read-only; the preview cache needs "
            "a separate writable share."
            if _can_mount()
            else "Drives are connected on this machine itself (in Finder on a Mac), "
            "not from here. Where the bulky files live is set below."
        ),
        drive_mounting=_can_mount(),
    )


def _can_mount() -> bool:
    """Whether the mounts table and the add-a-drive form mean anything here:
    only where the kernel's mount list can be read (Linux, the containers)."""
    return Path("/proc/self/mounts").is_file()


def _is_writable(path: Path) -> bool:
    import os

    return os.access(path, os.W_OK)


class FstabRequest(BaseModel):
    server: str = Field(min_length=1, max_length=253)
    share: str = Field(min_length=1, max_length=255)
    mount_point: str = Field(min_length=1, max_length=1024)
    protocol: str = Field(default="cifs", pattern="^(cifs|nfs)$")
    purpose: str = Field(default="media", pattern="^(media|cache)$")


class FstabResponse(BaseModel):
    fstab_line: str
    commands: list[str]
    notes: list[str]


@router.post("/fstab-line", response_model=FstabResponse, dependencies=[require_admin])
async def generate_fstab_line(body: FstabRequest, _user: CurrentUser) -> FstabResponse:
    """Produce the exact host configuration for a new share.

    Media mounts are read-only so a compromised container still cannot alter
    originals; cache mounts are read-write and owned by uid 1000, the user the
    containers run as.
    """
    read_only = body.purpose == "media"
    access = "ro" if read_only else "rw"
    server, share, target = body.server.strip(), body.share.strip("/"), body.mount_point

    if body.protocol == "cifs":
        options = (
            f"credentials=/etc/framefound-smb.cred,{access},uid=1000,gid=1000,"
            "iocharset=utf8,vers=3.0,_netdev,nofail"
        )
        source = f"//{server}/{share}"
    else:
        options = f"{access},_netdev,nofail,soft,timeo=100"
        source = f"{server}:/{share}"

    commands = [f"sudo mkdir -p {target}"]
    if body.protocol == "cifs":
        commands.append(
            'sudo bash -c \'printf "username=USER\\npassword=PASS\\n" '
            "> /etc/framefound-smb.cred && chmod 600 /etc/framefound-smb.cred'"
        )
    commands += [
        f"echo '{source}  {target}  {body.protocol}  {options}  0 0' | sudo tee -a /etc/fstab",
        f"sudo mount {target}",
    ]

    notes = [
        "_netdev,nofail keeps the machine booting when the NAS is unreachable.",
        "uid=1000,gid=1000 matches the user inside the containers; without it every write fails."
        if body.protocol == "cifs"
        else "Check the export's squash settings so uid 1000 can write.",
    ]
    if read_only:
        notes.append("Mounted read-only: FrameFound can never modify your originals.")
        notes.append(f"Then add it as a library, or set FRAMEFOUND_MEDIA_ROOT={target}.")
    else:
        notes.append(f"Then set FRAMEFOUND_DATA_STORE={target} and restart the stack.")

    return FstabResponse(
        fstab_line=f"{source}  {target}  {body.protocol}  {options}  0 0",
        commands=commands,
        notes=notes,
    )


class AddDriveRequest(BaseModel):
    """A network share to mount and, optionally, start using immediately."""

    protocol: str = Field(default="cifs", pattern="^(cifs|nfs)$")
    server: str = Field(min_length=1, max_length=253)
    share: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=120, description="Folder name under the root")
    purpose: str = Field(default="media", pattern="^(media|cache)$")
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=400)
    # Media shares usually want a library straight away; a cache share does not.
    create_library: bool = True
    library_name: str = Field(default="", max_length=120)


class DriveResult(BaseModel):
    ok: bool
    target: str
    detail: str = ""
    library_id: uuid.UUID | None = None
    fstab_line: str = ""
    persist_hint: str = ""


class UnmountDriveRequest(BaseModel):
    target: str = Field(min_length=1, max_length=1024)


def _mounter_base() -> str:
    import os

    return os.environ.get("FRAMEFOUND_MOUNTER_URL", "http://mounter:8000")


def _mounter_token() -> str:
    import os

    return os.environ.get("FRAMEFOUND_MOUNTER_TOKEN", "")


async def _call_mounter(path: str, payload: dict[str, object]) -> dict[str, object]:
    import httpx

    token = _mounter_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail=(
                "The mount helper is not configured. Set FRAMEFOUND_MOUNTER_TOKEN "
                "and start the `mounter` service, or add the drive from the host "
                "using the generated fstab line."
            ),
        )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{_mounter_base()}{path}",
                json=payload,
                headers={"X-FrameFound-Token": token},
            )
    except Exception as err:
        raise HTTPException(status_code=503, detail="The mount helper is not reachable.") from err
    if response.status_code >= 400:
        detail = response.json().get("detail", "The mount was refused.")
        raise HTTPException(status_code=response.status_code, detail=str(detail))
    return dict(response.json())


@router.post("/drives", response_model=DriveResult, dependencies=[require_admin])
async def add_drive(body: AddDriveRequest, _user: CurrentUser, db: DbDep) -> DriveResult:
    """Mount a network share and optionally register it as a library.

    The request is validated here *and* again inside the helper. This side
    validating is a courtesy that produces a good error message; the helper
    validating is the control that matters, because it is the process holding
    the capability.
    """
    try:
        spec = parse_mount_spec(
            protocol=body.protocol,
            server=body.server,
            share=body.share,
            name=body.name,
            purpose=body.purpose,
        )
    except MountSpecError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    result = await _call_mounter(
        "/mount",
        {
            "protocol": spec.protocol,
            "server": spec.server,
            "share": spec.share,
            "name": spec.name,
            "purpose": spec.purpose,
            "username": body.username,
            "password": body.password,
        },
    )
    if not result.get("ok"):
        return DriveResult(ok=False, target=str(spec.target), detail=str(result.get("detail", "")))

    # Containers see the shared roots at /media and /cache; the helper mounts
    # at /mnt/... on the host side of the same bind.
    container_path = _container_path(spec)

    library_id: uuid.UUID | None = None
    if body.purpose == "media" and body.create_library:
        existing = (
            await db.execute(select(Library).where(Library.root_path == container_path))
        ).scalar_one_or_none()
        if existing is None:
            library = Library(
                name=(body.library_name or body.name).strip(),
                root_path=container_path,
                read_only=True,
                enabled=True,
                watcher_enabled=True,
                # Off by default: proxies are the fastest way to fill a disk,
                # and a new share's size is unknown until it is scanned.
                generate_proxies=False,
                transcribe_enabled=True,
                scan_interval_minutes=1440,
            )
            db.add(library)
            await db.flush()
            db.add(Scan(library_id=library.id, status="pending"))
            await db.commit()
            library_id = library.id
        else:
            library_id = existing.id

    return DriveResult(
        ok=True,
        target=str(spec.target),
        library_id=library_id,
        fstab_line=spec.fstab_line(),
        persist_hint=(
            "This mount is live now but will not survive a host reboot. Add the "
            "fstab line above on the host to make it permanent."
        ),
    )


def _container_path(spec: "MountSpec") -> str:
    """Where the containers will see this mount.

    The helper mounts under /mnt/media or /mnt/cache on the host; the app
    containers bind those same roots at /media and /cache.
    """
    return f"/media/{spec.name}" if spec.purpose == "media" else f"/cache/{spec.name}"


@router.post("/drives/unmount", response_model=DriveResult, dependencies=[require_admin])
async def unmount_drive(body: UnmountDriveRequest, _user: CurrentUser, db: DbDep) -> DriveResult:
    """Unmount a share. Refuses while a library still points at it.

    Unmounting under a live library would flip every one of its assets to
    `missing` on the next scan, which reads like data loss even though the
    catalogue is intact.
    """
    name = body.target.rstrip("/").rsplit("/", 1)[-1]
    for candidate in (f"/media/{name}", f"/cache/{name}"):
        library = (
            await db.execute(select(Library).where(Library.root_path == candidate))
        ).scalar_one_or_none()
        if library is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"The library '{library.name}' still uses this drive. "
                    "Remove or disable the library first."
                ),
            )

    result = await _call_mounter("/unmount", {"target": body.target})
    return DriveResult(
        ok=bool(result.get("ok")),
        target=body.target,
        detail=str(result.get("detail", "")),
    )


# --- where the bulky kinds live ------------------------------------------------
#
# Each kind (storage/stores.py) lives on this machine's own disk, in the one
# bulk folder, or in a folder of its own. These endpoints say where each one
# is and move one. The System page's volume list cannot say the first: it
# merges a bulk folder into the library that shares its drive.

# A probe is a directory read on what may be a network share. One that does
# not answer in this long is reported as such, not waited for — the rule,
# and the incident behind it, are `_volumes`' in api/v1/system.py.
PROBE_TIMEOUT_S = 10.0
# The page asks again every few seconds while it is open.
KINDS_CACHE_S = 10.0
# Sizes found by walking a folder tree (the model library, frame grabs).
TREE_CACHE_S = 600.0

# Not one of the movable kinds; listed so its size can be seen. A frame grab
# is about 50 MB and one video may have three hundred.
GRABS = "grabs"

_LABELS = {
    "proxies": "Video proxies",
    "renders": "Slideshow videos",
    "exports": "Listing exports",
    "basemaps": "Basemaps",
    "models": "Model library",
    "backups": "Backup copies",
    GRABS: "Frame grabs",
}
# Counted by the kind's own pattern (`moves.usage`): a handful of large
# files each. The model library too: its pattern is only its `library/`
# folder (the ONNX and Whisper caches beside it are not counted), and these
# are the files a move of it copies and Remove old copies counts, so the
# page, the move and `framefound storage` give one number.
_FLAT = frozenset({"exports", "basemaps", "backups", "models"})
# Counted by walking the tree, and remembered for TREE_CACHE_S.
_WALKED = frozenset({GRABS})


def _description(kind: str, settings: Settings) -> str:
    if kind == "proxies":
        return (
            "A smaller copy of each video, for playing and scrubbing. Most of the "
            "space FrameFound uses. They can be made again from the originals."
        )
    if kind == "renders":
        return "The finished video of each slideshow."
    if kind == "exports":
        return "The zip file made each time a listing is exported."
    if kind == "basemaps":
        return "Offline maps for the Places page, one file per region."
    if kind == "models":
        # Only reads the stores record (no look at the folder). While the
        # library is on this machine nothing is copied (library.local_dir).
        if stores.is_separate("models", settings):
            return (
                "Every AI model you have added. A model that has a role is also copied to "
                "this machine, so it keeps working when this folder is not connected."
            )
        # Said for the folder as it is now and for after a move: "nothing is
        # copied" read as true for good, and the Move form sits right below.
        return (
            "Every AI model you have added is kept in this folder, and runs from it "
            "while it is on this machine. Moved to another drive, a model that has a "
            "role is also copied to this machine."
        )
    if kind == "backups":
        return (
            "A second copy of each nightly catalogue backup. The first copy "
            "always stays on this machine."
        )
    return (
        "Full-quality stills taken from video for editing, about 50 MB each. "
        "They stay on this machine."
    )


class KindStatus(BaseModel):
    kind: str  # proxies | renders | exports | basemaps | models | backups | grabs
    label: str
    description: str
    # The folder in use. None only for backup copies when none is kept.
    path: str | None
    # Where it lives on this machine when it has not been moved.
    default_path: str | None
    # Its folder inside the bulk folder, when there is a bulk folder and the
    # kind is not already in it: the place to offer as a move's target.
    bulk_path: str | None
    # local (its usual place on this machine) | bulk | custom | none
    where: str
    connected: bool
    # Why its folder cannot be used right now. For an administrator: has paths.
    problem: str | None
    files: int | None
    size_bytes: int | None
    free_gb: float | None
    total_gb: float | None
    movable: bool
    # Where the last move left this kind's old copies, until they are removed.
    old_path: str | None


class MoveStatus(BaseModel):
    id: str
    kind: str
    label: str
    source: str | None
    target: str
    mode: str  # copy | empty
    # queued | checking | copying | finishing | switching | done | failed | cancelled
    phase: str
    # Waiting or running: nothing else can be moved, and the page keeps asking.
    active: bool
    # Cancel was asked for and the move has not stopped yet.
    cancelling: bool
    # Of the pass in hand; they start again for each catch-up pass.
    files_total: int
    files_done: int
    bytes_total: int
    bytes_done: int
    # Set when done: how many files had to be copied (the rest were there).
    copied: int | None
    error: str | None
    requested_by: str
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class StorageKinds(BaseModel):
    bulk_root: str | None
    # Set when FrameFound's own record of where things live cannot be read.
    problem: str | None
    kinds: list[KindStatus]
    # The move in progress, or the last one and how it ended.
    move: MoveStatus | None
    # A move that `move` is not following is running (usually one started
    # from a terminal): the sentence to show. Nothing else can be moved
    # until it ends.
    busy: str | None = None
    # Whether this account may start a move or change the bulk folder now.
    can_move: bool


@dataclass(frozen=True)
class _Place:
    kind: str
    root: Path | None
    default: Path | None
    where: str
    bulk_path: Path | None
    old: Path | None


@dataclass(frozen=True)
class _Layout:
    bulk: Path | None
    damaged: str | None
    places: tuple[_Place, ...]


def _layout(settings: Settings) -> _Layout:
    """Where every kind lives. Read from the settings and the stores file on
    this machine's own disk; no store is touched."""
    state = stores.read_state(settings.data_dir)
    bulk = stores.bulk_root(settings)
    places: list[_Place] = []
    for kind in stores.KINDS:
        root = stores.kind_root(kind, settings)
        default = stores.default_root(kind, settings)
        in_bulk = None if bulk is None else bulk / kind
        if root is None:
            where = "none"
        elif root == default:
            where = "local"
        elif root == in_bulk:
            where = "bulk"
        else:
            where = "custom"
        old = state.previous.get(kind)
        places.append(
            _Place(
                kind,
                root,
                default,
                where,
                None if root == in_bulk else in_bulk,
                None if old == root else old,
            )
        )
    grabs = settings.data_dir / GRABS
    places.append(_Place(GRABS, grabs, grabs, "local", None, None))
    return _Layout(bulk, state.damaged, tuple(places))


@dataclass(frozen=True)
class _Measured:
    problem: str | None = None
    files: int | None = None
    size_bytes: int | None = None
    total: int | None = None
    free: int | None = None
    # The folder is the kind's place in the bulk folder under another
    # spelling (`Framefound`/`FrameFound` on a Mac's volumes, a symlink).
    in_bulk: bool = False


# folder -> (when it was walked, (files, bytes))
_TREE_CACHE: dict[Path, tuple[float, tuple[int, int]]] = {}


def _tree_size(root: Path) -> tuple[int, int]:
    """(files, bytes) under `root`, by walking it. Links are not followed: a
    model cache links every file to the one real copy of it."""
    cached = _TREE_CACHE.get(root)
    if cached is not None and time.monotonic() - cached[0] < TREE_CACHE_S:
        return cached[1]
    files = size = 0
    folders = [root]
    while folders:
        try:
            with os.scandir(folders.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        folders.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        files += 1
                        size += entry.stat(follow_symlinks=False).st_size
        except OSError:  # not made yet, or removed while being counted
            continue
    # Remembered here rather than by the caller, so a walk that outlasted the
    # request that asked for it still answers the next one.
    _TREE_CACHE[root] = (time.monotonic(), (files, size))
    return files, size


def _measure(kind: str, root: Path, settings: Settings) -> _Measured:
    """Is the kind's folder there, what is in it, and how much room is left.
    Blocking, on what may be a share: only ever called in a thread."""
    try:
        problem = stores.kind_problem(kind, settings) if kind in stores.KINDS else None
        if problem is not None:
            return _Measured(problem=problem)
        files: int | None = None
        size: int | None = None
        if kind in _FLAT:
            files, size = moves.usage(kind, root)
        elif kind in _WALKED:
            files, size = _tree_size(root)
        # A kind that has made nothing yet has no folder; the disk it will
        # fill is the one its nearest existing parent is on.
        there = next((p for p in (root, *root.parents) if p.exists()), None)
        usage = shutil.disk_usage(there) if there is not None else None
        # Text says "a folder of its own" for a kind whose folder is spelt
        # differently from its place in the bulk folder; the disk may say
        # they are one. Offering to move it "into" the bulk folder would be
        # offering to move it onto itself.
        bulk = stores.bulk_root(settings) if kind in stores.KINDS else None
        in_bulk = bulk is not None and root != bulk / kind and moves.one_folder(root, bulk / kind)
    except OSError as err:
        return _Measured(problem=f"{root} cannot be read ({err.strerror or err}).")
    return _Measured(
        files=files,
        size_bytes=size,
        total=None if usage is None else usage.total,
        free=None if usage is None else usage.free,
        in_bulk=in_bulk,
    )


_KindsKey = tuple[tuple[str, Path | None], ...]
# Keyed by where every kind lives, so a move that has just switched one is
# measured afresh rather than answered from memory.
_KINDS_CACHE: dict[_KindsKey, tuple[float, dict[str, _Measured]]] = {}
# The question still being asked of each folder, if one is.
_ASKING: dict[tuple[str, Path], "asyncio.Future[_Measured]"] = {}


async def _ask(kind: str, root: Path, settings: Settings) -> _Measured:
    """`_measure`, off the event loop, and not waited for past the timeout.

    A share that has hung keeps the thread asking it until the system gives
    up, a minute or more, and this page asks every few seconds. A new thread
    for each would use up every thread the API has — the same ones that
    serve media — so while one question to a folder is unanswered, no second
    is asked."""
    loop = asyncio.get_running_loop()
    key = (kind, root)
    asking = _ASKING.get(key)
    if asking is None or asking.done() or asking.get_loop() is not loop:
        asking = asyncio.ensure_future(asyncio.to_thread(_measure, kind, root, settings))
        # Looked at even when nobody is waiting for it any more.
        asking.add_done_callback(lambda done: done.cancelled() or done.exception())
        _ASKING[key] = asking
    try:
        return await asyncio.wait_for(asyncio.shield(asking), PROBE_TIMEOUT_S)
    except TimeoutError:
        return _Measured(
            problem=f"This location did not answer within {PROBE_TIMEOUT_S:.0f} seconds."
        )


async def _measure_all(layout: _Layout, settings: Settings) -> dict[str, _Measured]:
    key: _KindsKey = tuple((place.kind, place.root) for place in layout.places)
    cached = _KINDS_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < KINDS_CACHE_S:
        return cached[1]
    asked = [(place.kind, place.root) for place in layout.places if place.root is not None]
    answers = await asyncio.gather(*(_ask(kind, root, settings) for kind, root in asked))
    measured = {kind: answer for (kind, _root), answer in zip(asked, answers, strict=True)}
    _KINDS_CACHE.clear()
    _KINDS_CACHE[key] = (time.monotonic(), measured)
    return measured


async def _catalogue_sizes(db: DbDep) -> dict[str, tuple[int, int]]:
    """(files, bytes) of the kinds whose sizes the catalogue already holds.
    Tens of thousands of proxies on a share are not walked to be counted."""
    proxies: Any = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(Derivative.size_bytes), 0)).where(
                Derivative.kind == "proxy", Derivative.status == "ready"
            )
        )
    ).one()
    renders: Any = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(Slideshow.size_bytes), 0)).where(
                Slideshow.relative_path.is_not(None)
            )
        )
    ).one()
    return {
        "proxies": (int(proxies[0]), int(proxies[1])),
        "renders": (int(renders[0]), int(renders[1])),
    }


def _gb(size: int | None) -> float | None:
    return None if size is None else round(size / 1024**3, 1)


def _when(moment: float | None) -> datetime | None:
    return None if moment is None else datetime.fromtimestamp(moment, tz=UTC)


def _status(state: move_job.MoveState) -> MoveStatus:
    shown = move_job.settled(state)
    active = shown.phase not in move_job.FINISHED
    return MoveStatus(
        id=shown.id,
        kind=shown.kind,
        label=_LABELS.get(shown.kind, shown.kind),
        source=shown.source or None,
        target=shown.target,
        mode=shown.mode,
        phase=shown.phase,
        active=active,
        cancelling=active and shown.cancel,
        files_total=shown.files_total,
        files_done=shown.files_done,
        bytes_total=shown.bytes_total,
        bytes_done=shown.bytes_done,
        copied=shown.copied,
        error=shown.error,
        requested_by=shown.requested_by,
        requested_at=datetime.fromtimestamp(shown.requested_at, tz=UTC),
        started_at=_when(shown.started_at),
        finished_at=_when(shown.finished_at),
    )


def _move_out(state: move_job.MoveState | None) -> MoveStatus | None:
    return None if state is None else _status(state)


async def _answered[T](check: Callable[[], T], folder: Path) -> T:
    """Run a check of `folder` off the event loop. A refusal becomes a 400
    with its sentence; a folder that does not answer, a 503."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(check), PROBE_TIMEOUT_S)
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{folder} did not answer within {PROBE_TIMEOUT_S:.0f} seconds. Check that "
                "the drive or share is connected, then try again."
            ),
        ) from None
    except moves.MoveError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@router.get("/kinds", response_model=StorageKinds)
async def storage_kinds(user: CurrentUser, db: DbDep, settings: SettingsDep) -> StorageKinds:
    """Each bulky kind of file: where it lives, how much there is, and
    whether that place is connected."""
    layout = await asyncio.to_thread(_layout, settings)
    measured = await _measure_all(layout, settings)
    known = await _catalogue_sizes(db)
    move = await move_job.load(db)
    busy = None if move_job.is_active(move) else await _moving_elsewhere(settings)
    kinds: list[KindStatus] = []
    for place in layout.places:
        found = measured.get(place.kind, _Measured())
        files, size = found.files, found.size_bytes
        if place.kind in known:
            files, size = known[place.kind]
        in_bulk = found.in_bulk or place.where == "bulk"
        kinds.append(
            KindStatus(
                kind=place.kind,
                label=_LABELS[place.kind],
                description=_description(place.kind, settings),
                path=None if place.root is None else str(place.root),
                default_path=None if place.default is None else str(place.default),
                bulk_path=(None if in_bulk or place.bulk_path is None else str(place.bulk_path)),
                where="bulk" if in_bulk else place.where,
                connected=place.root is not None and found.problem is None,
                problem=found.problem,
                files=files,
                size_bytes=size,
                free_gb=_gb(found.free),
                total_gb=_gb(found.total),
                movable=place.kind in moves.MOVABLE,
                old_path=None if place.old is None else str(place.old),
            )
        )
    return StorageKinds(
        bulk_root=None if layout.bulk is None else str(layout.bulk),
        problem=layout.damaged,
        kinds=kinds,
        move=_move_out(move),
        busy=busy,
        can_move=(
            user.role == "admin"
            and layout.damaged is None
            and not move_job.is_active(move)
            and busy is None
        ),
    )


def _folder(text: str) -> Path:
    """The folder a request names, with any `..` worked out first: the
    checks that follow compare it with the data folder and the libraries by
    their parts, and `/data/../data/renders` must not read as somewhere else."""
    return moves.tidy(Path(text.strip()))


async def _moving_elsewhere(settings: Settings) -> str | None:
    """The sentence for a move that is running and is not the one the
    `storage_move` row is following: usually one started with `framefound
    storage move`. The turn is a file in the data folder
    (moves.active_move), so the page knows of a move whoever started it."""
    held = await asyncio.to_thread(moves.active_move, settings)
    return None if held is None else moves.busy_sentence(held)


async def _nothing_moving(db: DbDep, settings: Settings) -> None:
    current = await move_job.load(db)
    if current is not None and move_job.is_active(current):
        things = stores.words(current.kind).things.capitalize()
        raise HTTPException(
            status_code=409,
            detail=(
                f"{things} are being moved right now. Wait for that to finish, or "
                "cancel it, before changing where anything else lives."
            ),
        )
    elsewhere = await _moving_elsewhere(settings)
    if elsewhere is not None:
        raise HTTPException(status_code=409, detail=elsewhere)


class BulkRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    # Yes to the question a 428 asked: "…is on the same disk… Use this
    # folder anyway?", or "…its marker file is missing… Start it empty?"
    # for a bulk folder that no longer looks like one.
    confirm_same_disk: bool = False


class BulkResult(BaseModel):
    bulk_root: str
    # Kinds that already hold files and so were left where they are. Each is
    # brought over with its own move, when the operator chooses.
    staying: list[str]


@router.post("/bulk", response_model=BulkResult, dependencies=[require_admin])
async def set_bulk_root(
    body: BulkRequest, request: Request, user: CurrentUser, db: DbDep, settings: SettingsDep
) -> BulkResult:
    """Name the one folder for everything bulky. Nothing is moved: a kind
    with no files yet starts there, a kind that has files stays put."""
    await _nothing_moving(db, settings)
    root = _folder(body.path)
    catalogued = await moves.catalogued_under(db, root, settings)

    def check() -> moves.Question | None:
        # A look only: nothing is made in the folder until the question, if
        # there is one, has been answered.
        moves.validate_bulk_root(root, settings, catalogued=catalogued, create=False)
        return moves.bulk_question(root, settings)

    question = await _answered(check, root)
    if question is not None and not body.confirm_same_disk:
        raise HTTPException(status_code=428, detail=str(question))
    try:
        # Not under the timeout: a write is seen through, never abandoned
        # halfway and reported as refused while it carries on in its thread.
        staying = await asyncio.to_thread(
            moves.adopt_bulk_root,
            root,
            settings=settings,
            catalogued=catalogued,
            sure=body.confirm_same_disk,
        )
    except moves.MoveError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    _KINDS_CACHE.clear()
    await auth_service.audit(
        db,
        "storage.bulk_folder_set",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"folder": str(root), "staying": staying},
    )
    await db.commit()
    log.info("storage.bulk_folder_set", folder=str(root), staying=staying)
    return BulkResult(bulk_root=str(root), staying=staying)


def _movable(kind: str) -> stores.Words:
    if kind in moves.MOVABLE:
        return stores.words(kind)
    if kind in _LABELS:
        raise HTTPException(status_code=400, detail=f"{_LABELS[kind]} cannot be moved.")
    raise HTTPException(status_code=404, detail="No such kind of storage")


class MoveRequest(BaseModel):
    target: str = Field(min_length=1, max_length=1024)
    # copy: bring the files along. empty: start the new folder with nothing,
    # for a store that is gone for good (its files are made again).
    mode: Literal["copy", "empty"] = "copy"
    # Yes to the question a 428 asked. Until it is sent, nothing is made in
    # the folder: a no leaves nothing behind where a share mounts.
    confirm_same_disk: bool = False


@router.post(
    "/kinds/{kind}/move",
    response_model=MoveStatus,
    status_code=202,
    dependencies=[require_admin],
)
async def move_kind(
    kind: str,
    body: MoveRequest,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
) -> MoveStatus:
    """Start moving one kind to another folder. The folder is checked here,
    so a wrong one is refused at once; the copy itself runs in a worker, and
    `GET /storage/move` says how far it has got."""
    words = _movable(kind)
    await _nothing_moving(db, settings)
    target = _folder(body.target)
    catalogued = await moves.catalogued_under(db, target, settings)
    # What the catalogue says the kind takes, for the kinds too many to
    # count on a share inside a request (moves.room_problem).
    known_bytes = (await _catalogue_sizes(db)).get(kind, (0, None))[1]

    def check() -> moves.Preflight:
        # Everything the worker would refuse, found now: said in a progress
        # row minutes later it is a puzzle, said here it answers the click.
        found = moves.preflight(
            kind,
            target,
            settings=settings,
            mode=body.mode,
            catalogued=catalogued,
            known_bytes=known_bytes,
            confirmed=body.confirm_same_disk,
        )
        if found.already:
            raise moves.MoveError(f"{words.things.capitalize()} already live in {target}.")
        return found

    found = await _answered(check, target)
    if found.question is not None and not body.confirm_same_disk:
        raise HTTPException(status_code=428, detail=str(found.question))

    state = move_job.new(kind, target, source=found.source, mode=body.mode, requested_by=user.email)
    # The row first: the job looks for it the moment it is queued.
    await move_job.save(db, state)
    try:
        from framefound.processing.tasks import move_storage_kind

        move_storage_kind.delay(state.id)
    except Exception:
        state.phase = "failed"
        state.error = "The processing queue is unavailable, so the move was not started."
        state.finished_at = time.time()
        await move_job.save(db, state)
        raise HTTPException(status_code=503, detail="The processing queue is unavailable") from None
    await auth_service.audit(
        db,
        "storage.move_started",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"kind": kind, "from": state.source, "to": state.target, "mode": state.mode},
    )
    await db.commit()
    log.info("storage.move_queued", kind=kind, target=str(target), mode=body.mode)
    return _status(state)


@router.get("/move", response_model=MoveStatus | None)
async def move_progress(_user: CurrentUser, db: DbDep) -> MoveStatus | None:
    """The move in progress, or the last one and how it ended; null before
    the first. Reads one row: cheap enough to ask every two seconds."""
    return _move_out(await move_job.load(db))


@router.post("/move/cancel", response_model=MoveStatus | None, dependencies=[require_admin])
async def cancel_move(request: Request, user: CurrentUser, db: DbDep) -> MoveStatus | None:
    """Stop the move. One still waiting for a worker is cancelled at once;
    one that is copying stops within a few seconds, with nothing changed and
    what it copied kept for another try."""
    state = await move_job.load(db)
    if state is None or not move_job.is_active(state):
        return _move_out(state)
    if state.phase == move_job.WAITING:
        # Its queue message is still out there; the job sees the row is no
        # longer waiting and does nothing.
        state.phase = "cancelled"
        state.error = move_job.NEVER_STARTED
        state.finished_at = time.time()
    await auth_service.audit(
        db,
        "storage.move_cancelled",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"kind": state.kind, "to": state.target},
    )
    # Each of these commits, and the audit row with it.
    if state.phase == "cancelled":
        await move_job.save(db, state)
    else:
        await move_job.ask_to_stop(db, state)
    return _move_out(state)


class CleanupRequest(BaseModel):
    # Also remove old files that have no copy in the folder now in use. Only
    # after a first call reported some `kept`, and the operator has looked.
    everything: bool = False
    # The folder the page showed (the kind's `old_path`). When the record
    # names another by the time the click arrives, nothing is removed and
    # the answer is 409. Optional, for older pages.
    expected_folder: str | None = Field(default=None, max_length=1024)


class CleanupResult(BaseModel):
    kind: str
    folder: str
    files: int
    bytes: int
    # Old files left alone because the folder in use has no copy of them.
    kept: int


@router.post("/kinds/{kind}/cleanup", response_model=CleanupResult, dependencies=[require_admin])
async def remove_old_copies(
    kind: str,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    body: CleanupRequest | None = None,
) -> CleanupResult:
    """Free the space a finished move left behind: this kind's files, in the
    folder it used to live in, and by default only those with a copy in the
    folder it lives in now."""
    words = _movable(kind)
    current = await move_job.load(db)
    if current is not None and current.kind == kind and move_job.is_active(current):
        raise HTTPException(
            status_code=409,
            detail=f"{words.things.capitalize()} are being moved right now. Nothing was removed.",
        )
    everything = body is not None and body.everything
    shown = body.expected_folder if body is not None else None
    expected = None if shown is None or not shown.strip() else _folder(shown)
    old = (await asyncio.to_thread(stores.read_state, settings.data_dir)).previous.get(kind)
    try:
        # In a thread, and seen through: thousands of deletions on a share
        # take minutes, and asking again afterwards removes what is left.
        removed = await asyncio.to_thread(
            moves.remove_old_copies,
            kind,
            settings=settings,
            everything=everything,
            expected=expected,
        )
    except moves.FolderChanged as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except moves.MoveError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    _KINDS_CACHE.clear()
    await auth_service.audit(
        db,
        "storage.old_copies_removed",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={
            "kind": kind,
            "folder": str(old),
            "files": removed.files,
            "bytes": removed.bytes,
            "kept": removed.kept,
            "everything": everything,
        },
    )
    await db.commit()
    return CleanupResult(
        kind=kind, folder=str(old), files=removed.files, bytes=removed.bytes, kept=removed.kept
    )


class RebuildResult(BaseModel):
    # Proxies the catalogue lists as made, and of those, how many have no file.
    on_record: int
    missing: int
    # Videos in a library that makes proxies that never got one (skipped
    # while the proxy store was away, or stopped mid-transcode).
    never_made: int
    # How many were queued to be made: missing + never_made.
    queued: int


QUEUE_DOWN = "The processing queue is unavailable"


@router.post("/proxies/rebuild", response_model=RebuildResult, dependencies=[require_admin])
async def rebuild_proxies(
    request: Request, user: CurrentUser, db: DbDep, settings: SettingsDep
) -> RebuildResult:
    """Queue the proxies that should be there and are not: what `framefound
    proxies rebuild` does, from the Storage page. For after the proxy store
    was away, or was started empty. A proxy that exists is never made twice,
    so asking again is safe."""
    try:
        # Looks at the store and at every ready proxy's file, on what may be
        # a share: each look runs in a thread (proxy_store.missing).
        found = await proxy_store.find(db, settings.data_dir)
    except proxy_store.StoreAway:
        # A second look at the same share, so in a thread too.
        said = await asyncio.to_thread(stores.away, "proxies", settings)
        raise HTTPException(
            status_code=503,
            detail=said
            or "The proxy store is not connected. Reconnect the drive or share and try again.",
        ) from None
    wanted = len(found.missing) + len(found.never_made)
    stopped: proxy_store.QueueDown | None = None
    try:
        queued = await asyncio.to_thread(proxy_store.queue_all, found)
    except proxy_store.QueueDown as err:
        stopped, queued = err, err.queued
    if stopped is not None and queued == 0:
        raise HTTPException(status_code=503, detail=QUEUE_DOWN) from None
    # Recorded whenever anything was queued, even when the queue then went:
    # those proxies will be made.
    await auth_service.audit(
        db,
        "storage.proxies_rebuilt",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={
            "missing": len(found.missing),
            "never_made": len(found.never_made),
            "queued": queued,
        },
    )
    await db.commit()
    log.info("storage.proxies_rebuilt", queued=queued, wanted=wanted)
    if stopped is not None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{QUEUE_DOWN}: it stopped answering after {queued:,} of {wanted:,} were "
                "queued. Try again in a moment to queue the rest."
            ),
        )
    return RebuildResult(
        on_record=found.on_record,
        missing=len(found.missing),
        never_made=len(found.never_made),
        queued=queued,
    )
