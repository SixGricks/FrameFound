"""Derivatives table + library processing-profile fields.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "derivatives",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("media_format", sa.String(length=20), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("codec", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_derivatives"),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_derivatives_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("asset_id", "kind", name="uq_derivatives_asset_id"),
    )
    op.create_index("ix_derivatives_kind", "derivatives", ["kind"])

    op.add_column(
        "libraries",
        sa.Column("generate_proxies", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "libraries",
        sa.Column("proxy_resolution", sa.Integer(), nullable=False, server_default="1080"),
    )


def downgrade() -> None:
    op.drop_column("libraries", "proxy_resolution")
    op.drop_column("libraries", "generate_proxies")
    op.drop_table("derivatives")
