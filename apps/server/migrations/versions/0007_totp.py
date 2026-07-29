"""TOTP enrolment fields.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_pending_secret", sa.String(length=255), nullable=True))
    op.add_column(
        "users",
        sa.Column("totp_recovery_hashes", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("users", "totp_recovery_hashes")
    op.drop_column("users", "totp_pending_secret")
