"""AI spend ledger, and where a tag suggestion came from

`ai_usage` records every call FrameFound makes to the Claude API with the
token counts the API reported, so the monthly budget on the Security page
is enforced from measured spend. `asset_tags.origin` says who proposed a
suggestion — the CLIP learner, the Claude reviewer, or Claude through the
MCP connector — so the review queue can show it and the operator can weigh
a second opinion as one.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column(
            "asset_id",
            sa.Uuid(),
            sa.ForeignKey("assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
    )
    # The budget check is "sum since the first of the month", every call.
    op.create_index("ix_ai_usage_created_at", "ai_usage", ["created_at"])
    op.create_index("ix_ai_usage_purpose_created", "ai_usage", ["purpose", "created_at"])

    with op.batch_alter_table("asset_tags") as batch:
        batch.add_column(sa.Column("origin", sa.String(20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("asset_tags") as batch:
        batch.drop_column("origin")
    op.drop_index("ix_ai_usage_purpose_created", table_name="ai_usage")
    op.drop_index("ix_ai_usage_created_at", table_name="ai_usage")
    op.drop_table("ai_usage")
