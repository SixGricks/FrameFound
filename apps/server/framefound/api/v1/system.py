"""System status endpoints (authenticated)."""

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select, text

from framefound import __version__
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep
from framefound.db.models import Asset, Derivative, Job
from framefound.storage.reachability import unreachable_reason

log = structlog.get_logger()
router = APIRouter(prefix="/system", tags=["system"])

# The backup service writes one archive a day; a day and a half without a
# new one means it has stopped.
BACKUP_STALE_HOURS = 36

# Order is the pipeline order, so the dashboard reads as a flow rather than
# an alphabetical list.  must be here or the slowest stage in the
# system would be invisible on the page built to show what is happening.
QUEUES = ("metadata", "visuals", "frames", "vision", "transcribe", "media", "default")


class ComponentStatus(BaseModel):
    status: str  # ok | error | unconfigured
    detail: str | None = None


class VolumeStatus(BaseModel):
    label: str
    path: str
    total_gb: float
    free_gb: float
    used_percent: float
    # ok | low | full | unreachable
    status: str
    detail: str


class BackupStatus(BaseModel):
    status: str  # ok | stale | missing
    last_backup_at: datetime | None
    detail: str


class HealthReport(BaseModel):
    version: str
    database: ComponentStatus
    queue: ComponentStatus
    data_dir_free_gb: float | None
    volumes: list[VolumeStatus]
    backup: BackupStatus


@router.get("/health", response_model=HealthReport)
async def system_health(_user: CurrentUser, db: DbDep, settings: SettingsDep) -> HealthReport:
    try:
        await db.execute(text("SELECT 1"))
        database = ComponentStatus(status="ok")
    except Exception:  # pragma: no cover - depends on infra
        log.warning("health.database_error", exc_info=True)
        database = ComponentStatus(status="error", detail="Database is not reachable")

    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(  # type: ignore[no-untyped-call]
            settings.redis_url, socket_connect_timeout=2
        )
        try:
            await client.ping()
            queue = ComponentStatus(status="ok")
        finally:
            await client.aclose()
    except ModuleNotFoundError:
        queue = ComponentStatus(status="unconfigured", detail="Queue support not installed")
    except Exception:  # pragma: no cover - depends on infra
        log.warning("health.queue_error", exc_info=True)
        queue = ComponentStatus(status="error", detail="Job queue is not reachable")

    try:
        usage = shutil.disk_usage(settings.data_dir)
        free_gb: float | None = round(usage.free / 1024**3, 1)
    except OSError:
        free_gb = None

    return HealthReport(
        version=__version__,
        database=database,
        queue=queue,
        data_dir_free_gb=free_gb,
        volumes=await _volumes(db, settings),
        backup=_backup_status(settings),
    )


def _backup_status(settings: SettingsDep) -> BackupStatus:
    """The newest archive the backup service wrote into the data directory.

    Added after the install went eight weeks with no backup at all: the
    backup command existed and worked, and nothing ever ran it.
    """
    folder = Path(settings.data_dir) / "backups"
    try:
        archives = [(p.stat().st_mtime, p) for p in folder.glob("framefound-*.tar.gz")]
    except OSError:
        archives = []
    if not archives:
        return BackupStatus(
            status="missing",
            last_backup_at=None,
            detail=(
                "No automatic backup exists. The catalogue — names, tags, listings, "
                "edits — lives only in the database until one does."
            ),
        )
    newest = datetime.fromtimestamp(max(archives)[0], tz=UTC)
    hours = (datetime.now(UTC) - newest).total_seconds() / 3600
    if hours > BACKUP_STALE_HOURS:
        return BackupStatus(
            status="stale",
            last_backup_at=newest,
            detail=(
                f"The newest backup is {hours / 24:.0f} days old; the backup service has stopped."
            ),
        )
    return BackupStatus(status="ok", last_backup_at=newest, detail="")


