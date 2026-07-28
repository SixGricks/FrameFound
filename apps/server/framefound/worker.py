"""Celery worker entrypoint: `celery -A framefound.worker ...`.

The app object lives in framefound.celery_app; importing task modules here
registers them. Add new task modules to the import list below.
"""

from framefound import processing  # noqa: F401  (registers processing tasks)
from framefound.celery_app import celery_app


@celery_app.task(name="framefound.ping")
def ping() -> str:
    """Wiring smoke test: `celery -A framefound.worker call framefound.ping`."""
    return "pong"


__all__ = ["celery_app", "ping"]
