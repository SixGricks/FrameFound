"""Frame embeddings + HNSW index (PostgreSQL/pgvector).

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIMENSIONS = 512  # CLIP ViT-B/32


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute(f"ALTER TABLE frames ADD COLUMN embedding vector({DIMENSIONS})")
        # HNSW beats IVFFlat here: no training step, and it stays accurate as
        # rows are added continuously by background processing rather than in
        # one bulk load. Cosine ops match the L2-normalised vectors we store.
        op.execute(
            "CREATE INDEX ix_frames_embedding ON frames USING hnsw (embedding vector_cosine_ops)"
        )
    else:
        # SQLite (tests): JSON round-trip, similarity computed in Python.
        op.add_column("frames", sa.Column("embedding", sa.JSON(), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_frames_embedding")
    op.drop_column("frames", "embedding")
