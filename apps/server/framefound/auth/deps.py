"""FastAPI dependencies for authentication and role-based authorization."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, params
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.auth import service
from framefound.config import Settings, get_settings
from framefound.db.engine import get_session
from framefound.db.models import User

SESSION_COOKIE = "framefound_session"

DbDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def client_ip(request: Request) -> str | None:
    # TODO(m7): honor X-Forwarded-For only from configured trusted proxies
    # (threat model: correct client IP handling). Direct peer until then.
    return request.client.host if request.client else None


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


def require_role(*roles: str) -> params.Depends:
    async def _check(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(403, "You do not have permission to do this")
        return user

    dependency: params.Depends = Depends(_check)
    return dependency


require_admin = require_role("admin")
