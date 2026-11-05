"""Celery application object.

Lives in its own module so task modules can import it without circular
imports; `framefound.worker` remains the `-A` entrypoint and pulls in the
task modules for registration.

Queues are segregated by resource class (ADR-0004):
  default    - metadata extraction, light work
  media      - FFmpeg transcodes (CPU/NVENC heavy)
  transcribe - ASR (GPU-eligible)
  vision     - embeddings, captions, OCR (GPU-eligible)
"""

from celery import Celery
from celery.schedules import crontab

from framefound.config import get_settings
from framefound.logging import configure_logging

configure_logging()

celery_app = Celery("framefound", broker=get_settings().redis_url)
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
