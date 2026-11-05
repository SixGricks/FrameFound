"""The AI spend ledger and the monthly cap.

What matters here is that the cap is enforced *before* the call that would
cross it, from what earlier calls actually cost — and that an unknown model
is never priced as free.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import spend
from framefound.db.base import Base


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncSession:  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 's.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


# --- pricing --------------------------------------------------------------


def test_a_call_is_priced_from_the_tokens_the_api_reported() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert spend.price_of("claude-haiku-5-5", usage) == pytest.approx(0.10)
    assert spend.price_of("claude-opus-5-5", usage) == pytest.approx(4.0)


def test_the_longest_prefix_wins() -> None:
    """ "claude-sonnet-5-5" must not be priced as "claude-sonnet-5" because
    that key happens to be shorter and come first."""
    usage = {"input_tokens": 1_000_000}
    assert spend.price_of("claude-sonnet-5-5", usage) == pytest.approx(
        spend.PRICES["claude-sonnet-5-5"][0]
    )
    assert spend.price_of("claude-haiku-5-5", usage) != spend.price_of("claude-haiku-4-5", usage)


def test_cache_reads_are_priced_at_the_cache_rate() -> None:
    usage = {"input_tokens": 0, "cache_read_input_tokens": 1_000_000}
    assert spend.price_of("claude-sonnet-5", usage) == pytest.approx(0.20)


def test_an_unknown_model_has_no_price_but_a_dear_estimate() -> None:
    """The ledger records the call with cost unknown; the *estimate* used to
    decide whether the next call fits assumes the dearest listed rate, so
    a cap can never be walked past by a model the table has not heard of."""
    assert spend.price_of("claude-future-9", {"input_tokens": 1}) is None
    dearest = max(spend.estimate_call(model) for model in spend.PRICES)
    assert spend.estimate_call("claude-future-9") == pytest.approx(dearest)


def test_the_estimate_is_cheaper_for_haiku_than_opus() -> None:
    assert spend.estimate_call("claude-haiku-5-5") < spend.estimate_call("claude-opus-5-5")


def test_the_month_starts_on_the_first() -> None:
    when = datetime(2026, 11, 5, 14, 30, tzinfo=UTC)
    assert spend.month_start(when) == datetime(2026, 11, 1, tzinfo=UTC)


# --- the ledger -----------------------------------------------------------


async def test_recorded_calls_add_up(db: AsyncSession) -> None:
    for _ in range(3):
        await spend.record(
            db,
            purpose=spend.TAG_REVIEW,
            model="claude-haiku-5-5",
            usage={"input_tokens": 1_000_000},
        )
    await db.commit()
    assert await spend.spent_this_month(db) == pytest.approx(0.30)
    assert await spend.spent_this_month(db, spend.RECIPE) == 0.0


async def test_a_failed_call_costs_nothing(db: AsyncSession) -> None:
    """No usage comes back from a refused request, and the API does not bill
    one — so neither does the ledger."""
    row = await spend.record(
        db, purpose=spend.TAG_REVIEW, model="claude-haiku-5-5", usage=None, ok=False, note="529"
    )
    await db.commit()
    assert row.cost_usd is None
    assert await spend.spent_this_month(db) == 0.0


async def test_last_months_spend_does_not_count(db: AsyncSession) -> None:
    row = await spend.record(
        db, purpose=spend.RECIPE, model="claude-opus-5-5", usage={"input_tokens": 1_000_000}
    )
    row.created_at = spend.month_start() - timedelta(days=1)
    await db.commit()
    assert await spend.spent_this_month(db) == 0.0


# --- the cap --------------------------------------------------------------


async def test_the_cap_refuses_the_call_that_would_cross_it(db: AsyncSession) -> None:
    await spend.record(
        db, purpose=spend.RECIPE, model="claude-opus-5-5", usage={"input_tokens": 1_000_000}
    )
    await db.commit()  # $4.00 spent
    budget = await spend.check_budget(db, cap_usd=5.0, estimate=0.5)
    assert budget.remaining_usd == pytest.approx(1.0)
    with pytest.raises(spend.BudgetExceeded) as caught:
        await spend.check_budget(db, cap_usd=5.0, estimate=1.5)
    assert "$4.00 of $5.00" in str(caught.value)


async def test_a_cap_of_zero_pauses_everything(db: AsyncSession) -> None:
    with pytest.raises(spend.BudgetExceeded):
        await spend.check_budget(db, cap_usd=0.0, estimate=0.0001)
