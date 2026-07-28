"""Auth endpoints: first-run setup, login, logout, current user.

CSRF posture: session cookie is SameSite=Lax + HttpOnly, which blocks
cross-site POSTs from carrying the cookie in modern browsers. Re-evaluated
before remote access ships (M7 hardening checklist).
"""

import uuid

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

from framefound.auth import service
from framefound.auth.deps import (
    SESSION_COOKIE,
    CurrentUser,
    DbDep,
    SettingsDep,
    client_ip,
)
from framefound.auth.passwords import MIN_PASSWORD_LENGTH
from framefound.auth.ratelimit import LoginRateLimiter

log = structlog.get_logger()
router = APIRouter(prefix="/auth", tags=["auth"])

login_limiter = LoginRateLimiter()


class SetupRequest(BaseModel):
    setup_token: str
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str

    model_config = {"from_attributes": True}


def _set_session_cookie(response: Response, token: str, settings: SettingsDep) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_absolute_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


@router.post("/setup", response_model=UserOut, status_code=201)
async def first_run_setup(
    body: SetupRequest, request: Request, response: Response, db: DbDep, settings: SettingsDep
) -> UserOut:
    """Create the first administrator. Requires the one-time setup token
    printed by the installer; the token is consumed on success."""
    ip = client_ip(request)
    if not service.verify_setup_token(body.setup_token, settings.setup_token):
        await service.audit(db, "setup.invalid_token", ip=ip)
        await db.commit()
        raise HTTPException(403, "Invalid setup token")
    if await service.setup_token_consumed(db) or await service.count_users(db) > 0:
        raise HTTPException(409, "Setup has already been completed")

    admin = await service.create_user(db, body.email, body.password, role="admin")
    await service.consume_setup_token(db)
    token = await service.create_auth_session(
        db, admin, settings, ip, request.headers.get("user-agent")
    )
    await service.audit(db, "setup.admin_created", actor_user_id=admin.id, ip=ip)
    await db.commit()
    _set_session_cookie(response, token, settings)
    log.info("setup.admin_created")
    return UserOut.model_validate(admin)


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginRequest, request: Request, response: Response, db: DbDep, settings: SettingsDep
) -> UserOut:
    ip = client_ip(request)
    key = f"{ip}:{body.email.lower()}"
    wait = login_limiter.retry_after(key)
    if wait > 0:
        raise HTTPException(
            429,
            "Too many failed sign-in attempts. Try again shortly.",
            headers={"Retry-After": str(int(wait) + 1)},
        )

    user = await service.authenticate(db, body.email, body.password)
    if user is None:
        login_limiter.record_failure(key)
        await service.audit(db, "auth.login_failed", ip=ip, detail={"email": body.email.lower()})
        await db.commit()
        # Same message whether the email or the password was wrong.
        raise HTTPException(401, "Incorrect email or password")

    login_limiter.record_success(key)
    token = await service.create_auth_session(
        db, user, settings, ip, request.headers.get("user-agent")
    )
    await service.audit(db, "auth.login", actor_user_id=user.id, ip=ip)
    await db.commit()
    _set_session_cookie(response, token, settings)
    return UserOut.model_validate(user)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: DbDep, user: CurrentUser) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await service.revoke_session(db, token)
    await service.audit(db, "auth.logout", actor_user_id=user.id, ip=client_ip(request))
    await db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
