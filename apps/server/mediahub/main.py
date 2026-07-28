"""FastAPI application entrypoint.

Run: uvicorn mediahub.main:app
"""

import structlog
from fastapi import FastAPI

from mediahub import __version__
from mediahub.api.v1.router import api_v1

log = structlog.get_logger()


def create_app() -> FastAPI:
    app = FastAPI(
        title="MediaHub API",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.include_router(api_v1, prefix="/api/v1")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up. Used by Docker healthchecks."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, str]:
        # TODO(m1): check database and redis connectivity before reporting ready.
        return {"status": "ok"}

    return app


app = create_app()
