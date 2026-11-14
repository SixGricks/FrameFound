"""Celery application object.

Lives in its own module so task modules can import it without circular
imports; `framefound.worker` remains the `-A` entrypoint and pulls in the
task modules for registration.

Queues are segregated by resource class (ADR-0004):
  default    - metadata extraction, light work
  media      - FFmpeg transcodes (CPU/NVENC heavy)
  exports    - the operator's own renders and exports; server workers only
  transcribe - ASR (GPU-eligible)
  vision     - embeddings, captions, OCR (GPU-eligible)
  local_ai   - local AI models: a wake-up only; the work is the rows of
               local_ai_jobs (local_models/jobs.py). One job at a time, at
               the lowest priority; on the Mac, worker-local-ai consumes it
"""

import time
from typing import Any, NoReturn

import structlog
from celery import Celery, Task
from celery.exceptions import Reject
from celery.schedules import crontab

from framefound.config import Settings, get_settings
from framefound.logging import configure_logging
from framefound.storage import worker_roots
from framefound.storage.worker_roots import NotHere, data_store_problem, is_remote_worker

configure_logging()


class FrameFoundTask(Task):  # type: ignore[misc]
    """Every FrameFound task. Adds one thing: on a worker running on another
    machine (docs/remote-workers.md), a job that machine cannot do goes back
    to its queue for another worker instead of failing.

    Two cases: the shared data store is not mounted there (checked before
    every task, so nothing is ever written to an empty mountpoint), or the
    task raised NotHere (a library it has no mapping for, or whose share it
    cannot see). On the server neither can happen and this is a pass-through.
    """

    # Most tasks autoretry on any exception, which would turn a hand-back into
    # a counted retry — and, after a few, a failure.
    dont_autoretry_for = (Reject, NotHere)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not is_remote_worker():
            return super().__call__(*args, **kwargs)
        problem = data_store_problem(get_settings().data_dir)
        if problem is not None:
            self._hand_back(problem)
        try:
            return super().__call__(*args, **kwargs)
        except NotHere as err:
            self._hand_back(str(err))

    def _hand_back(self, reason: str) -> NoReturn:
        structlog.get_logger().warning("task.handed_back", task=self.name, reason=reason)
        time.sleep(worker_roots.HAND_BACK_PAUSE_S)
        raise Reject(reason, requeue=True)


celery_app = Celery("framefound", broker=get_settings().redis_url, task_cls=FrameFoundTask)
celery_app.conf.update(
    task_default_queue="default",
    task_acks_late=True,  # a killed worker re-queues the job instead of losing it
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # long-running media jobs: no hoarding
    # With acks_late on Redis, a message still unacked after the visibility
    # timeout is put back on its queue while the first run carries on.
    # kombu's default is one hour, and a long auction recording (the speech
    # mix alone may take two, then Whisper on a no-AVX CPU) outlived it: the
    # copy ran as soon as the original finished, outlived the hour too, and
    # the only transcribe slot re-transcribed the same file in a loop. The
    # 4 h proxy transcode was exposed the same way. Twelve hours is twice
    # the scanner's ORPHAN_JOB_AGE (6 h, when a "running" job is declared
    # dead), so the broker never redelivers a run the scanner still thinks
    # is alive. The cost falls on every deploy or restart mid-run: Celery's
    # warm shutdown (SIGTERM) waits for the task rather than requeueing it,
    # docker stop SIGKILLs it after 10 s, and the unacked message only comes
    # back when this timeout runs out — up to 12 h. Only a dying child
    # process (task_reject_on_worker_lost) requeues at once. The scanner's
    # transcript sweep waits out the same 12 h (BROKER_REDELIVERY_AGE, held
    # equal by a test) so that copy is the only one: queueing its own at
    # the 6 h reap heard the whole file twice.
    broker_transport_options={"visibility_timeout": 12 * 3600},
    broker_connection_retry_on_startup=True,
    result_backend=None,  # results live in our own tables, not Redis
    timezone="UTC",
    beat_schedule={
        # Library scan schedules run in the scanner service, not beat.
        # TODO(m3): derivative retention cleanup
        # TODO(m7): DDNS update checks
        # The tag learner's safety net. New media is scored against every
        # learned tag as it is embedded; this re-runs every tag against the
        # whole library to catch what that missed and refresh each bar.
        "tag-suggest-sweep": {
            "task": "framefound.suggest_all_tags",
            "schedule": crontab(hour=7, minute=40),  # 02:40 Eastern
        },
        # The Claude tag reviewer's nightly sweep. The task itself checks
        # whether automatic review is on, so scheduling it is free when off.
        "ai-tag-review-sweep": {
            "task": "framefound.review_sweep",
            "schedule": crontab(hour=8, minute=20),  # 03:20 Eastern
        },
    },
)


# The local-AI lane's tick (local_models/jobs.py): what starts a job when the
# overnight window opens or a pause is lifted from another tab, reaps a job
# whose worker died, and unloads a model that may no longer be loaded. Once a
# minute, in UTC like every beat entry; whether it is overnight is decided by
# the Mac's own clock inside the tick, never by a crontab. `expires` so ticks
# cannot pile up behind a long download (the lane runs one job at a time).
LOCAL_AI_TICK: dict[str, Any] = {
    "task": "framefound.local_ai_tick",
    "schedule": 60.0,
    "options": {"expires": 50},
}


def local_ai_beat(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """The beat entries local AI adds: the tick, only on an install that can run
    models. The containers get none (D26): there is nothing there for it to do.
    Nor does the review copy (demo mode): its jobs run inside the API, and a
    worker ticking beside it would drive stand-ins of its own (P30)."""
    from framefound import local_models

    if local_models.availability(settings).mode != "ready":
        return {}
    return {"local-ai-tick": {**LOCAL_AI_TICK, "options": dict(LOCAL_AI_TICK["options"])}}


celery_app.conf.beat_schedule.update(local_ai_beat())