async def _volumes(db: DbDep, settings: SettingsDep) -> list[VolumeStatus]:
    """Every volume FrameFound depends on, and whether it is actually there.

    Written when the install went from one disk to two. With a single volume
    "the disk is full" was unambiguous. With derivatives on one disk, the
    database on another and originals on a pair of network shares, the same
    condition now surfaces as unrelated-looking failures — proxies pausing,
    basemap extracts dying, a slideshow render failing partway — and no page
    said which disk ran out.

    A library root that has become unreachable is the more insidious case: the
    mount is gone, the directory it was mounted on still exists and is empty,
    and a scan reports every asset as missing. Distinguishing "the share is
    down" from "somebody deleted the footage" is the entire point of listing
    them here.

    Which this function once failed to do for 39 days: `disk_usage()` on an
    empty mountpoint succeeds — it measures whatever disk the directory sits
    on — so two unmounted NAS libraries reported as healthy under the VM's
    own root disk. Reachability is now asked directly (storage/reachability.py)
    before any space is measured.
    """
    from framefound.db.models import Library

    floor = settings.min_free_gb
    # The third element is how many files the catalogue holds there — it is
    # what makes an empty library folder alarming — or None for system disks.
    targets: list[tuple[str, Path, int | None]] = [
        ("Derivatives", Path(settings.data_dir), None),
        # The database's own volume is not mounted in this container, so it
        # used to be listed as "unreachable" on every load: a permanent false
        # alarm that teaches the page to be ignored. The container root sits
        # on the Docker disk, which is where the database volume lives.
        ("System disk (database)", Path("/"), None),
    ]
    counts: dict[uuid.UUID, int] = {
        library_id: count
        for library_id, count in (
            await db.execute(select(Asset.library_id, func.count()).group_by(Asset.library_id))
        ).all()
    }
    libraries = (await db.execute(select(Library).order_by(Library.name))).scalars().all()
    for library in libraries:
        targets.append((library.name, Path(library.root_path), int(counts.get(library.id, 0))))

    seen: set[str] = set()
    out: list[VolumeStatus] = []
    for label, path, known in targets:
        reason = unreachable_reason(path, known) if known is not None else None
        if reason is None:
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                reason = "This location cannot be read."
        if reason is not None:
            out.append(
                VolumeStatus(
                    label=label,
                    path=str(path),
                    total_gb=0.0,
                    free_gb=0.0,
                    used_percent=0.0,
                    status="unreachable",
                    detail=(
                        f"{reason} The catalogue is intact — search and thumbnails still "
                        "work — and everything recovers when the share comes back."
                    ),
                )
            )
            continue

        # Two libraries on one share report the same device; showing it twice
        # would imply two problems where there is one.
        key = f"{usage.total}:{usage.free}"
        if key in seen:
            continue
        seen.add(key)

        free_gb_here = usage.free / 1024**3
        used_percent = (
            round(100 * (usage.total - usage.free) / usage.total, 1) if usage.total else 0.0
        )
        if free_gb_here < floor:
            status, detail = (
                "full",
                (
                    f"Below the {floor:.0f} GB floor, so derivative generation is paused. "
                    "The catalogue and originals are unaffected."
                ),
            )
        elif free_gb_here < floor * 3:
            status, detail = "low", "Approaching the floor at which generation pauses."
        else:
            status, detail = "ok", ""

        out.append(
            VolumeStatus(
                label=label,
                path=str(path),
                total_gb=round(usage.total / 1024**3, 1),
                free_gb=round(free_gb_here, 1),
                used_percent=used_percent,
                status=status,
                detail=detail,
            )
        )
    return out


class Alert(BaseModel):
    level: str  # error | warning
    title: str
    detail: str
    href: str


