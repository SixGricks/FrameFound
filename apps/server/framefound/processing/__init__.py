"""Media processing stages: metadata extraction (M2), proxies/thumbnails (M3),
tag learning (M6).

Importing this package registers its Celery tasks.
"""

from framefound.processing import (
    tag_tasks,  # noqa: F401  (registers tag learning)
    tasks,  # noqa: F401
)

__all__ = ["tag_tasks", "tasks"]
