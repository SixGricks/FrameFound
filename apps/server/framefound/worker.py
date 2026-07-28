"""Celery application.

Queues are segregated by resource class (ADR-0004):
  default    - metadata extraction, light work
  media      - FFmpeg transcodes (CPU/NVENC heavy)
  transcribe - ASR (GPU-eligible)
  vision     - embeddings, captions, OCR (GPU-eligible)

Tasks are idempotent and keyed on (asset id, stage, model version); pipeline
state lives in Postgres, not in Celery — see docs/architecture.md.
"""

from celery import Celery

from framefound.config import get_settings
from framefound.logging import configure_logging

configure_logging()
settings = get_settings()

celery_app = Celery("framefound", broker=settings.redis_url)
celery_app.conf.update(
    task_default_queue="default",
    task_acks_late=True,  # a killed worker re-queues the job instead of losing it
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # long-running media jobs: no hoarding
    broker_connection_retry_on_startup=True,
    result_backend=None,  # results live in our own tables, not Redis
    timezone="UTC",
    beat_schedule={
        # TODO(m2): reconciliation scans per library schedule
        # TODO(m3): derivative retention cleanup
        # TODO(m7): DDNS update checks
    },
)


@celery_app.task(name="framefound.ping")
def ping() -> str:
    """Wiring smoke test: `celery -A framefound.worker call framefound.ping`."""
    return "pong"
