"""When a model may run: two memory budgets and the overnight window.

The Mac that runs models is also the owner's editing machine. By day a model
may take only a small share of its memory; overnight, when nobody is cutting,
a larger one. A model runs only if the memory it needs fits the budget in
force at that hour. This is an admission rule, not a cap: nothing here
watches a model that grows after it was let in.

Pure. Every function is given the time by its caller — the Mac's own wall
clock as a naive `datetime` — because "overnight" means overnight where the
Mac is. Nothing here reads a clock or a timezone database (the scheduler's
clock is UTC, and Windows, where this is tested, has no timezone database).
This module imports only the standard library.

GB is 1024**3 bytes, here and everywhere in the model manager.
"""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal

PAUSED = "Local AI is paused."
BAD_CLOCK = "Give the time as hours and minutes, like 21:30."


@dataclass(frozen=True)
class Budgets:
    day_gb: float = 8.0
    night_gb: float = 20.0
    # The overnight window: start inclusive, end exclusive, and it may wrap
    # midnight (it usually does). Equal times mean there is no window.
    night_start: time = time(21, 30)
    night_end: time = time(5, 30)


@dataclass(frozen=True)
class Decision:
    run: bool
    why: Literal["ok", "paused", "window", "never_fits"]
    # "" when it may run; otherwise the sentence to show.
    message: str
    # When to ask again. Set only for "window": a pause ends when somebody
    # ends it, and a model that never fits waits for a setting to change.
    not_before: datetime | None = None


def parse_clock(text: str) -> time:
    """`21:30` (or `5:30`) as a time of day; ValueError(sentence) otherwise."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text)
    if match is None or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError(BAD_CLOCK)
    return time(int(match[1]), int(match[2]))


def clock_text(at: time) -> str:
    return f"{at.hour:02d}:{at.minute:02d}"


def gb_text(gb: float) -> str:
    """A memory figure as the pages write it: one decimal, and `8` rather than `8.0`."""
    text = f"{gb:.1f}"
    return text.removesuffix(".0")


def has_window(b: Budgets) -> bool:
    """Is there an overnight window at all? One that starts and ends at the same minute
    never opens."""
    return b.night_start != b.night_end


def lacks_window(memory_gb: float, b: Budgets) -> bool:
    """Would it fit the overnight budget, with no overnight window for it to run in?

    Too big for the day, small enough for the night, and the night never
    comes. Asked wherever a sentence has to say which of the two settings
    is in the way: the budget, or the window.
    """
    return b.day_gb < memory_gb <= b.night_gb and not has_window(b)


def is_overnight(now: datetime, b: Budgets) -> bool:
    at = now.time()
    if b.night_start < b.night_end:
        return b.night_start <= at < b.night_end
    if b.night_start > b.night_end:
        return at >= b.night_start or at < b.night_end
    return False


def budget_gb(now: datetime, b: Budgets) -> float:
    """The budget in force at this moment."""
    return b.night_gb if is_overnight(now, b) else b.day_gb


def next_change(now: datetime, b: Budgets) -> datetime:
    """When the budget in force next changes: the next start or end of the window.

    Always after `now`. With no window (equal times) nothing ever changes;
    the next time the clock reads that time is returned, so a caller that
    waits until then simply decides again.
    """
    return min(_next(now, b.night_start), _next(now, b.night_end))


def fits(memory_gb: float, b: Budgets) -> Literal["day", "night", "never"]:
    """The budget a model fits: `day` (which is any hour, the budgets being the usual way
    round), `night` for the overnight one only, or `never`.

    `never` is what `decide` would say at every hour. A model that fits only
    the overnight budget is `never` when there is no overnight window: it
    must not be shown as "overnight only" beside a sentence saying it will
    not run.
    """
    if memory_gb <= b.day_gb:
        return "day"
    if memory_gb <= b.night_gb and has_window(b):
        return "night"
    return "never"


def decide(memory_gb: float, now: datetime, b: Budgets, *, paused: bool = False) -> Decision:
    """May a model that needs this much memory run now, and if not, when?"""
    if paused:
        return Decision(False, "paused", PAUSED)
    overnight = is_overnight(now, b)
    in_force, other = (b.night_gb, b.day_gb) if overnight else (b.day_gb, b.night_gb)
    if memory_gb <= in_force:
        return Decision(True, "ok", "")
    needs = gb_text(memory_gb)
    if memory_gb <= other and has_window(b):
        change = next_change(now, b)
        if overnight:
            # Only when the budgets are the unusual way round (overnight the
            # smaller); said properly rather than left to read as a mistake.
            message = (
                f"Waiting for the daytime (starts {clock_text(b.night_end)}): it needs about "
                f"{needs} GB and the overnight budget is {gb_text(b.night_gb)} GB."
            )
        else:
            message = (
                f"Waiting for the overnight window (starts {clock_text(b.night_start)}): it "
                f"needs about {needs} GB and the daytime budget is {gb_text(b.day_gb)} GB."
            )
        return Decision(False, "window", message, change)
    if lacks_window(memory_gb, b):
        # It would fit overnight, but a window that starts and ends at the
        # same minute never opens. Waiting "until 21:30" would wait for ever.
        return Decision(
            False,
            "never_fits",
            f"It needs about {needs} GB, more than the daytime budget "
            f"({gb_text(b.day_gb)} GB), and there is no overnight window: it starts and "
            f"ends at {clock_text(b.night_start)}. Change either under Sharing this Mac.",
        )
    larger, name = (b.night_gb, "overnight") if b.night_gb >= b.day_gb else (b.day_gb, "daytime")
    return Decision(
        False,
        "never_fits",
        f"It needs about {needs} GB, more than the {name} budget ({gb_text(larger)} GB). "
        "Raise the budget under Sharing this Mac.",
    )


def _next(now: datetime, at: time) -> datetime:
    """The next time the wall clock reads `at`, strictly after `now`.

    Wall-clock arithmetic on purpose: "21:30 tomorrow" is still 21:30 on the
    night the clocks change.
    """
    moment = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return moment if moment > now else moment + timedelta(days=1)
