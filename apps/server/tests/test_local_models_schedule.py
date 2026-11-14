"""When a model may run (local_models/schedule.py): the budgets and the window.

Every time here is the Mac's own wall clock, handed in. Nothing reads a clock.
"""

import ast
import sys
from datetime import datetime, time
from pathlib import Path

import pytest

from framefound.local_models import schedule
from framefound.local_models.schedule import Budgets

# The plan's defaults: 8 GB by day, 20 GB overnight, 21:30 to 05:30.
DEFAULT = Budgets()


def at(clock: str, day: int = 12) -> datetime:
    hour, minute = clock.split(":")
    return datetime(2026, 11, day, int(hour), int(minute))


@pytest.mark.parametrize(
    ("clock", "overnight"),
    [("21:29", False), ("21:30", True), ("23:59", True), ("00:00", True), ("05:29", True)]
    + [("05:30", False), ("12:00", False)],
)
def test_the_window_starts_on_its_first_minute_and_ends_before_its_last(
    clock: str, overnight: bool
) -> None:
    assert schedule.is_overnight(at(clock), DEFAULT) is overnight
    assert schedule.budget_gb(at(clock), DEFAULT) == (20.0 if overnight else 8.0)


def test_the_last_second_before_the_window_is_still_daytime() -> None:
    assert not schedule.is_overnight(datetime(2026, 11, 12, 21, 29, 59, 999999), DEFAULT)
    assert schedule.is_overnight(datetime(2026, 11, 12, 5, 29, 59), DEFAULT)


def test_a_window_that_does_not_cross_midnight() -> None:
    small_hours = Budgets(night_start=time(1, 0), night_end=time(5, 0))

    assert not schedule.is_overnight(at("00:59"), small_hours)
    assert schedule.is_overnight(at("01:00"), small_hours)
    assert schedule.is_overnight(at("04:59"), small_hours)
    assert not schedule.is_overnight(at("05:00"), small_hours)
    assert not schedule.is_overnight(at("23:00"), small_hours)
    assert schedule.next_change(at("23:00"), small_hours) == at("01:00", day=13)
    assert schedule.next_change(at("02:00"), small_hours) == at("05:00")


def test_a_small_model_runs_at_noon() -> None:
    decision = schedule.decide(6.0, at("12:00"), DEFAULT)

    assert decision.run and decision.why == "ok"
    assert decision.message == ""
    assert decision.not_before is None


def test_a_model_too_big_for_the_day_waits_until_the_window_opens_tonight() -> None:
    decision = schedule.decide(10.1, at("14:00"), DEFAULT)

    assert not decision.run and decision.why == "window"
    assert decision.not_before == at("21:30")
    assert decision.message == (
        "Waiting for the overnight window (starts 21:30): it needs about 10.1 GB and the "
        "daytime budget is 8 GB."
    )


def test_the_same_model_runs_at_two_in_the_morning() -> None:
    assert schedule.decide(10.1, at("02:00"), DEFAULT).run
    assert schedule.decide(20.0, at("21:30"), DEFAULT).run
    # A minute after the window shut, it waits for tonight's.
    after = schedule.decide(10.1, at("05:30"), DEFAULT)
    assert not after.run and after.not_before == at("21:30")


def test_a_model_larger_than_the_overnight_budget_never_runs() -> None:
    for clock in ("12:00", "02:00"):
        decision = schedule.decide(25.0, at(clock), DEFAULT)

        assert not decision.run and decision.why == "never_fits"
        assert decision.not_before is None
        assert decision.message == (
            "It needs about 25 GB, more than the overnight budget (20 GB). Raise the budget "
            "under Sharing this Mac."
        )


def test_nothing_runs_while_paused_however_small() -> None:
    decision = schedule.decide(1.0, at("02:00"), DEFAULT, paused=True)

    assert not decision.run and decision.why == "paused"
    assert decision.message == "Local AI is paused."
    # A pause ends when somebody ends it: there is no time to wait for.
    assert decision.not_before is None


def test_the_next_change_is_found_across_midnight() -> None:
    assert schedule.next_change(at("14:00"), DEFAULT) == at("21:30")
    assert schedule.next_change(at("23:00"), DEFAULT) == at("05:30", day=13)
    assert schedule.next_change(at("02:00"), DEFAULT) == at("05:30")
    # On the very minute the window opens, the next change is its end.
    assert schedule.next_change(at("21:30"), DEFAULT) == at("05:30", day=13)
    assert schedule.next_change(at("05:30"), DEFAULT) == at("21:30")
    assert schedule.next_change(datetime(2026, 12, 31, 23, 0), DEFAULT) == datetime(
        2027, 1, 1, 5, 30
    )


