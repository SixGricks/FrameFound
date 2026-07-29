"""Storage inspection and setup guidance (stage 1 of docs/roadmap.md).

Deliberately read-only. Mounting a filesystem needs CAP_SYS_ADMIN, and
granting that to an internet-facing web application would let it mount
arbitrary network paths as root — the escalation the threat model exists to
prevent. So this endpoint reports what is already mounted and *generates the
fstab line to paste*, rather than performing the mount itself. A scoped mount
helper is planned separately (ADR-0018).
"""

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, require_admin
from framefound.db.models import Asset, Library, Scan
from framefound.storage.spec import MountSpec, MountSpecError, parse_mount_spec

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
        ),
    )


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
