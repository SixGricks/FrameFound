"""Media processing stages: metadata extraction (M2), proxies/thumbnails (M3),
tag learning (M6), the Claude tag reviewer.

Importing this package registers its Celery tasks.
"""

from framefound.processing import (
    tag_review_tasks,  # noqa: F401  (registers the Claude reviewer)
    tag_tasks,  # noqa: F401  (registers tag learning)
    tasks,  # noqa: F401
)

__all__ = ["tag_review_tasks", "tag_tasks", "tasks"]
