"""Asset edits: a develop recipe per photograph, versioned, never destructive

An edit is a recipe (slider values as JSON), not pixels. Originals are
mounted read-only and stay that way; the recipe is applied when a preview or
an export is rendered. Rows are append-only versions — undo is "use the
previous row", and reverting to the original is deleting the rows, neither
of which touches a file.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "asset_edits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.Uuid(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("recipe", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("asset_id", "version", name="uq_asset_edits_asset_version"),
    )
    op.create_index("ix_asset_edits_asset_id", "asset_edits", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_edits_asset_id", table_name="asset_edits")
    op.drop_table("asset_edits")
