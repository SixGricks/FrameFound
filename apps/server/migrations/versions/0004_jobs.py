"""Job execution history.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_name", sa.String(length=100), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.id"], name="fk_jobs_asset_id_assets", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_jobs_task_name", "jobs", ["task_name"])
    op.create_index("ix_jobs_asset_id", "jobs", ["asset_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_started_at", "jobs", ["started_at"])


def downgrade() -> None:
    op.drop_table("jobs")
