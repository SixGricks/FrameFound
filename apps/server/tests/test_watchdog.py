"""The event-loop watchdog names whatever blocks the API.

Written after a slideshow proposal ran a Python O(n²) loop on the request
thread for hours and froze every page; finding the line took a profiler
attached to production. The watchdog has to catch exactly that shape.
"""

import asyncio
import time
from datetime import timedelta

from framefound.api import watchdog


def _blocking_work(seconds: float) -> None:
    time.sleep(seconds)  # stands in for CPU-bound code run on the loop


async def test_a_blocked_loop_is_caught_and_the_culprit_named() -> None:
    dog = watchdog.LoopWatchdog(stall_after=0.3, tick=0.1)
    watchdog._last_stall = None
    dog.start()
    try:
        await asyncio.sleep(0.25)  # a healthy loop first: no stall
        assert watchdog.last_stall() is None

        _blocking_work(1.0)
        await asyncio.sleep(0.4)  # let the loop tick and the watcher see it recover

        stall = watchdog.last_stall()
        assert stall is not None
        assert "_blocking_work" in stall.where or "test_watchdog" in stall.where
        assert stall.ongoing is False, "recovery is recorded"
        assert stall.seconds >= 0.3
    finally:
        dog.stop()


def test_an_old_stall_is_not_news() -> None:
    watchdog._last_stall = watchdog.Stall(
        started_at=watchdog.datetime.now(watchdog.UTC) - timedelta(hours=2),
        seconds=40.0,
        where="media/slideshow.py:66 in collapse_near_duplicates",
        ongoing=False,
    )
    try:
        assert watchdog.last_stall() is None
        assert watchdog.last_stall(within=timedelta(hours=3)) is not None
    finally:
        watchdog._last_stall = None
