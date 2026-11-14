"""Looking: reviews somebody asked for, the ledger of looks, and the roll-ups

`look_requests` is a review: a folder of a library (and everything beneath
it) to be looked at by a local model, now or overnight, with its progress.
`looks` is every attempt a local model made at a photograph or a shot, keyed
by the shot's times on its asset rather than a `shot_windows` id (those rows
are rebuilt on every re-analysis). Which build answered is kept as text, so
removing a model changes nothing a model once said.

On existing tables, all nullable or defaulted, so no row is rewritten:
`assets` gains the roll-up Browse and search sort by (`look_score`,
`look_caption`, `look_reason`, `looked_at`, `looked_by`); `asset_tags`
gains the look that proposed a suggestion and the moment in a clip it is
about; `libraries` gains the switch for looking at what arrives
(`look_at_ingest`, off) and when it went on (`look_since`).

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | None = None
depends_on: str | None = None


def _count(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def _text(name: str, length: int) -> sa.Column[object]:
    return sa.Column(name, sa.String(length), nullable=False, server_default="")


def _when(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        "look_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "library_id",
            sa.Uuid(),
            sa.ForeignKey("libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _text("folder", 2048),
        sa.Column("run_when", sa.String(10), nullable=False, server_default="now"),
        sa.Column("redo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("prepare", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("model_id", sa.Uuid(), nullable=True),
        _text("model_name", 200),
        sa.Column("state", sa.String(12), nullable=False, server_default="active"),
        _text("message", 500),
        sa.Column("look_job_id", sa.Uuid(), nullable=True),
        sa.Column("prepare_job_id", sa.Uuid(), nullable=True),
        _count("done"),
        _count("total"),
        _count("failed"),
        _count("tags_suggested"),
        _count("new_tags"),
        _count("held"),
        _count("stills_only"),
        _text("prep_state", 10),
        _count("prep_total"),
        _count("prep_ready"),
        _count("prep_gave_up"),
        _when("prep_queued_at"),
        _when("prep_progress_at"),
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
    op.create_index("ix_look_requests_library_id", "look_requests", ["library_id"])
    op.create_index("ix_look_requests_state", "look_requests", ["state", "created_at"])

    op.create_table(
        "looks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.Uuid(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("start_ms", sa.Integer(), nullable=True),
        sa.Column("end_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("score", sa.SmallInteger(), nullable=True),
        _text("caption", 300),
        _text("reason", 200),
        sa.Column("answer", sa.JSON(), nullable=True),
        _text("note", 200),
        _text("key", 300),
        _text("model", 200),
        _text("model_revision", 64),
        # Two quantisations of one GGUF repository at one commit are two
        # builds; without this they would read as having looked already.
        _text("variant", 100),
        _text("runtime", 30),
        _text("runtime_version", 40),
        _text("prompt_version", 40),
        sa.Column("seconds", sa.Float(), nullable=True),
        sa.Column("demo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("look_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_looks_asset_span", "looks", ["asset_id", "start_ms", "end_ms"])
    op.create_index("ix_looks_model_created", "looks", ["model", "created_at"])
    op.create_index("ix_looks_request_id", "looks", ["request_id"])

    with op.batch_alter_table("assets") as batch:
        batch.add_column(sa.Column("look_score", sa.SmallInteger(), nullable=True))
        batch.add_column(sa.Column("look_caption", sa.String(300), nullable=True))
        batch.add_column(sa.Column("look_reason", sa.String(200), nullable=True))
        batch.add_column(_when("looked_at"))
        batch.add_column(sa.Column("looked_by", sa.String(300), nullable=True))
        batch.create_index("ix_assets_looked_at", ["looked_at"])

    with op.batch_alter_table("asset_tags") as batch:
        batch.add_column(sa.Column("look_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("at_ms", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_asset_tags_look_id_looks", "looks", ["look_id"], ["id"], ondelete="SET NULL"
        )
        # Deleting an asset deletes its looks, and each must find the tags
        # that point at it; without this that is a scan of the whole table.
        batch.create_index("ix_asset_tags_look_id", ["look_id"])

    with op.batch_alter_table("libraries") as batch:
        # Off for every existing library: reviews run when somebody asks.
        batch.add_column(
            sa.Column("look_at_ingest", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(_when("look_since"))


def downgrade() -> None:
    with op.batch_alter_table("libraries") as batch:
        batch.drop_column("look_since")
        batch.drop_column("look_at_ingest")

    with op.batch_alter_table("asset_tags") as batch:
        batch.drop_index("ix_asset_tags_look_id")
        batch.drop_constraint("fk_asset_tags_look_id_looks", type_="foreignkey")
        batch.drop_column("at_ms")
        batch.drop_column("look_id")

    with op.batch_alter_table("assets") as batch:
        batch.drop_index("ix_assets_looked_at")
        batch.drop_column("looked_by")
        batch.drop_column("looked_at")
        batch.drop_column("look_reason")
        batch.drop_column("look_caption")
        batch.drop_column("look_score")

    op.drop_index("ix_looks_request_id", table_name="looks")
    op.drop_index("ix_looks_model_created", table_name="looks")
    op.drop_index("ix_looks_asset_span", table_name="looks")
    op.drop_table("looks")

    op.drop_index("ix_look_requests_state", table_name="look_requests")
    op.drop_index("ix_look_requests_library_id", table_name="look_requests")
    op.drop_table("look_requests")
