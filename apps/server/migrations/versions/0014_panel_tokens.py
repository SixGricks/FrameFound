"""Panel tokens: revocable credentials for editing panels

Separate from auth_sessions: a session belongs to a browser and slides its
expiry, a panel token belongs to a machine and is read-only by default.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "panel_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("host", sa.String(length=20), nullable=False, server_default="other"),
        sa.Column("scopes", sa.String(length=200), nullable=False, server_default="read"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=45), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_panel_tokens_token_hash", "panel_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_panel_tokens_token_hash", table_name="panel_tokens")
    op.drop_table("panel_tokens")
