"""Library management endpoints.

Authorization: reads for any signed-in user; anything that touches paths or
triggers work is admin-only. Library roots are validated against the media
root allowlist — the scanner never accepts arbitrary filesystem paths.
"""

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from framefound.auth import service as auth_service
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, client_ip, require_admin
from framefound.db.models import Asset, Library, PathMapping, Scan
from framefound.scanner.paths import PathValidationError, validate_library_root

log = structlog.get_logger()
router = APIRouter(prefix="/libraries", tags=["libraries"])

ACTIVE_SCAN_STATUSES = ("pending", "running", "paused")


class PathMappingIn(BaseModel):
    profile_name: str = Field(min_length=1, max_length=100)
    platform: str = Field(pattern="^(windows|macos|linux)$")
    mapped_prefix: str = Field(min_length=1, max_length=1024)


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    root_path: str = Field(min_length=1, max_length=1024)
    read_only: bool = True
    include_extensions: list[str] | None = None
    exclude_globs: list[str] = []
    scan_interval_minutes: int | None = Field(default=None, ge=5)
    watcher_enabled: bool = False
    path_mappings: list[PathMappingIn] = []


class LibraryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    include_extensions: list[str] | None = None
    exclude_globs: list[str] | None = None
    scan_interval_minutes: int | None = Field(default=None, ge=5)
    watcher_enabled: bool | None = None
    enabled: bool | None = None
    generate_proxies: bool | None = None
    proxy_resolution: int | None = Field(default=None, ge=360, le=2160)
    transcribe_enabled: bool | None = None


class LibraryOut(BaseModel):
    id: uuid.UUID
    name: str
    root_path: str
    read_only: bool
    include_extensions: list[str] | None
    exclude_globs: list[str]
    scan_interval_minutes: int | None
    watcher_enabled: bool
    enabled: bool
    generate_proxies: bool
    proxy_resolution: int
    transcribe_enabled: bool
    last_scan_at: datetime | None
    asset_count: int = 0

    model_config = {"from_attributes": True}


