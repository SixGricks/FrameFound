"""Indexes for Browse's default order and the maintenance sweeps

Browse lists newest first — `ORDER BY first_indexed_at DESC, id` — and
nothing indexed first_indexed_at, so every page was a top-N sort over the
whole assets table. The scanner's visual repair sweep asks the jobs table
"has this task run for this asset, and when" for every candidate asset; jobs
has one row per attempt and is never pruned, so with single-column indexes
that was a scan of the task's rows each time (review, 2026-10: the sweep was
the largest consumer of the database by index probes).

Revision ID: 0024
Revises: 0023
"""

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index("ix_assets_first_indexed_at", "assets", ["first_indexed_at"])
    op.create_index("ix_jobs_task_asset", "jobs", ["task_name", "asset_id"])
    op.create_index("ix_jobs_task_started", "jobs", ["task_name", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_task_started", table_name="jobs")
    op.drop_index("ix_jobs_task_asset", table_name="jobs")
    op.drop_index("ix_assets_first_indexed_at", table_name="assets")
