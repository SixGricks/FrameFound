"""Trigram indexes for filename and folder search

Search matches filenames and folder paths with `ILIKE '%term%'`. A leading
wildcard cannot use a B-tree, so every query read the whole assets table.
pg_trgm's GIN operator class answers ILIKE with a leading wildcard from the
index. Postgres only; the SQLite test dialect has no equivalent and needs
none at fixture scale.

Revision ID: 0026
Revises: 0025
"""

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_assets_filename_trgm ON assets "
            "USING GIN (filename gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_assets_relative_path_trgm ON assets "
            "USING GIN (relative_path gin_trgm_ops)"
        )


def downgrade() -> None:
    # The extension stays: dropping it could break anything else that came
    # to rely on it, and leaving it costs nothing.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_assets_relative_path_trgm")
        op.execute("DROP INDEX IF EXISTS ix_assets_filename_trgm")
