"""Face suggestions: propose a face for a person without moving it

A suggestion is deliberately *not* the same thing as an assignment.

`faces.person_id` is cluster membership, and the existing review flow marks a
rejected face `source='rejected'` while keeping that membership, so the pair
"this face is not that person" survives. That works when the face was already
in the cluster. It fails badly the moment a search reaches across the whole
catalogue: pulling a face out of its own group to offer it, then having the
operator say no, would strand it — attached to a person it is not, and no
longer available to be grouped or named as whoever it actually is. On this
deployment that would put 7,582 faces at risk to review one person.

So a suggestion rides alongside the face instead: the face keeps its
`person_id` and its cluster, and only accepting one moves it.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


FK_NAME = "fk_faces_suggested_person_id"


def upgrade() -> None:
    op.add_column("faces", sa.Column("suggested_person_id", sa.Uuid(), nullable=True))
    # Cosine similarity to that person's prototype at the time of the sweep.
    op.add_column("faces", sa.Column("suggested_similarity", sa.Float(), nullable=True))
    # 'pending' awaits judgement; 'rejected' is remembered so the next sweep
    # does not offer the same face again — the nagging this design avoids.
    op.add_column("faces", sa.Column("suggestion_state", sa.String(length=16), nullable=True))
    op.create_index(
        "ix_faces_suggestion",
        "faces",
        ["suggested_person_id", "suggestion_state"],
        postgresql_where=sa.text("suggested_person_id IS NOT NULL"),
    )
    # The foreign key is added separately, and only where it can be: SQLite has
    # no ALTER for constraints, and the CI smoke test runs this migration
    # against SQLite. Production is PostgreSQL, which is where the ON DELETE
    # rule actually has to hold.
    #
    # SET NULL, never CASCADE. These rows are faces, not suggestions — cascading
    # would make "forget this person" delete every face the system had merely
    # *wondered* about, which on this deployment is hundreds of faces belonging
    # to other people entirely.
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            FK_NAME,
            "faces",
            "people",
            ["suggested_person_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(FK_NAME, "faces", type_="foreignkey")
    op.drop_index("ix_faces_suggestion", table_name="faces")
    op.drop_column("faces", "suggestion_state")
    op.drop_column("faces", "suggested_similarity")
    op.drop_column("faces", "suggested_person_id")