@router.get("/alerts", response_model=list[Alert])
async def system_alerts(_user: CurrentUser, db: DbDep, settings: SettingsDep) -> list[Alert]:
    """What the operator needs to know now, shown on every page.

    The NAS outage of Aug-Sep 2026 lasted 39 days because every signal of it
    lived on pages nobody had reason to open. A problem that stops work gets
    a banner; the System page keeps the detail.
    """
    alerts: list[Alert] = []
    volumes = await _volumes(db, settings)

    down = [v.label for v in volumes if v.status == "unreachable"]
    if down:
        names = down[0] if len(down) == 1 else f"{', '.join(down[:-1])} and {down[-1]}"
        alerts.append(
            Alert(
                level="error",
                title=f"{names} {'is' if len(down) == 1 else 'are'} unreachable",
                detail=(
                    "The network share is probably not mounted. Search and thumbnails "
                    "still work; originals, edits, exports and new files wait for it."
                ),
                href="/health",
            )
        )
    full = [v.label for v in volumes if v.status == "full"]
    if full:
        alerts.append(
            Alert(
                level="error",
                title=f"{', '.join(full)} is out of space",
                detail="Previews and derivatives are paused until space is freed.",
                href="/health",
            )
        )
    backup = _backup_status(settings)
    if backup.status != "ok":
        alerts.append(
            Alert(
                level="warning",
                title="No recent backup" if backup.status == "stale" else "No backup yet",
                detail=backup.detail,
                href="/health",
            )
        )
    return alerts


class FailedJob(BaseModel):
    id: uuid.UUID
    task_name: str
    asset_id: uuid.UUID | None
    error: str | None
    started_at: datetime

    model_config = {"from_attributes": True}


class ProcessingReport(BaseModel):
    queue_depths: dict[str, int]  # live broker depths; -1 = broker unreachable
    assets_by_status: dict[str, int]
    derivatives: dict[str, dict[str, int]]  # kind -> status -> count
    jobs_last_hour: dict[str, int]  # status -> count
    recent_failures: list[FailedJob]


@router.get("/processing", response_model=ProcessingReport)
async def processing_report(
    _user: CurrentUser, db: DbDep, settings: SettingsDep
) -> ProcessingReport:
    depths: dict[str, int] = {}
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(  # type: ignore[no-untyped-call]
            settings.redis_url, socket_connect_timeout=2
        )
        try:
            for queue_name in QUEUES:
                depths[queue_name] = int(await client.llen(queue_name))
        finally:
            await client.aclose()
    except Exception:
        depths = dict.fromkeys(QUEUES, -1)

    assets_by_status = {
        status: count
        for status, count in (
            await db.execute(
                select(Asset.processing_status, func.count()).group_by(Asset.processing_status)
            )
        ).all()
    }
    derivatives: dict[str, dict[str, int]] = {}
    for kind, status, count in (
        await db.execute(
            select(Derivative.kind, Derivative.status, func.count()).group_by(
                Derivative.kind, Derivative.status
            )
        )
    ).all():
        derivatives.setdefault(kind, {})[status] = count

    hour_ago = text("now() - interval '1 hour'")
    try:
        jobs_last_hour = {
            status: count
            for status, count in (
                await db.execute(
                    select(Job.status, func.count())
                    .where(Job.started_at > hour_ago)
                    .group_by(Job.status)
                )
            ).all()
        }
    except Exception:  # SQLite in tests has no now()-interval; report all-time
        await db.rollback()
        jobs_last_hour = {
            status: count
            for status, count in (
                await db.execute(select(Job.status, func.count()).group_by(Job.status))
            ).all()
        }

    failures = (
        (
            await db.execute(
                select(Job).where(Job.status == "failed").order_by(Job.started_at.desc()).limit(20)
            )
        )
        .scalars()
        .all()
    )
    return ProcessingReport(
        queue_depths=depths,
        assets_by_status=assets_by_status,
        derivatives=derivatives,
        jobs_last_hour=jobs_last_hour,
        recent_failures=[FailedJob.model_validate(j) for j in failures],
    )
