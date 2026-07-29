"""The embedding column must behave correctly on BOTH dialects.

The functional suite runs on SQLite, where similarity is computed in Python —
so the PostgreSQL query path is never executed there. These tests compile the
statements against the Postgres dialect instead, which is what catches
problems like a TypeDecorator silently dropping pgvector's operators.
"""

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from framefound.db.models import Frame
from framefound.db.vector_type import EMBEDDING_DIMENSIONS, Embedding

VECTOR = [0.1] * EMBEDDING_DIMENSIONS


def test_cosine_distance_compiles_to_pgvector_operator() -> None:
    stmt = select(Frame.id).order_by(Frame.embedding.cosine_distance(VECTOR))
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "<=>" in sql


def test_l2_distance_available() -> None:
    stmt = select(Frame.id).order_by(Frame.embedding.l2_distance(VECTOR))
    assert "<->" in str(stmt.compile(dialect=postgresql.dialect()))


def test_column_is_a_real_vector_on_postgres() -> None:
    ddl = str(Embedding().compile(dialect=postgresql.dialect()))
    assert ddl.upper().startswith("VECTOR")
    assert str(EMBEDDING_DIMENSIONS) in ddl


def test_column_falls_back_to_json_on_sqlite() -> None:
    assert "JSON" in str(Embedding().compile(dialect=sqlite.dialect())).upper()


def test_is_not_null_filter_compiles_on_both_dialects() -> None:
    stmt = select(Frame.id).where(Frame.embedding.is_not(None))
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        assert "IS NOT NULL" in str(stmt.compile(dialect=dialect))
