"""FastAPI application entrypoint.

Run: uvicorn framefound.main:app
"""

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from framefound import __version__
from framefound.api.public_gate import PublicAccessGate
from framefound.api.v1.router import api_v1
from framefound.config import get_settings
from framefound.errors import error_response, register_error_handlers
from framefound.logging import configure_logging

log = structlog.get_logger()


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    if not settings.secret_key:
        log.warning("config.secret_key_missing", hint="set FRAMEFOUND_SECRET_KEY in .env")

    app = FastAPI(
        title="FrameFound API",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    register_error_handlers(app)
    app.add_middleware(PublicAccessGate)
    app.include_router(api_v1, prefix="/api/v1")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up. Used by Docker healthchecks."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        """Readiness: dependencies reachable. Checked before routing traffic."""
        problems: list[str] = []
        try:
            from sqlalchemy import text

            from framefound.db.engine import get_engine

            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            log.warning("readyz.database_unreachable", exc_info=True)
            problems.append("database")
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(  # type: ignore[no-untyped-call]
                settings.redis_url, socket_connect_timeout=2
            )
            try:
                await client.ping()
            finally:
                await client.aclose()
        except ModuleNotFoundError:
            pass  # queue extra not installed (unit-test environments)
        except Exception:
            log.warning("readyz.queue_unreachable", exc_info=True)
            problems.append("queue")

        if problems:
            return error_response(503, "Not ready", detail={"unreachable": problems})
        return JSONResponse({"status": "ok"})

    return app


app = create_app()
