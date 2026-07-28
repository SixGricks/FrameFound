"""Transcripts + segments, library transcribe toggle, Postgres FTS index.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcripts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("language_confidence", sa.Float(), nullable=True),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("segment_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_transcripts"),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_transcripts_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("asset_id", name="uq_transcripts_asset_id"),
    )

    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transcript_id", sa.Uuid(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("speaker", sa.String(length=50), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_transcript_segments"),
        sa.ForeignKeyConstraint(
            ["transcript_id"],
            ["transcripts.id"],
            name="fk_transcript_segments_transcript_id_transcripts",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_transcript_segments_transcript_id", "transcript_segments", ["transcript_id"]
    )
    op.create_index("ix_transcript_segments_start_ms", "transcript_segments", ["start_ms"])

    op.add_column(
        "libraries",
        sa.Column("transcribe_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # Full-text search index (Postgres only; SQLite test runs use LIKE).
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_transcript_segments_text_fts ON transcript_segments "
            "USING GIN (to_tsvector('english', text))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_transcript_segments_text_fts")
    op.drop_column("libraries", "transcribe_enabled")
    op.drop_table("transcript_segments")
    op.drop_table("transcripts")
