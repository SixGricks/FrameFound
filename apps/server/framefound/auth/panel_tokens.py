"""Minting and checking panel tokens.

A panel token is a bearer credential for a *machine* — the Premiere panel, the
Lightroom plugin, a script on the edit bay. The design follows the same rules
the session layer already established, for the same reasons:

**Only a hash is stored.** A database leak exposes no working credentials.

**SHA-256, not Argon2.** Passwords are hashed slowly because humans choose
guessable ones and the attacker's advantage is a dictionary. A token is 256
bits from a CSPRNG: there is no dictionary, nothing to guess, and no
rate-limiting benefit worth paying ~100 ms of Argon2 on every request a panel
makes. The session layer reached the same conclusion.

**Read-only by default.** The panel's job is to find footage and hand it to an
editor. It never needs to write to the catalogue and it must never be able to
touch an original, so the default scope grants neither, and the widest scope
offered still grants neither.

**Revocable, and visibly so.** Every token is listed with its prefix, its host,
and when it was last used, next to the sessions on the Security page. A
credential that cannot be revoked from the machine it grants access to is a
leak with a delay on it.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import PanelToken, User

# Identifies the credential on sight — in a log, a config file, or a secret
# scanner. Worth having: an unidentifiable opaque string gets pasted somewhere
# public because nobody recognised what it was.
TOKEN_PREFIX = "ffp_"  # noqa: S105 - a public marker, not a secret
# 32 bytes url-safe. Same strength as a session token.
TOKEN_BYTES = 32
PREFIX_SHOWN = 10

READ = "read"
EXPORT = "export"
# The complete set. Deliberately short: every scope here is a promise about
# what a stolen token cannot do, and a long list makes that promise unreadable.
SCOPES = (READ, EXPORT)


class PanelTokenError(RuntimeError):
    pass


@dataclass(frozen=True)
class MintedToken:
    """The one and only time the plaintext exists outside the client."""

    record: PanelToken
    plaintext: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalise_scopes(scopes: list[str] | None) -> str:
    """Validated, de-duplicated, always including read.

    A token with no scope at all would authenticate and then be refused by
    every endpoint, which reads as a broken panel rather than a permissions
    mistake.
    """
    wanted = {s.strip().lower() for s in (scopes or []) if s.strip()}
    unknown = wanted - set(SCOPES)
    if unknown:
        raise PanelTokenError(f"Unknown scope: {', '.join(sorted(unknown))}")
    wanted.add(READ)
    return ",".join(sorted(wanted))


def has_scope(token: PanelToken, scope: str) -> bool:
    return scope in token.scopes.split(",")


async def mint(
    db: AsyncSession,
    *,
    user: User,
    name: str,
    host: str = "other",
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
) -> MintedToken:
    """Create a token. The plaintext is returned once and never recoverable."""
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    record = PanelToken(
        user_id=user.id,
        name=name.strip()[:120],
        token_hash=_hash(plaintext),
        prefix=plaintext[:PREFIX_SHOWN],
        host=host if host in ("premiere", "lightroom", "other") else "other",
        scopes=normalise_scopes(scopes),
        expires_at=expires_at,
    )
    db.add(record)
    await db.flush()
    return MintedToken(record=record, plaintext=plaintext)


async def resolve(db: AsyncSession, token: str, *, ip: str | None = None) -> User | None:
    """The user behind a bearer token, or None if it is not usable.

    Every rejection returns None rather than explaining itself. A caller
    holding a revoked token and a caller holding a made-up one learn the same
    thing, which is nothing.
    """
    if not token or not token.startswith(TOKEN_PREFIX):
        return None

    record = (
        await db.execute(select(PanelToken).where(PanelToken.token_hash == _hash(token)))
    ).scalar_one_or_none()
    if record is None or record.revoked:
        return None
    if record.expires_at is not None and _aware(record.expires_at) <= datetime.now(UTC):
        return None

    user = await db.get(User, record.user_id)
    if user is None:
        return None

    # Last-used is what makes the Security page actionable: a token that has
    # not been touched in six months is one the operator can revoke without
    # wondering what it would break.
    record.last_used_at = datetime.now(UTC)
    record.last_used_ip = ip
    return user


async def resolve_record(db: AsyncSession, token: str) -> PanelToken | None:
    """The token row itself, for scope checks after authentication."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    return (
        await db.execute(select(PanelToken).where(PanelToken.token_hash == _hash(token)))
    ).scalar_one_or_none()


def _aware(when: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres does not."""
    return when if when.tzinfo else when.replace(tzinfo=UTC)
