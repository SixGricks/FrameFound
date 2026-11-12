"""Record which machine ran each job

A worker can now run on another machine (the Mac mini — see
docs/remote-workers.md). `jobs.worker` holds its FRAMEFOUND_WORKER_NAME so
the processing page can show where the work went and how each machine is
doing; NULL is the server's own containers, which is every existing row.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("worker", sa.String(100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("worker")
