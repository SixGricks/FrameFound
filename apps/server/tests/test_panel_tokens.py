"""Panel tokens: minting, scope handling, and the ways one stops working.

A panel token is the only credential in this system that lives on a machine
rather than in a browser, so the properties worth pinning are the ones that
make it safe to hand out: it cannot be read back, it cannot outlive its
revocation, and it cannot do more than it was granted.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.auth import panel_tokens
from framefound.db.base import Base
from framefound.db.models import User


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncSession:  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def user(db: AsyncSession) -> User:
    person = User(email="dj@example.com", password_hash="x", role="admin")
    db.add(person)
    await db.commit()
    return person


# --- the secret -----------------------------------------------------------


async def test_the_token_is_returned_once_and_stored_only_as_a_hash(
    db: AsyncSession, user: User
) -> None:
    """A database leak must not expose a working credential."""
    minted = await panel_tokens.mint(db, user=user, name="Edit bay")
    await db.commit()
    assert minted.plaintext not in (minted.record.token_hash, minted.record.prefix)
    assert len(minted.record.token_hash) == 64
    assert minted.record.token_hash != minted.plaintext


async def test_the_token_is_identifiable_on_sight(db: AsyncSession, user: User) -> None:
    """So it is recognisable in a log or a config file, and by a secret
    scanner, rather than being an opaque string somebody pastes in public."""
    minted = await panel_tokens.mint(db, user=user, name="Edit bay")
    assert minted.plaintext.startswith(panel_tokens.TOKEN_PREFIX)


async def test_two_tokens_are_never_the_same(db: AsyncSession, user: User) -> None:
    a = await panel_tokens.mint(db, user=user, name="one")
    b = await panel_tokens.mint(db, user=user, name="two")
    await db.commit()
    assert a.plaintext != b.plaintext
    assert a.record.token_hash != b.record.token_hash


async def test_a_prefix_is_kept_so_a_list_can_be_told_apart(db: AsyncSession, user: User) -> None:
    minted = await panel_tokens.mint(db, user=user, name="Edit bay")
    assert minted.record.prefix and minted.plaintext.startswith(minted.record.prefix)
    # Short enough to be useless on its own.
    assert len(minted.record.prefix) <= panel_tokens.PREFIX_SHOWN


# --- resolving ------------------------------------------------------------


async def test_a_good_token_resolves_to_its_user(db: AsyncSession, user: User) -> None:
    minted = await panel_tokens.mint(db, user=user, name="Edit bay")
    await db.commit()
    assert (await panel_tokens.resolve(db, minted.plaintext)) is not None


async def test_a_revoked_token_stops_working(db: AsyncSession, user: User) -> None:
    minted = await panel_tokens.mint(db, user=user, name="Edit bay")
    await db.commit()
    minted.record.revoked = True
    await db.commit()
    assert (await panel_tokens.resolve(db, minted.plaintext)) is None


async def test_an_expired_token_stops_working(db: AsyncSession, user: User) -> None:
    minted = await panel_tokens.mint(
        db, user=user, name="Temp", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    await db.commit()
    assert (await panel_tokens.resolve(db, minted.plaintext)) is None


async def test_a_token_with_no_expiry_keeps_working(db: AsyncSession, user: User) -> None:
    """An edit bay used twice a year must not silently stop working."""
    minted = await panel_tokens.mint(db, user=user, name="Edit bay", expires_at=None)
    await db.commit()
    assert (await panel_tokens.resolve(db, minted.plaintext)) is not None


@pytest.mark.parametrize(
    "bogus", ["", "nonsense", "Bearer x", "ffp_", "ffp_wrong", "sk_live_something"]
)
async def test_a_token_that_was_never_issued_is_refused(
    db: AsyncSession, user: User, bogus: str
) -> None:
    await panel_tokens.mint(db, user=user, name="Edit bay")
    await db.commit()
    assert (await panel_tokens.resolve(db, bogus)) is None


async def test_use_is_recorded_so_stale_tokens_can_be_spotted(db: AsyncSession, user: User) -> None:
    """A token untouched for six months is one the operator can revoke without
    wondering what it would break."""
    minted = await panel_tokens.mint(db, user=user, name="Edit bay")
    await db.commit()
    assert minted.record.last_used_at is None
    await panel_tokens.resolve(db, minted.plaintext, ip="192.168.1.50")
    await db.commit()
    assert minted.record.last_used_at is not None
    assert minted.record.last_used_ip == "192.168.1.50"


# --- scopes ---------------------------------------------------------------


async def test_read_is_always_granted(db: AsyncSession, user: User) -> None:
    """A token with no scope would authenticate and then be refused by every
    endpoint, which reads as a broken panel rather than a permissions choice."""
    minted = await panel_tokens.mint(db, user=user, name="Edit bay", scopes=[])
    assert panel_tokens.has_scope(minted.record, panel_tokens.READ)


async def test_export_is_opt_in(db: AsyncSession, user: User) -> None:
    """Finding footage and streaming a proxy is one thing; producing a file the
    host application will act on is a step further."""
    default = await panel_tokens.mint(db, user=user, name="Reader")
    assert not panel_tokens.has_scope(default.record, panel_tokens.EXPORT)

    wider = await panel_tokens.mint(db, user=user, name="Editor", scopes=["export"])
    assert panel_tokens.has_scope(wider.record, panel_tokens.EXPORT)


async def test_an_unknown_scope_is_refused_rather_than_ignored(
    db: AsyncSession, user: User
) -> None:
    """Silently dropping it would mint a token the operator believes is more
    capable than it is."""
    with pytest.raises(panel_tokens.PanelTokenError, match="admin"):
        await panel_tokens.mint(db, user=user, name="Sneaky", scopes=["admin"])


async def test_no_scope_grants_writing_to_the_library() -> None:
    """The whole scope vocabulary is a promise about what a stolen token cannot
    do. If this list ever grows a write, that promise needs re-reading."""
    assert set(panel_tokens.SCOPES) == {"read", "export"}


async def test_scopes_are_stored_normalised(db: AsyncSession, user: User) -> None:
    minted = await panel_tokens.mint(db, user=user, name="x", scopes=["EXPORT", " read "])
    assert minted.record.scopes == "export,read"


# --- host label -----------------------------------------------------------


async def test_an_unrecognised_host_falls_back_rather_than_being_stored(
    db: AsyncSession, user: User
) -> None:
    """`host` is a label for the operator, never a permission — so an odd value
    must not become one."""
    minted = await panel_tokens.mint(db, user=user, name="x", host="'; drop table --")
    assert minted.record.host == "other"


@pytest.mark.parametrize("host", ["premiere", "lightroom", "other"])
async def test_known_hosts_are_kept(db: AsyncSession, user: User, host: str) -> None:
    minted = await panel_tokens.mint(db, user=user, name="x", host=host)
    assert minted.record.host == host
