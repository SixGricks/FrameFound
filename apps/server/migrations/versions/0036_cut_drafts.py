"""Assembly: versions, reasons and provenance in cut history; draft requests

`cuts` learns families and where a cut came from: `root_id` and
`parent_id` group the versions of one cut (neither is a foreign key, so
deleting version 1 does not un-group the rest), `version` numbers them,
`sent_at` is the first download of a local draft's Premiere file (until
then the draft marks no shot used), and the provenance names say which
local model drafted it, as text. Every existing cut becomes version 1 of a
family of its own, sent or not as before: `source` `mcp` and `import`
count as they always have.

`cut_pieces.note` keeps the reason for each piece, which until now became a
marker in the XML and was then lost.

`cut_drafts` holds a request to the Editor and how it went; a draft that
succeeded is an ordinary cut.

Additive only: nullable or defaulted columns on two tables, one new table,
no data rewritten.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | None = None
depends_on: str | None = None


def _provenance() -> list[sa.Column[object]]:
    """The names every table of answers records a build by, as text.

    `variant` is "" rather than NULL, as on `clip_looks` and `looks`.
    """
    return [
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("model_revision", sa.String(64), nullable=True),
        sa.Column("variant", sa.String(100), nullable=False, server_default=""),
        sa.Column("runtime", sa.String(30), nullable=True),
        sa.Column("runtime_version", sa.String(40), nullable=True),
        sa.Column("prompt_version", sa.String(40), nullable=True),
    ]


def _when(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    with op.batch_alter_table("cuts") as batch:
        batch.add_column(sa.Column("root_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("parent_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(_when("sent_at"))
        for column in _provenance():
            batch.add_column(column)
        # A family is read by its root on every cut page.
        batch.create_index("ix_cuts_root_id", ["root_id"])

    with op.batch_alter_table("cut_pieces") as batch:
        batch.add_column(sa.Column("note", sa.String(500), nullable=False, server_default=""))

    op.create_table(
        "cut_drafts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("state", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("message", sa.String(500), nullable=False, server_default=""),
        sa.Column("title", sa.String(120), nullable=False, server_default=""),
        sa.Column("brief", sa.Text(), nullable=False, server_default=""),
        sa.Column("scope", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("target_s", sa.Float(), nullable=False, server_default="30"),
        sa.Column("min_shot_s", sa.Float(), nullable=False, server_default="1.5"),
        sa.Column("max_shot_s", sa.Float(), nullable=False, server_default="8"),
        sa.Column("exclude_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("candidates", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("shots_in_scope", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clips", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("problems", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "cut_id", sa.Uuid(), sa.ForeignKey("cuts.id", ondelete="SET NULL"), nullable=True
        ),
        # History and a prunable job row: no foreign keys.
        sa.Column("revises_cut_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("origin", sa.String(10), nullable=False, server_default="web"),
        *_provenance(),
        sa.Column("seconds", sa.Float(), nullable=True),
        sa.Column("demo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        _when("started_at"),
        _when("finished_at"),
    )
    # Deleting a cut has to find the draft that became it.
    op.create_index("ix_cut_drafts_cut_id", "cut_drafts", ["cut_id"])
    op.create_index("ix_cut_drafts_created_at", "cut_drafts", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_cut_drafts_created_at", table_name="cut_drafts")
    op.drop_index("ix_cut_drafts_cut_id", table_name="cut_drafts")
    op.drop_table("cut_drafts")

    with op.batch_alter_table("cut_pieces") as batch:
        batch.drop_column("note")

    with op.batch_alter_table("cuts") as batch:
        batch.drop_index("ix_cuts_root_id")
        for name in (
            "prompt_version",
            "runtime_version",
            "runtime",
            "variant",
            "model_revision",
            "model",
            "sent_at",
            "version",
            "parent_id",
            "root_id",
        ):
            batch.drop_column(name)
