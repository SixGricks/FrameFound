"""Public-access gate.

When public access is off, requests arriving from outside the LAN or tailnet
are refused before they reach any handler. This is defence in depth, not the
primary control — the reverse proxy and router still decide what is reachable
at all — but it takes effect on the next request, with no proxy reload and no
waiting for DNS to propagate. That makes it a usable panic button.

Health endpoints stay open so container and uptime checks keep working.
"""

import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from framefound.api.v1.remote_access import classify_client
from framefound.auth.deps import client_ip
from framefound.errors import error_response

log = structlog.get_logger()

ALWAYS_ALLOWED = ("/healthz", "/readyz")
# The gate reads a database row; cache it briefly so a hot path does not add a
# query per request. A few seconds of staleness is acceptable for a control
# whose purpose is "stop this within a minute", not "within a millisecond".
CACHE_SECONDS = 5.0


class PublicAccessGate(BaseHTTPMiddleware):
    def __init__(self, app: object) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._allow_public = True
        self._checked_at = 0.0

    async def _public_allowed(self) -> bool:
        now = time.monotonic()
        if now - self._checked_at < CACHE_SECONDS:
            return self._allow_public
        try:
            from framefound.db.engine import session_factory
            from framefound.ddns import settings_store as store

            async with session_factory()() as db:
                config = await store.load_config(db)
                self._allow_public = config.public_access_enabled
        except Exception:
            # If configuration cannot be read, do not lock anyone out — the
            # network layer is still the primary control.
            self._allow_public = True
        self._checked_at = now
        return self._allow_public

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in ALWAYS_ALLOWED:
            return await call_next(request)
        origin = classify_client(client_ip(request))
        if origin == "internet" and not await self._public_allowed():
            log.warning("public_gate.blocked", path=request.url.path)
            return error_response(403, "Remote access is turned off for this server")
        return await call_next(request)
