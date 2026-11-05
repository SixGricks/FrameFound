"""Who is calling the connector, and what they may do.

A panel token in the Authorization header, exactly as the Premiere panel
and the Lightroom plugin send it. The MCP SDK ships its own OAuth
machinery; it is not used here, because FrameFound already has a
credential that is minted, listed and revoked on the Security page, and
a second kind of credential would be a second thing to leak.

The gate runs before the MCP transport sees the request. It resolves the
token once, records last-used, and leaves two things for the tools to
pick up from the request's context: the principal (who, with which
scopes) and the database session factory — the latter so that the test
suite's in-memory override reaches the tools the same way it reaches the
API.
"""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import cast

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from framefound.auth import panel_tokens
from framefound.auth.deps import bearer_token, client_ip
from framefound.db.engine import get_session
from framefound.errors import error_response

log = structlog.get_logger()

SessionFactory = Callable[[], AsyncIterator[AsyncSession]]


@dataclass(frozen=True)
class Principal:
    """The token's owner, and what the token lets it do."""

    user_id: uuid.UUID
    email: str
    role: str
    token_name: str
    scopes: frozenset[str]
    # scheme://host the caller used to reach us, so links back to media
    # (previews, proxies) are ones the same caller can open.
    base_url: str

    @property
    def can_tag(self) -> bool:
        # A read-only account's token must not write through the connector
        # what the account could not write in a browser.
        return panel_tokens.TAG in self.scopes and self.role != "readonly"


class NotAuthenticated(RuntimeError):
    pass


_principal: ContextVar[Principal | None] = ContextVar("framefound_mcp_principal", default=None)
_sessions: ContextVar[SessionFactory | None] = ContextVar("framefound_mcp_sessions", default=None)


def principal() -> Principal:
    found = _principal.get()
    if found is None:
        raise NotAuthenticated("No principal in this request")
    return found


def _open(factory: SessionFactory) -> AsyncGenerator[AsyncSession, None]:
    """The dependency is declared as an iterator; it is in fact a generator,
    which is what lets it be closed early when a tool call fails."""
    return cast(AsyncGenerator[AsyncSession, None], factory())


@asynccontextmanager
async def open_session() -> AsyncIterator[AsyncSession]:
    """One database session for the tool call, from whatever factory the
    gate recorded — the real engine, or a test's override."""
    stream = _open(_sessions.get() or get_session)
    db = await anext(stream)
    try:
        yield db
    finally:
        await stream.aclose()


def bind(principal_value: Principal, factory: SessionFactory) -> None:
    """For tests and in-process callers: what the gate would have set."""
    _principal.set(principal_value)
    _sessions.set(factory)


def _base_url(request: Request) -> str:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}"


class BearerGate:
    """ASGI wrapper: no valid panel token, no MCP."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        # The root FastAPI app, whose dependency overrides the tests use to
        # point everything at one in-memory database.
        root = scope.get("app")
        overrides = getattr(root, "dependency_overrides", {})
        factory: SessionFactory = overrides.get(get_session, get_session)

        found: Principal | None = None
        token = bearer_token(request)
        if token:
            stream = _open(factory)
            db = await anext(stream)
            try:
                user = await panel_tokens.resolve(db, token, ip=client_ip(request))
                record = await panel_tokens.resolve_record(db, token) if user else None
                if user is not None and record is not None:
                    await db.commit()  # persist last-used
                    found = Principal(
                        user_id=user.id,
                        email=user.email,
                        role=user.role,
                        token_name=record.name or record.prefix,
                        scopes=frozenset(s for s in record.scopes.split(",") if s),
                        base_url=_base_url(request),
                    )
            finally:
                await stream.aclose()
        if found is None:
            log.info("mcp.unauthenticated", path=request.url.path)
            response = error_response(
                401,
                "A FrameFound panel token is required: Authorization: Bearer ffp_…",
                headers={"WWW-Authenticate": 'Bearer realm="FrameFound MCP"'},
            )
            await response(scope, receive, send)
            return

        token_p = _principal.set(found)
        token_s = _sessions.set(factory)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(token_p)
            _sessions.reset(token_s)
