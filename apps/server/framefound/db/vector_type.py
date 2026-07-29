"""Portable embedding column.

Production runs PostgreSQL with pgvector, where the column is a real
`vector(N)` supporting indexed nearest-neighbour search. The unit suite runs
on SQLite, which has no such type — there the values round-trip as JSON so
model code, fixtures, and migrations stay identical across both.
"""

from typing import Any

from sqlalchemy import JSON, Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

EMBEDDING_DIMENSIONS = 512  # CLIP ViT-B/32


class Embedding(TypeDecorator[list[float]]):
    # none_as_null matters: without it SQLAlchemy stores Python None as the
    # JSON value `null`, which is NOT SQL NULL — so "not embedded yet" frames
    # would satisfy `embedding IS NOT NULL` and poison similarity queries.
    impl = JSON(none_as_null=True)
    cache_ok = True

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        super().__init__()
        self.dimensions = dimensions

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(self.dimensions))
        return dialect.type_descriptor(JSON(none_as_null=True))

    def process_bind_param(self, value: list[float] | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        return list(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        return list(value)
