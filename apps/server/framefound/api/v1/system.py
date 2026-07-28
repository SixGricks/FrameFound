"""System status endpoints (authenticated)."""

import shutil
import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select, text

from framefound import __version__
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep
from framefound.db.models import Asset, Derivative, Job

log = structlog.get_logger()
router = APIRouter(prefix="/system", tags=["system"])

QUEUES = ("visuals", "metadata", "media", "transcribe", "vision", "default")


class ComponentStatus(BaseModel):
    status: str  # ok | error | unconfigured
    detail: str | None = None


class HealthReport(BaseModel):
    version: str
    database: ComponentStatus
    queue: ComponentStatus
    data_dir_free_gb: float | None


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
        version=__version__, database=database, queue=queue, data_dir_free_gb=free_gb
    )


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
