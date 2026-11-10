"""Pair editing proxies with their originals

Premiere's ingest writes `<clip folder>/Proxies/<stem>_Proxy.<ext>`, and the
scanner catalogued every one as a second asset: found by every search,
dated by the day the proxy was made rather than the shoot, transcribed,
embedded and tagged a second time. `assets.proxy_of_id` records that a row
is the proxy of another, which takes it out of the catalogue while keeping
the row (and anything already decided on it); `libraries.proxy_pattern` is
where each library's editors put their proxies.

Schema only. Existing duplicates are linked by
`python -m framefound.ops.pair_proxies` after its dry-run report.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("libraries") as batch:
        batch.add_column(
            sa.Column(
                "proxy_pattern",
                sa.String(200),
                nullable=False,
                server_default="Proxies/{stem}_Proxy",
            )
        )
    with op.batch_alter_table("assets") as batch:
        batch.add_column(sa.Column("proxy_of_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_assets_proxy_of_id_assets",
            "assets",
            ["proxy_of_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_assets_proxy_of_id", ["proxy_of_id"])


def downgrade() -> None:
    with op.batch_alter_table("assets") as batch:
        batch.drop_index("ix_assets_proxy_of_id")
        batch.drop_constraint("fk_assets_proxy_of_id_assets", type_="foreignkey")
        batch.drop_column("proxy_of_id")
    with op.batch_alter_table("libraries") as batch:
        batch.drop_column("proxy_pattern")