class ScanOut(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    files_seen: int
    files_new: int
    files_changed: int
    files_moved: int
    files_missing: int
    files_deferred: int
    error: str | None

    model_config = {"from_attributes": True}


async def _get_library(db: DbDep, library_id: uuid.UUID) -> Library:
    library = await db.get(Library, library_id)
    if library is None:
        raise HTTPException(404, "Library not found")
    return library


@router.post("", response_model=LibraryOut, status_code=201, dependencies=[require_admin])
async def create_library(
    body: LibraryCreate, request: Request, db: DbDep, settings: SettingsDep, user: CurrentUser
) -> LibraryOut:
    try:
        resolved = validate_library_root(body.root_path, settings.media_root)
    except PathValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    duplicate = (
        await db.execute(select(Library).where(Library.name == body.name))
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(409, "A library with that name already exists")

    library = Library(
        name=body.name,
        root_path=str(resolved),
        read_only=body.read_only,
        include_extensions=body.include_extensions,
        exclude_globs=body.exclude_globs,
        scan_interval_minutes=body.scan_interval_minutes,
        watcher_enabled=body.watcher_enabled,
    )
    db.add(library)
    await db.flush()
    for mapping in body.path_mappings:
        db.add(PathMapping(library_id=library.id, **mapping.model_dump()))
    db.add(Scan(library_id=library.id, status="pending"))  # initial scan
    await auth_service.audit(
        db,
        "library.created",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"name": body.name},
    )
    await db.commit()
    return LibraryOut.model_validate(library)


@router.get("", response_model=list[LibraryOut])
async def list_libraries(_user: CurrentUser, db: DbDep) -> list[LibraryOut]:
    rows = (
        await db.execute(
            select(Library, func.count(Asset.id))
            .outerjoin(Asset, Asset.library_id == Library.id)
            .group_by(Library.id)
            .order_by(Library.name)
        )
    ).all()
    results = []
    for library, count in rows:
        out = LibraryOut.model_validate(library)
        out.asset_count = count
        results.append(out)
    return results


@router.get("/{library_id}", response_model=LibraryOut)
async def get_library(library_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> LibraryOut:
    library = await _get_library(db, library_id)
    count = (
        await db.execute(select(func.count(Asset.id)).where(Asset.library_id == library_id))
    ).scalar_one()
    out = LibraryOut.model_validate(library)
    out.asset_count = count
    return out


@router.patch("/{library_id}", response_model=LibraryOut, dependencies=[require_admin])
async def update_library(
    library_id: uuid.UUID, body: LibraryUpdate, db: DbDep, _user: CurrentUser
) -> LibraryOut:
    library = await _get_library(db, library_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(library, field, value)
    await db.commit()
    return LibraryOut.model_validate(library)


@router.delete("/{library_id}", status_code=204, dependencies=[require_admin])
async def delete_library(
    library_id: uuid.UUID,
    confirm_name: str,
    request: Request,
    db: DbDep,
    user: CurrentUser,
) -> None:
    """Remove a library FROM THE CATALOG. Original files are never touched.
    Requires `confirm_name` to match the library name exactly."""
    library = await _get_library(db, library_id)
    if confirm_name != library.name:
        raise HTTPException(400, "Confirmation name does not match the library name")
    await auth_service.audit(
        db,
        "library.deleted",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"name": library.name},
    )
    await db.delete(library)  # cascades to assets/scans/mappings (catalog rows only)
    await db.commit()


@router.post(
    "/{library_id}/scan", response_model=ScanOut, status_code=202, dependencies=[require_admin]
)
async def trigger_scan(library_id: uuid.UUID, db: DbDep, _user: CurrentUser) -> ScanOut:
    library = await _get_library(db, library_id)
    active = (
        await db.execute(
            select(Scan).where(Scan.library_id == library.id, Scan.status.in_(ACTIVE_SCAN_STATUSES))
        )
    ).scalar_one_or_none()
    if active is not None:
        raise HTTPException(409, "A scan is already queued or running for this library")
    scan = Scan(library_id=library.id, status="pending")
    db.add(scan)
    await db.commit()
    return ScanOut.model_validate(scan)


@router.get("/{library_id}/scan", response_model=ScanOut)
async def latest_scan(library_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> ScanOut:
    await _get_library(db, library_id)
    scan = (
        await db.execute(
            select(Scan)
            .where(Scan.library_id == library_id)
            .order_by(Scan.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if scan is None:
        raise HTTPException(404, "No scans yet for this library")
    return ScanOut.model_validate(scan)


async def _transition_scan(
    db: DbDep, library_id: uuid.UUID, from_: tuple[str, ...], to: str
) -> Scan:
    scan = (
        await db.execute(select(Scan).where(Scan.library_id == library_id, Scan.status.in_(from_)))
    ).scalar_one_or_none()
    if scan is None:
        raise HTTPException(409, f"No scan in a state that can move to '{to}'")
    scan.status = to
    await db.commit()
    return scan


@router.post("/{library_id}/scan/pause", response_model=ScanOut, dependencies=[require_admin])
async def pause_scan(library_id: uuid.UUID, db: DbDep, _user: CurrentUser) -> ScanOut:
    return ScanOut.model_validate(await _transition_scan(db, library_id, ("running",), "paused"))


@router.post("/{library_id}/scan/resume", response_model=ScanOut, dependencies=[require_admin])
async def resume_scan(library_id: uuid.UUID, db: DbDep, _user: CurrentUser) -> ScanOut:
    return ScanOut.model_validate(await _transition_scan(db, library_id, ("paused",), "running"))


@router.post("/{library_id}/scan/cancel", response_model=ScanOut, dependencies=[require_admin])
async def cancel_scan(library_id: uuid.UUID, db: DbDep, _user: CurrentUser) -> ScanOut:
    return ScanOut.model_validate(
        await _transition_scan(db, library_id, ("pending", "running", "paused"), "cancelled")
    )


# --- path mappings --------------------------------------------------------
#
# These could only be set when a library was created, which made them
# unreachable for the four libraries that already existed — and the editing
# panels are the first thing that actually consumes them. A profile the
# operator cannot create is a feature that does not exist.


class PathMappingOut(PathMappingIn):
    id: uuid.UUID
    example: str


@router.get("/{library_id}/path-mappings", response_model=list[PathMappingOut])
async def list_path_mappings(
    library_id: uuid.UUID, _user: CurrentUser, db: DbDep
) -> list[PathMappingOut]:
    """Where each workstation mounts this library.

    Each entry carries a worked `example` — the library root as that machine
    would see it. A prefix looks right far more often than it is right, and one
    worked example is what makes a wrong drive letter obvious at a glance.
    """
    from framefound.api.v1.panel import _translate

    library = await db.get(Library, library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="No such library")

    rows = (
        (await db.execute(select(PathMapping).where(PathMapping.library_id == library_id)))
        .scalars()
        .all()
    )
    root = library.root_path.rstrip("/")
    return [
        PathMappingOut(
            id=row.id,
            profile_name=row.profile_name,
            platform=row.platform,
            mapped_prefix=row.mapped_prefix,
            example=_translate(f"{root}/Example/Clip.mp4", root, row.mapped_prefix),
        )
        for row in rows
    ]


@router.put(
    "/{library_id}/path-mappings",
    response_model=list[PathMappingOut],
    dependencies=[require_admin],
)
async def replace_path_mappings(
    library_id: uuid.UUID, body: list[PathMappingIn], _user: CurrentUser, db: DbDep
) -> list[PathMappingOut]:
    """Replace the whole set for one library.

    Replace rather than patch: there are rarely more than a handful, they are
    edited as a group, and a partial update across a unique (library, profile)
    constraint is a great deal of machinery for a form with three fields.
    """
    from sqlalchemy import delete as sql_delete

    library = await db.get(Library, library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="No such library")

    names = [m.profile_name.strip() for m in body]
    if len(set(names)) != len(names):
        raise HTTPException(status_code=400, detail="Two profiles cannot share a name")

    await db.execute(sql_delete(PathMapping).where(PathMapping.library_id == library_id))
    for mapping in body:
        db.add(PathMapping(library_id=library_id, **mapping.model_dump()))
    await db.commit()
    log.info("library.path_mappings_replaced", library_id=str(library_id), count=len(body))
    return await list_path_mappings(library_id, _user, db)
