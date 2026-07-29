"""Auth endpoints: first-run setup, login, logout, 2FA, session management.

CSRF posture: session cookie is SameSite=Lax + HttpOnly, which blocks
cross-site POSTs from carrying the cookie in modern browsers.
"""

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

from framefound.auth import service, totp
from framefound.auth.crypto import SecretUnavailable, seal, unseal
from framefound.auth.deps import (
    SESSION_COOKIE,
    CurrentUser,
    DbDep,
    SettingsDep,
    client_ip,
)
from framefound.auth.passwords import MIN_PASSWORD_LENGTH, verify_password
from framefound.auth.ratelimit import LoginRateLimiter
from framefound.db.models import User

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
    totp_code: str | None = Field(default=None, max_length=32)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    totp_enabled: bool = False

    model_config = {"from_attributes": True}

    @classmethod
    def of(cls, user: User) -> "UserOut":
        out = cls.model_validate(user)
        out.totp_enabled = bool(user.totp_secret)
        return out


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
    return UserOut.of(admin)


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

    if user.totp_secret:
        # Second factor required. A missing code is NOT a failed attempt —
        # counting it would let anyone lock out an account they know the
        # address of; a wrong code is counted.
        if not body.totp_code:
            raise HTTPException(
                401,
                "Enter the code from your authenticator app",
                headers={"X-FrameFound-Auth": "totp_required"},
            )
        if not _second_factor_ok(user, body.totp_code):
            login_limiter.record_failure(key)
            await service.audit(db, "auth.totp_failed", actor_user_id=user.id, ip=ip)
            await db.commit()
            raise HTTPException(401, "That code is not valid")

    login_limiter.record_success(key)
    token = await service.create_auth_session(
        db, user, settings, ip, request.headers.get("user-agent")
    )
    await service.audit(db, "auth.login", actor_user_id=user.id, ip=ip)
    await db.commit()
    _set_session_cookie(response, token, settings)
    return UserOut.of(user)


def _second_factor_ok(user: User, code: str) -> bool:
    """A valid TOTP code, or a single-use recovery code."""
    if user.totp_secret:
        try:
            if totp.verify(unseal(user.totp_secret), code):
                return True
        except SecretUnavailable:
            log.error("auth.totp_secret_unreadable", user_id=str(user.id))
            return False
    return service.consume_recovery_code(user, code)


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
    return UserOut.of(user)


# --------------------------------------------------------------------------
# Two-factor authentication
# --------------------------------------------------------------------------


class TotpStart(BaseModel):
    password: str = Field(max_length=128)


class TotpEnrolment(BaseModel):
    provisioning_uri: str
    secret: str  # shown once, for manual entry when a QR cannot be scanned


class TotpConfirm(BaseModel):
    code: str = Field(max_length=32)


class TotpDisable(BaseModel):
    password: str = Field(max_length=128)
    code: str = Field(max_length=32)


class RecoveryCodes(BaseModel):
    recovery_codes: list[str]


@router.post("/totp/start", response_model=TotpEnrolment)
async def totp_start(
    body: TotpStart, db: DbDep, user: CurrentUser, settings: SettingsDep
) -> TotpEnrolment:
    """Issue a pending secret. 2FA is not active until /totp/confirm."""
    if not verify_password(user.password_hash, body.password):
        raise HTTPException(403, "Password is incorrect")
    if user.totp_secret:
        raise HTTPException(409, "Two-factor authentication is already enabled")
    secret = totp.new_secret()
    user.totp_pending_secret = seal(secret)
    await db.commit()
    return TotpEnrolment(
        provisioning_uri=totp.provisioning_uri(secret, user.email, settings.server_name),
        secret=secret,
    )


@router.post("/totp/confirm", response_model=RecoveryCodes)
async def totp_confirm(
    body: TotpConfirm, request: Request, db: DbDep, user: CurrentUser
) -> RecoveryCodes:
    """Activate 2FA once the authenticator proves it works."""
    if not user.totp_pending_secret:
        raise HTTPException(409, "Start two-factor setup first")
    if not totp.verify(unseal(user.totp_pending_secret), body.code):
        raise HTTPException(400, "That code is not valid. Check your authenticator app.")

    codes = totp.new_recovery_codes()
    user.totp_secret = user.totp_pending_secret
    user.totp_pending_secret = None
    user.totp_recovery_hashes = [service.hash_recovery_code(c) for c in codes]
    await service.audit(db, "auth.totp_enabled", actor_user_id=user.id, ip=client_ip(request))
    await db.commit()
    # Returned exactly once — only hashes are stored.
    return RecoveryCodes(recovery_codes=codes)


@router.post("/totp/disable", status_code=204)
async def totp_disable(body: TotpDisable, request: Request, db: DbDep, user: CurrentUser) -> None:
    if not user.totp_secret:
        raise HTTPException(409, "Two-factor authentication is not enabled")
    if not verify_password(user.password_hash, body.password):
        raise HTTPException(403, "Password is incorrect")
    if not _second_factor_ok(user, body.code):
        raise HTTPException(400, "That code is not valid")
    user.totp_secret = None
    user.totp_pending_secret = None
    user.totp_recovery_hashes = []
    await service.audit(db, "auth.totp_disabled", actor_user_id=user.id, ip=client_ip(request))
    await db.commit()


# --------------------------------------------------------------------------
# Session management
# --------------------------------------------------------------------------


class SessionOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    ip: str | None
    user_agent: str | None
    current: bool = False

    model_config = {"from_attributes": True}


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(request: Request, db: DbDep, user: CurrentUser) -> list[SessionOut]:
    token = request.cookies.get(SESSION_COOKIE) or ""
    current_hash = service._hash_token(token) if token else None
    out = []
    for auth_session in await service.list_sessions(db, user.id):
        item = SessionOut.model_validate(auth_session)
        item.current = auth_session.token_hash == current_hash
        out.append(item)
    return out


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(
    session_id: uuid.UUID, request: Request, db: DbDep, user: CurrentUser
) -> None:
    if not await service.revoke_session_by_id(db, user.id, session_id):
        raise HTTPException(404, "No such session")
    await service.audit(db, "auth.session_revoked", actor_user_id=user.id, ip=client_ip(request))
    await db.commit()


@router.post("/sessions/revoke-others", status_code=200)
async def revoke_other_sessions(request: Request, db: DbDep, user: CurrentUser) -> dict[str, int]:
    """Sign out everywhere else — the fast response to a suspected leak."""
    token = request.cookies.get(SESSION_COOKIE)
    count = await service.revoke_all_sessions(db, user.id, except_token=token)
    await service.audit(
        db, "auth.sessions_revoked_all", actor_user_id=user.id, ip=client_ip(request)
    )
    await db.commit()
    return {"revoked": count}
