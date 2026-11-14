"""The `local_ai` lane: one wake-up task over the work table.

The work is the rows of `local_ai_jobs` (local_models/jobs.py). The Celery
message carries nothing: `local_ai_tick` runs the most urgent runnable row
and, when more are queued, wakes the lane again. A message delivered twice,
or a tick from beat while a job runs, claims nothing it should not (the
claim is one conditional statement), so nothing here needs a countdown or
an eta — which the 12-hour visibility timeout would redeliver anyway.

On the Mac, `worker-local-ai` consumes the lane one job at a time, at the
lowest priority of any service. In the containers `worker-ai` consumes it so
a stray message is never stranded; nothing is queued there (the API refuses
every local-AI write), and a tick that arrives anyway closes any active row
with the reason and returns in milliseconds.
"""

import asyncio

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.celery_app import celery_app
from framefound.config import Settings, get_settings
from framefound.local_models import availability, jobs
from framefound.local_models.deps import deps_for

log = structlog.get_logger()


@celery_app.task(
    name="framefound.local_ai_tick",
    queue="local_ai",
    # A wake-up is never retried: the next one (beat sends one a minute)
    # finds whatever this one left.
    max_retries=0,
)
def local_ai_tick() -> None:
    """Run the most urgent local-AI job; wake the lane again if more are queued."""
    if asyncio.run(_tick()):
        wake()


async def _tick(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if availability(settings).mode == "demo":
        # The review copy runs its jobs inside the API (P30). A tick that
        # reaches a worker anyway (a stray message) leaves them to it.
        return False
    # Its own engine: asyncpg pools belong to the event loop that made them,
    # and each asyncio.run is a new loop (processing/tasks.py does the same).
    engine = create_async_engine(settings.db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return await jobs.run_next(factory, deps_for(settings))
    finally:
        await engine.dispose()


def wake(settings: Settings | None = None) -> bool:
    """Ask the lane to look at the work table now. False when the queue is down
    (the job is still in the table: the once-a-minute tick finds it).

    In demo mode there is no queue: the jobs run inside this process
    (`demo.wake_inline`), and this is True once that lane is running.
    """
    settings = settings or get_settings()
    if availability(settings).mode == "demo":
        # Imported here and nowhere else, so a real install never starts it (P30).
        from framefound.local_models import demo

        return demo.wake_inline(settings)
    try:
        local_ai_tick.delay()
    except Exception:
        log.warning("local_ai.wake_failed", exc_info=True)
        return False
    return True


# Registers L1's four kinds, in the worker and in the API alike.
from framefound.local_models import handlers  # noqa: E402,F401 - registers L1's four kinds

# Later phases add ONE line each below this one for a handler module outside local_models/ (13.2):
#   L3  from framefound.looking import runner
#   L4  from framefound.processing import colour_jobs
#   L5  from framefound.nle import drafts
from framefound.processing import colour_jobs  # noqa: E402,F401 - registers clip_look, listing_look (L4)

# isort: split
from framefound.nle import drafts  # noqa: E402,F401 - registers the draft_cut job (L5)

# isort: split
from framefound.looking import runner  # noqa: E402,F401 - registers the look kinds (L3)
