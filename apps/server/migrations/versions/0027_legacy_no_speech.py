"""Record the clips older transcription runs heard nothing in

Until the 2026-11 transcription review, a run that heard no speech returned
before writing a Transcript row and its job was still marked succeeded. A
run that hears nothing now writes an empty row, and that row is what search,
the MCP connector and the asset page read as "no speech". The clips heard
before the change had no row, so the page promised them a transcript "on its
way" that no job would ever bring — the scanner's sweep counts a succeeded
run as the asset's turn and never queues it again.

This writes the empty row those runs would have written: audio-bearing
assets in a library with transcription on, with a succeeded transcribe job
and no transcript. `model_name` says where the row came from, and language
is empty because nothing was heard to detect one from.

The row is a placeholder, not a verdict. Many of these clips were not
silent: the old filter dropped a cue on either doubtful signal and only
audio stream 0 was decoded. The scanner's transcript sweep takes every row
marked LEGACY_MODEL for one more listen (scanner LEGACY_NO_SPEECH_MODEL),
and the asset page reads it as queued until that run replaces it.

Revision ID: 0027
Revises: 0026
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | None = None
depends_on: str | None = None

LEGACY_MODEL = "legacy/no-speech"

_assets = sa.table(
    "assets",
    sa.column("id", sa.Uuid),
    sa.column("library_id", sa.Uuid),
    sa.column("media_type", sa.String),
    sa.column("audio_codec", sa.String),
)
_libraries = sa.table(
    "libraries", sa.column("id", sa.Uuid), sa.column("transcribe_enabled", sa.Boolean)
)
_jobs = sa.table(
    "jobs",
    sa.column("asset_id", sa.Uuid),
    sa.column("task_name", sa.String),
    sa.column("status", sa.String),
    sa.column("started_at", sa.DateTime(timezone=True)),
    sa.column("finished_at", sa.DateTime(timezone=True)),
)
_transcripts = sa.table(
    "transcripts",
    sa.column("id", sa.Uuid),
    sa.column("asset_id", sa.Uuid),
    sa.column("language", sa.String),
    sa.column("model_name", sa.String),
    sa.column("processed_at", sa.DateTime(timezone=True)),
    sa.column("full_text", sa.Text),
    sa.column("segment_count", sa.Integer),
    sa.column("version", sa.Integer),
)


def upgrade() -> None:
    bind = op.get_bind()
    # When the run that heard nothing finished, so the row is dated by the
    # listening rather than by this migration.
    heard_at = sa.func.max(sa.func.coalesce(_jobs.c.finished_at, _jobs.c.started_at))
    rows = bind.execute(
        sa.select(_jobs.c.asset_id, heard_at)
        .join(_assets, _assets.c.id == _jobs.c.asset_id)
        .join(_libraries, _libraries.c.id == _assets.c.library_id)
        .where(
            _jobs.c.task_name == "transcribe_asset",
            _jobs.c.status == "succeeded",
            _libraries.c.transcribe_enabled.is_(True),
            sa.or_(_assets.c.media_type == "audio", _assets.c.audio_codec.is_not(None)),
            ~sa.exists().where(_transcripts.c.asset_id == _jobs.c.asset_id),
        )
        .group_by(_jobs.c.asset_id)
    ).all()
    if not rows:
        return
    # IDs minted here rather than by the database: the transcripts table has
    # no server-side default, and this stays one statement on either dialect.
    op.bulk_insert(
        _transcripts,
        [
            {
                "id": uuid.uuid4(),
                "asset_id": asset_id,
                "language": "",
                "model_name": LEGACY_MODEL,
                "processed_at": heard_at_value,
                "full_text": "",
                "segment_count": 0,
                "version": 1,
            }
            for asset_id, heard_at_value in rows
        ],
    )


def downgrade() -> None:
    op.execute(
        _transcripts.delete().where(
            _transcripts.c.model_name == LEGACY_MODEL, _transcripts.c.segment_count == 0
        )
    )
