"""FastAPI dependencies for authentication and role-based authorization."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, params
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.auth import service
from framefound.auth.client_address import resolve_client_ip
from framefound.config import Settings, get_settings
from framefound.db.engine import get_session
from framefound.db.models import User

SESSION_COOKIE = "framefound_session"

DbDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def client_ip(request: Request) -> str | None:
    """Client address, spoof-resistant (see auth/client_address.py)."""
    return resolve_client_ip(request, get_settings().trusted_proxies)


async def get_current_user(request: Request, db: DbDep, settings: SettingsDep) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Not signed in")
    user = await service.resolve_session(db, token, settings)
    if user is None:
        raise HTTPException(401, "Session expired or invalid")
    await db.commit()  # persist the sliding-expiry update
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def bearer_token(request: Request) -> str | None:
    """The panel token from an Authorization header, if there is one."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def get_panel_principal(request: Request, db: DbDep, settings: SettingsDep) -> User:
    """A user authenticated by *either* a browser session or a panel token.

    Both are accepted on the panel endpoints so the same URLs can be developed
    against from a logged-in browser and used from Premiere, which is the
    difference between a surface that can be debugged and one that can only be
    guessed at.

    The token path is checked first: a developer with both a session cookie and
    a token in the header is testing the token, and silently authenticating
    them by cookie would make a broken token look like a working one.
    """
    from framefound.auth import panel_tokens

    token = bearer_token(request)
    if token:
        user = await panel_tokens.resolve(db, token, ip=client_ip(request))
        if user is None:
            raise HTTPException(401, "That panel token is not valid")
        await db.commit()  # persist last-used
        return user
    return await get_current_user(request, db, settings)


PanelPrincipal = Annotated[User, Depends(get_panel_principal)]


def require_panel_scope(scope: str) -> params.Depends:
    """Enforce a scope, but only against callers who actually used a token.

    A signed-in operator in a browser is not scope-limited — scopes exist to
    make a *token* weaker than the person who issued it, not to take authority
    away from that person.
    """

    async def _check(request: Request, db: DbDep) -> None:
        from framefound.auth import panel_tokens

        token = bearer_token(request)
        if token is None:
            return
        record = await panel_tokens.resolve_record(db, token)
        if record is None or not panel_tokens.has_scope(record, scope):
            raise HTTPException(403, f"This panel token does not have the '{scope}' scope")

    dependency: params.Depends = Depends(_check)
    return dependency


def require_role(*roles: str) -> params.Depends:
    async def _check(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(403, "You do not have permission to do this")
        return user

    dependency: params.Depends = Depends(_check)
    return dependency


require_admin = require_role("admin")
