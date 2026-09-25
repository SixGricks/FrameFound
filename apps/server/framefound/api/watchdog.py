"""Notice when the API's event loop stops turning, and say what stopped it.

The API is one process with one event loop, so synchronous work inside any
request handler holds every other request hostage. That is how pressing
Propose on the Slideshows page froze every page of the product (Sep 2026): an
O(n²) Python loop ran for hours, and from outside the only clues were a core
at 100% and a health check timing out. Finding the line took attaching a
profiler to the live process.

This makes a stall self-diagnosing. A coroutine stamps a heartbeat every
second; a plain thread — which keeps running while the loop is stuck —
watches it, and once the loop has been silent past the threshold it logs the
loop thread's Python stack, which names the blocking line exactly. When the
loop comes back it logs how long it was gone, and the stall is kept for the
alert banner, so the operator hears about it too.
"""

import asyncio
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

log = structlog.get_logger()

TICK_SECONDS = 1.0
STALL_AFTER_SECONDS = 5.0


@dataclass
class Stall:
    started_at: datetime
    seconds: float
    where: str  # the innermost framefound frame: "media/slideshow.py:66 in collapse…"
    ongoing: bool


_last_stall: Stall | None = None


def last_stall(within: timedelta = timedelta(hours=1)) -> Stall | None:
    """The most recent stall, if it began inside the window."""
    if _last_stall is None or datetime.now(UTC) - _last_stall.started_at > within:
        return None
    return _last_stall


def _where(frame: object) -> str:
    """The deepest frame inside this package: the code to blame, rather than
    the framework or library underneath it."""
    summary = traceback.extract_stack(frame)  # type: ignore[arg-type]
    ours = [f for f in summary if "/framefound/" in f.filename.replace("\\", "/")]
    pick = ours[-1] if ours else (summary[-1] if summary else None)
    if pick is None:
        return "unknown"
    path = pick.filename.replace("\\", "/").split("/framefound/", 1)[-1]
    return f"{path}:{pick.lineno} in {pick.name}"


class LoopWatchdog:
    def __init__(self, stall_after: float = STALL_AFTER_SECONDS, tick: float = TICK_SECONDS):
        self.stall_after = stall_after
        self.tick = tick
        self._beat = time.monotonic()
        self._stop = threading.Event()
        self._loop_thread: int | None = None
        self._task: asyncio.Task[None] | None = None

    async def _heartbeat(self) -> None:
        while True:
            self._beat = time.monotonic()
            await asyncio.sleep(self.tick)

    def _watch(self) -> None:
        global _last_stall
        stalled_at: float | None = None
        while not self._stop.wait(self.tick / 2):
            silent = time.monotonic() - self._beat
            if silent > self.stall_after and stalled_at is None:
                stalled_at = self._beat
                frame = sys._current_frames().get(self._loop_thread or -1)
                stack = "".join(traceback.format_stack(frame)) if frame is not None else ""
                where = _where(frame) if frame is not None else "unknown"
                _last_stall = Stall(
                    started_at=datetime.now(UTC) - timedelta(seconds=silent),
                    seconds=round(silent, 1),
                    where=where,
                    ongoing=True,
                )
                log.error(
                    "api.event_loop_blocked",
                    seconds=round(silent, 1),
                    where=where,
                    stack=stack[-6000:],
                )
            elif silent <= self.stall_after and stalled_at is not None:
                gone = round(time.monotonic() - stalled_at, 1)
                if _last_stall is not None:
                    _last_stall.seconds = gone
                    _last_stall.ongoing = False
                log.warning("api.event_loop_recovered", blocked_seconds=gone)
                stalled_at = None

    def start(self) -> None:
        """Call from inside the running loop (application startup)."""
        self._loop_thread = threading.get_ident()
        self._beat = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._heartbeat())
        threading.Thread(target=self._watch, name="loop-watchdog", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
