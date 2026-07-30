"""Tags with learned prototypes

A tag carries what the system has worked out it looks like: a CLIP vector
blending the tag's own words with the mean of the frames the operator tagged,
plus a per-tag threshold. See framefound/ai/tagging.py.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

from framefound.db.vector_type import Embedding

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("slug", sa.String(length=140), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("prototype", Embedding(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("threshold_reason", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("example_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("learned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suggest_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_tags_slug", "tags", ["slug"])

    op.create_table(
        "asset_tags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "asset_id",
            sa.Uuid(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tag_id", sa.Uuid(), sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.UniqueConstraint("asset_id", "tag_id", name="uq_asset_tag"),
    )
    op.create_index("ix_asset_tags_asset_id", "asset_tags", ["asset_id"])
    op.create_index("ix_asset_tags_tag_id", "asset_tags", ["tag_id"])
    op.create_index("ix_asset_tags_source", "asset_tags", ["source"])


def downgrade() -> None:
    op.drop_table("asset_tags")
    op.drop_index("ix_tags_slug", table_name="tags")
    op.drop_table("tags")
