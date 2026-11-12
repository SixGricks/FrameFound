"""FastAPI application entrypoint.

Run: uvicorn framefound.main:app
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from framefound import __version__
from framefound.api.public_gate import PublicAccessGate
from framefound.api.v1.router import api_v1
from framefound.api.watchdog import LoopWatchdog
from framefound.config import get_settings
from framefound.errors import error_response, register_error_handlers
from framefound.logging import configure_logging
from framefound.storage.worker_roots import write_marker

log = structlog.get_logger()

# Where Claude connects: behind Caddy's existing /api/* route, the public
# gate and whatever tailnet or domain the web UI is reached on (docs/mcp.md).
MCP_PATH = "/api/mcp"
# Stateless, plain-JSON answers: nothing to time out behind a proxy, and no
# per-session state to lose on a container restart. DNS-rebinding
# protection is off because this always sits behind Caddy with the Host
# header the operator actually used, and the bearer token is the gate.
_MCP_TRANSPORT: dict[str, Any] = {
    "streamable_http_path": MCP_PATH,
    "json_response": True,
    "stateless_http": True,
}


def _mcp_server() -> Any:
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        from framefound.mcp.server import build_server
    except ImportError:
        log.warning("mcp.unavailable", hint="pip install 'framefound[mcp]' to enable the connector")
        return None
    _MCP_TRANSPORT["transport_security"] = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    return build_server()


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    if not settings.secret_key:
        log.warning("config.secret_key_missing", hint="set FRAMEFOUND_SECRET_KEY in .env")

    # The MCP connector (framefound/mcp) is optional at install time: without
    # the `mcp` extra the API runs exactly as before and says so once.
    mcp_server = _mcp_server()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Proves to a worker on another machine that the folder it sees is
        # this data store and not an empty mountpoint (storage/worker_roots.py).
        write_marker(settings.data_dir)
        # One event loop serves every request; the watchdog names whatever
        # blocks it (see api/watchdog.py).
        watchdog = LoopWatchdog()
        watchdog.start()
        try:
            if mcp_server is None:
                yield
            else:
                # The transport's session manager has to be running for the
                # mounted app to answer; a mounted app's own lifespan is
                # never entered by the parent, so it is entered here.
                async with mcp_server.session_manager.run():
                    yield
        finally:
            watchdog.stop()

    app = FastAPI(
        title="FrameFound API",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.add_middleware(PublicAccessGate)
    app.include_router(api_v1, prefix="/api/v1")
    if mcp_server is not None:
        from starlette.routing import Route

        from framefound.mcp.auth import BearerGate

        # A Route rather than a Mount: Starlette redirects a mount's bare path
        # to a trailing slash, and an MCP client will not follow a 307 on a
        # POST. The inner app routes on the same path, so nothing is
        # rewritten on the way through.
        gate = BearerGate(mcp_server.streamable_http_app(**_MCP_TRANSPORT))
        app.router.routes.append(Route(MCP_PATH, gate, include_in_schema=False))

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

            client = aioredis.from_url(  # type: ignore[no-untyped-call, unused-ignore]
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
