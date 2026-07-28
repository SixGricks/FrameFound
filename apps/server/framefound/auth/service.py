"""Auth domain logic — pure functions over a session; no HTTP concerns."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.auth.passwords import hash_password, verify_password
from framefound.config import Settings
from framefound.db.models import ROLES, AppSetting, AuditLog, AuthSession, User

SETUP_CONSUMED_KEY = "setup_token_consumed"


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def count_users(db: AsyncSession) -> int:
    return (await db.execute(select(func.count(User.id)))).scalar_one()


async def create_user(db: AsyncSession, email: str, password: str, role: str) -> User:
    if role not in ROLES:
        raise ValueError(f"invalid role: {role}")
    user = User(email=email.lower(), password_hash=hash_password(password), role=role)
    db.add(user)
    await db.flush()
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower()))
    user = result.scalar_one_or_none()
    if user is None:
        # Hash anyway so response timing doesn't reveal whether the email exists.
        verify_password(hash_password("timing-equalizer"), password)
        return None
    if user.disabled or not verify_password(user.password_hash, password):
        return None
    return user


async def create_auth_session(
    db: AsyncSession,
    user: User,
    settings: Settings,
    ip: str | None,
    user_agent: str | None,
) -> str:
    """Create a session row and return the opaque token for the cookie."""
    token = secrets.token_urlsafe(32)
    now = _now()
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=_hash_token(token),
            expires_at=now + timedelta(minutes=settings.session_idle_minutes),
            absolute_expires_at=now + timedelta(hours=settings.session_absolute_hours),
            ip=ip,
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    user.last_login_at = now
    await db.flush()
    return token


async def resolve_session(db: AsyncSession, token: str, settings: Settings) -> User | None:
    """Validate a cookie token; slide the idle expiry forward on use."""
    result = await db.execute(
        select(AuthSession).where(AuthSession.token_hash == _hash_token(token))
    )
    auth_session = result.scalar_one_or_none()
    if auth_session is None or auth_session.revoked:
        return None
    now = _now()
    expires_at = auth_session.expires_at
    absolute = auth_session.absolute_expires_at
    # SQLite returns naive datetimes; our columns are always stored as UTC.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
        absolute = absolute.replace(tzinfo=UTC)
    if now >= expires_at or now >= absolute:
        return None
    auth_session.expires_at = min(now + timedelta(minutes=settings.session_idle_minutes), absolute)
    user = await db.get(User, auth_session.user_id)
    if user is None or user.disabled:
        return None
    return user


async def revoke_session(db: AsyncSession, token: str) -> None:
    result = await db.execute(
        select(AuthSession).where(AuthSession.token_hash == _hash_token(token))
    )
    auth_session = result.scalar_one_or_none()
    if auth_session is not None:
        auth_session.revoked = True


async def setup_token_consumed(db: AsyncSession) -> bool:
    return await db.get(AppSetting, SETUP_CONSUMED_KEY) is not None


async def consume_setup_token(db: AsyncSession) -> None:
    db.add(AppSetting(key=SETUP_CONSUMED_KEY, value={"at": _now().isoformat()}))


def verify_setup_token(provided: str, expected: str) -> bool:
    return bool(expected) and secrets.compare_digest(provided, expected)


async def audit(
    db: AsyncSession,
    event: str,
    actor_user_id: uuid.UUID | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(AuditLog(event=event, actor_user_id=actor_user_id, ip=ip, detail=detail or {}))
