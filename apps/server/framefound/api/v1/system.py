"""System status endpoints (authenticated)."""

import shutil

import structlog
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from framefound import __version__
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep

log = structlog.get_logger()
router = APIRouter(prefix="/system", tags=["system"])


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
