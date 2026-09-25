"""Listing auto-edit state: one run at a time, and what the last run did

Auto-edit's "running" lived only in the browser tab. On Sep 25 a second
press on 901 Smyrna Rd started a second run beside the first, and when the
API started refusing calls both runs failed photo after photo with nothing
on the page to say why. The listing now records its run: queued/running
(a second press is refused), then done/failed with a line saying what
happened ("62 edited, 1 skipped — …", "Stopped: … credit balance is too low").

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "listings",
        sa.Column("ai_edit_state", sa.String(length=20), nullable=False, server_default="idle"),
    )
    op.add_column(
        "listings",
        sa.Column("ai_edit_mode", sa.String(length=12), nullable=False, server_default=""),
    )
    op.add_column(
        "listings",
        sa.Column("ai_edit_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "listings",
        sa.Column("ai_edit_message", sa.String(length=300), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("listings", "ai_edit_message")
    op.drop_column("listings", "ai_edit_started_at")
    op.drop_column("listings", "ai_edit_mode")
    op.drop_column("listings", "ai_edit_state")
