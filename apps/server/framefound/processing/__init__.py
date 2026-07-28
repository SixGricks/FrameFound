"""Media processing stages: metadata extraction (M2), proxies/thumbnails (M3).

Importing this package registers its Celery tasks.
"""

from framefound.processing import tasks  # noqa: F401

__all__ = ["tasks"]
