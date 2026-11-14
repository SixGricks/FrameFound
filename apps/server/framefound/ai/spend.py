"""What the Claude API costs this install, and the ceiling it may not cross.

Every call FrameFound makes to Anthropic — the recipe-picker, photo naming,
the tag reviewer, the chat panel — lands in one ledger with the token counts the API itself
reported. The budget check reads that ledger, so the number the operator
sets on the Security page is enforced from measured spend, not from a guess
at the prompt size.

The cap is monthly and deliberately simple: a dollar figure, reset on the
first of the month. A run that would cross it stops *before* the call that
would do so, and says why. Nothing here is a billing system; the prices are
a dated snapshot and the ledger is for making the ceiling real, not for
reconciling an invoice.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import AiUsage

# $ per million tokens: input, output, 5-minute cache write, cache read.
# From platform.claude.com/docs/en/about-claude/pricing — a dated snapshot
# (2026-11-05); the token counts are the measurement, the prices are the
# conversion. A model not listed here is still called and recorded, with
# an unknown cost, and the budget check treats unknown as the most
# expensive listed rate rather than as free.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.10),
    "claude-sonnet-5-5": (2.0, 10.0, 2.50, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 2.50, 0.20),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 6.25, 0.50),
    "claude-fable-5-1": (10.0, 50.0, 12.50, 0.25),
}

# Purposes, for the ledger and the Security page's breakdown.
TAG_REVIEW = "tag_review"
RECIPE = "recipe"
DESCRIBE = "describe"
# The chat panel (framefound/chat). Its rows never carry an asset id: the
# tag reviewer reads "a tag_review row with an asset" as "already reviewed".
CHAT = "chat"

# A typical reviewer call: a 768px preview (~600 image tokens), the tag
# vocabulary and instructions, and a short structured answer. Used only
# to decide whether the *next* call fits under the cap; the ledger records
# what it actually cost afterwards.
ESTIMATE_INPUT_TOKENS = 1_800
ESTIMATE_OUTPUT_TOKENS = 400


def _price_row(model: str) -> tuple[float, float, float, float] | None:
    """Longest matching prefix: "claude-sonnet-5-5" must not be priced as
    "claude-sonnet-5" just because that key is shorter."""
    matches = [key for key in PRICES if model.startswith(key)]
    if not matches:
        return None
    return PRICES[max(matches, key=len)]


def price_of(model: str, usage: dict[str, int]) -> float | None:
    """Dollars for one call, from the usage block the API returned.
    None when the model is not in the price table."""
    row = _price_row(model)
    if row is None:
        return None
    inp, out, write, read = row
    return (
        usage.get("input_tokens", 0) * inp
        + usage.get("output_tokens", 0) * out
        + usage.get("cache_creation_input_tokens", 0) * write
        + usage.get("cache_read_input_tokens", 0) * read
    ) / 1_000_000


def _row_or_dearest(model: str) -> tuple[float, float, float, float]:
    """An unknown model is priced at the dearest listed rate: a cap that
    assumes unknown means free is not a cap."""
    return _price_row(model) or max(PRICES.values(), key=lambda r: r[0] + r[1])


def estimate_call(model: str) -> float:
    """What one reviewer call will probably cost, for the pre-call check.
    An unknown model is estimated at the dearest listed rate."""
    inp, out, _write, _read = _row_or_dearest(model)
    return (ESTIMATE_INPUT_TOKENS * inp + ESTIMATE_OUTPUT_TOKENS * out) / 1_000_000


# What one chat answer is assumed to write, thinking included, when deciding
# whether the next request fits. The ledger records what it really cost.
CHAT_OUTPUT_TOKENS = 1_500


def estimate_chat(model: str, prompt_tokens: int) -> float:
    """What the next chat request will probably cost: the whole pending
    prompt at the full input rate, plus a nominal answer.

    Deliberately not the cached rate. Most of a long conversation is read
    from the cache at a twentieth of the price, but only if the cache is
    still warm, and a check that assumes the cheap case lets the cap be
    crossed on the day it is not.
    """
    inp, out, _write, _read = _row_or_dearest(model)
    return (max(0, prompt_tokens) * inp + CHAT_OUTPUT_TOKENS * out) / 1_000_000


def month_start(now: datetime | None = None) -> datetime:
    """The first instant of the current month, UTC. The cap resets here."""
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def spent_since(db: AsyncSession, since: datetime, purpose: str | None = None) -> float:
    """Dollars recorded since `since`. A call the API turned away carries
    no usage and so costs nothing here, which matches what it bills. A
    call that failed after the API had begun to answer is different: it is
    billed, and whoever records it passes the usage reported up to then
    (the chat panel does), so it counts here like any other."""
    stmt = select(func.coalesce(func.sum(AiUsage.cost_usd), 0.0)).where(
        AiUsage.created_at >= since, AiUsage.cost_usd.is_not(None)
    )
    if purpose is not None:
        stmt = stmt.where(AiUsage.purpose == purpose)
    total = (await db.execute(stmt)).scalar_one()
    return float(total or 0.0)


async def spent_this_month(db: AsyncSession, purpose: str | None = None) -> float:
    return await spent_since(db, month_start(), purpose)


@dataclass(frozen=True)
class Budget:
    """The verdict of a pre-call check, in the operator's units."""

    cap_usd: float
    spent_usd: float

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)


class BudgetExceeded(RuntimeError):
    """The next call would take this month's spend past the cap."""

    def __init__(self, budget: Budget, estimate: float) -> None:
        self.budget = budget
        self.estimate = estimate
        super().__init__(
            f"Monthly AI budget reached: ${budget.spent_usd:.2f} of ${budget.cap_usd:.2f} "
            f"spent, next call ≈ ${estimate:.4f}"
        )


async def check_budget(db: AsyncSession, cap_usd: float, estimate: float) -> Budget:
    """Refuse the next call rather than discover the overrun afterwards.

    A cap of zero means "no Anthropic calls at all" — the honest reading of
    a budget of nothing — so the operator can pause spending without
    removing the key.
    """
    spent = await spent_this_month(db)
    budget = Budget(cap_usd=cap_usd, spent_usd=spent)
    if spent + estimate > cap_usd:
        raise BudgetExceeded(budget, estimate)
    return budget


async def record(
    db: AsyncSession,
    *,
    purpose: str,
    model: str,
    usage: dict[str, int] | None,
    asset_id: uuid.UUID | None = None,
    ok: bool = True,
    note: str = "",
) -> AiUsage:
    """Write one call to the ledger. Added to the session, not committed —
    the caller commits, so a run's bookkeeping lands with its results."""
    usage = usage or {}
    row = AiUsage(
        purpose=purpose,
        model=model,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens", 0)),
        cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
        cost_usd=price_of(model, usage) if usage else None,
        asset_id=asset_id,
        ok=ok,
        note=note[:200],
    )
    db.add(row)
    await db.flush()
    return row