def test_which_budget_a_model_fits() -> None:
    assert schedule.fits(5.8, DEFAULT) == "day"
    assert schedule.fits(8.0, DEFAULT) == "day"
    assert schedule.fits(8.1, DEFAULT) == "night"
    assert schedule.fits(20.0, DEFAULT) == "night"
    assert schedule.fits(20.1, DEFAULT) == "never"


def test_a_window_that_starts_and_ends_at_once_never_opens() -> None:
    no_window = Budgets(night_start=time(21, 30), night_end=time(21, 30))

    assert not schedule.is_overnight(at("21:30"), no_window)
    assert schedule.decide(6.0, at("21:30"), no_window).run
    decision = schedule.decide(10.1, at("14:00"), no_window)
    # Not "waiting until 21:30": that wait would never end.
    assert not decision.run and decision.why == "never_fits"
    assert decision.not_before is None
    assert "there is no overnight window" in decision.message
    assert schedule.next_change(at("14:00"), no_window) > at("14:00")

    # And the one word for it agrees: not "overnight only" for a model that
    # the sentence beside it says will not run.
    assert not schedule.has_window(no_window) and schedule.has_window(DEFAULT)
    assert schedule.fits(10.1, no_window) == "never"
    assert schedule.fits(6.0, no_window) == "day"
    assert schedule.lacks_window(10.1, no_window)
    # Which setting is in the way: the window for this one, the budget for these.
    assert not schedule.lacks_window(25.0, no_window)
    assert not schedule.lacks_window(6.0, no_window)
    assert not schedule.lacks_window(10.1, DEFAULT)


@pytest.mark.parametrize("gb", [1.0, 8.0, 8.1, 10.1, 20.0, 20.1, 64.0])
@pytest.mark.parametrize(
    "budgets",
    [
        DEFAULT,
        Budgets(night_start=time(21, 30), night_end=time(21, 30)),
        Budgets(day_gb=16.0, night_gb=8.0),
        Budgets(day_gb=16.0, night_gb=8.0, night_start=time(1, 0), night_end=time(1, 0)),
    ],
)
def test_the_budget_a_model_fits_is_what_the_hours_of_the_day_would_decide(
    gb: float, budgets: Budgets
) -> None:
    # `fits` is a summary of `decide`; the two are never allowed to disagree.
    runs = [schedule.decide(gb, at(clock), budgets).run for clock in ("12:00", "02:00")]
    by_day, by_night = runs

    fits = schedule.fits(gb, budgets)

    assert (fits == "never") is (not any(runs))
    if fits == "night":
        assert by_night and not by_day


def test_budgets_the_unusual_way_round_still_follow_the_budget_in_force() -> None:
    smaller_overnight = Budgets(day_gb=16.0, night_gb=8.0)

    assert schedule.decide(10.0, at("14:00"), smaller_overnight).run
    waiting = schedule.decide(10.0, at("23:00"), smaller_overnight)
    assert not waiting.run and waiting.why == "window"
    assert waiting.not_before == at("05:30", day=13)
    assert waiting.message == (
        "Waiting for the daytime (starts 05:30): it needs about 10 GB and the overnight "
        "budget is 8 GB."
    )
    never = schedule.decide(17.0, at("14:00"), smaller_overnight)
    assert never.why == "never_fits" and "the daytime budget (16 GB)" in never.message


@pytest.mark.parametrize(
    ("text", "clock"),
    [("21:30", time(21, 30)), ("05:30", time(5, 30)), ("5:30", time(5, 30))]
    + [(" 00:00 ", time(0, 0)), ("23:59", time(23, 59))],
)
def test_a_time_is_read_as_hours_and_minutes(text: str, clock: time) -> None:
    assert schedule.parse_clock(text) == clock
    assert schedule.parse_clock(schedule.clock_text(clock)) == clock


@pytest.mark.parametrize("text", ["", "24:00", "21:60", "2130", "21.30", "9pm", "21:3", "-1:30"])
def test_anything_else_is_refused_with_an_example(text: str) -> None:
    with pytest.raises(ValueError) as refused:
        schedule.parse_clock(text)

    assert str(refused.value) == "Give the time as hours and minutes, like 21:30."


def test_memory_is_written_with_one_decimal_and_no_trailing_zero() -> None:
    assert schedule.gb_text(8.0) == "8"
    assert schedule.gb_text(10.1) == "10.1"
    assert schedule.gb_text(5.76) == "5.8"
    assert schedule.gb_text(20) == "20"


def test_the_rules_need_nothing_but_the_standard_library() -> None:
    imported: set[str] = set()
    for item in ast.walk(ast.parse(Path(schedule.__file__).read_text(encoding="utf-8"))):
        if isinstance(item, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in item.names)
        elif isinstance(item, ast.ImportFrom):
            # A relative import is this package, which is not the standard library.
            imported.add("." if item.level else (item.module or "").split(".")[0])

    assert imported
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names
