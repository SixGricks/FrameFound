"""Migration 0036 (migrations/versions/0036_cut_drafts.py) gives cut history
its families, its reasons and its provenance, adds the draft requests'
table, keeps every cut already recorded exactly as it counted, and takes it
all away again.

The file is loaded by itself (the pattern of tests/test_colour_migration.py),
so it needs none of the migrations before it. Two databases are started
from: the schema the models describe with 0036's own downgrade run over it
(the schema 0036 upgrades from; a downgrade that missed anything shows up as
an upgrade that cannot add it again), and one whose `cuts` and `cut_pieces`
were made by 0028 itself and already hold a cut, which is what every
install that has used `build_rough_cut` has.
"""

import importlib.util
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import Session

from framefound.db import models
from framefound.db.base import Base
from framefound.nle import shotlist

NEW_TABLES = ("cut_drafts",)
# What 0036 adds to tables that already exist, and nothing else.
NEW_COLUMNS = {
    "cuts": {
        "root_id",
        "parent_id",
        "version",
        "sent_at",
        "model",
        "model_revision",
        "variant",
        "runtime",
        "runtime_version",
        "prompt_version",
    },
    "cut_pieces": {"note"},
}
NEW_INDEXES = {"cuts": {"ix_cuts_root_id"}}
TOUCHED = (*NEW_TABLES, *NEW_COLUMNS)
# The tables 0028 made; the 0028 database makes them with 0028's own code.
MADE_BY_0028 = ("shot_windows", "cuts", "cut_pieces")
VERSIONS = Path(__file__).parents[1] / "migrations" / "versions"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{name[:4]}", VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(connection: sa.Connection, step: str, name: str = "0036_cut_drafts.py") -> None:
    migration = load(name)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, step)()


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[sa.Engine]:
    """A database as it stood at 0035: the models' schema less 0036's part.

    Foreign keys are off while migrating, as under alembic: SQLite rebuilds
    a table to drop a column, and dropping the old copy with them on would
    delete the rows that point at it.
    """
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        run(connection, "downgrade")
    yield engine
    engine.dispose()


@pytest.fixture()
def recorded(tmp_path: Path) -> Iterator[sa.Engine]:
    """A database whose cut history 0028 made, holding one cut of two pieces."""
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'recorded.db').as_posix()}")
    others = [
        table
        for name, table in Base.metadata.tables.items()
        if name not in (*MADE_BY_0028, *NEW_TABLES)
    ]
    Base.metadata.create_all(engine, tables=others)
    with engine.begin() as connection:
        run(connection, "upgrade", "0028_shot_windows_and_cuts.py")
        catalogue(connection)
        put(
            connection,
            "cuts",
            id=hexid("5"),
            title="Brookside Quick 30",
            client="GELCO",
            library_id=hexid("2"),
            source="mcp",
            duration_s=9.0,
            piece_count=2,
            created_by=hexid("1"),
        )
        put(connection, "cuts", id=hexid("6"), title="Members' day", source="import")
        for digit, position, clip in (("7", 0, "3"), ("8", 1, "4")):
            put(
                connection,
                "cut_pieces",
                id=hexid(digit),
                cut_id=hexid("5"),
                position=position,
                asset_id=hexid(clip),
                filename=f"DJI_06{digit}9.MP4",
                in_ms=3000,
                out_ms=7500,
                shot={"move": "orbit", "light": "golden hour"},
            )
    yield engine
    engine.dispose()


def keys_on(engine: sa.Engine) -> sa.Connection:
    connection = engine.connect()
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    connection.commit()
    return connection


def hexid(digit: str) -> str:
    return digit * 32


def type_text(kind: Any) -> str:
    return str(kind.compile(dialect=sqlite.dialect()))


def put(connection: sa.Connection, table: str, **values: Any) -> None:
    """Insert a row the way the application writes it: every NOT NULL column
    left out gets the model's Python default. The table is read from the
    database, so a row can be written before the upgrade."""
    made = sa.Table(table, sa.MetaData(), autoload_with=connection)
    described = Base.metadata.tables[table]
    row = dict(values)
    for column in made.columns:
        if column.name in row or column.nullable or column.primary_key:
            continue
        default = described.columns[column.name].default
        if isinstance(default, sa.ColumnDefault):
            row[column.name] = default.arg(None) if callable(default.arg) else default.arg
    connection.execute(made.insert().values(**row))


def catalogue(connection: sa.Connection) -> None:
    """A user, a library and two clips."""
    put(connection, "users", id=hexid("1"), email="owner@example.com", password_hash="x")
    put(connection, "libraries", id=hexid("2"), name="GELCO", root_path="/m")
    for digit, name in (("3", "DJI_0639.MP4"), ("4", "DJI_0640.MP4")):
        put(
            connection,
            "assets",
            id=hexid(digit),
            library_id=hexid("2"),
            relative_path=f"Pete Dye/{name}",
            filename=name,
            extension="mp4",
            media_type="video",
            size_bytes=1,
            mtime=datetime(2026, 10, 3, 17, 42, 10, tzinfo=UTC),
        )


def shape(connection: sa.Connection, table: str) -> dict[str, Any]:
    """Everything about a table that a migration decides, as made."""
    inspector = sa.inspect(connection)
    return {
        "columns": {
            column["name"]: (column["nullable"], type_text(column["type"]))
            for column in inspector.get_columns(table)
        },
        "primary": inspector.get_pk_constraint(table)["constrained_columns"],
        "foreign": {
            (key["constrained_columns"][0], key["referred_table"], key["options"].get("ondelete"))
            for key in inspector.get_foreign_keys(table)
        },
        "indexes": {
            (index["name"], tuple(index["column_names"])) for index in inspector.get_indexes(table)
        },
        "unique": {
            (key["name"], tuple(key["column_names"]))
            for key in inspector.get_unique_constraints(table)
        },
    }


def modelled(table: str) -> dict[str, Any]:
    """The same, as the models describe it."""
    described = Base.metadata.tables[table]
    return {
        "columns": {
            column.name: (column.nullable, type_text(column.type)) for column in described.columns
        },
        "primary": [column.name for column in described.primary_key.columns],
        "foreign": {
            (key.parent.name, key.column.table.name, key.ondelete) for key in described.foreign_keys
        },
        "indexes": {
            (index.name, tuple(column.name for column in index.columns))
            for index in described.indexes
        },
        "unique": {
            (constraint.name, tuple(column.name for column in constraint.columns))
            for constraint in described.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        },
    }


def agrees_with_the_models(connection: sa.Connection) -> None:
    for table in TOUCHED:
        made, wanted = shape(connection, table), modelled(table)
        for part in ("columns", "primary", "foreign", "indexes", "unique"):
            assert made[part] == wanted[part], f"{table} {part}"

    # A NOT NULL value with no default of its own in the database would make
    # every row written by hand, or by an older server, fail. Nothing new
    # here is always written by its writer, so every one has a default.
    inspector = sa.inspect(connection)
    for table in TOUCHED:
        described = Base.metadata.tables[table]
        defaults = {column["name"]: column["default"] for column in inspector.get_columns(table)}
        for column in described.columns:
            new_here = table in NEW_TABLES or column.name in NEW_COLUMNS[table]
            if new_here and not column.nullable and not column.primary_key:
                assert defaults[column.name] is not None, f"{table}.{column.name}"


def test_it_follows_the_colour_migration_and_has_its_number_to_itself() -> None:
    migration = load("0036_cut_drafts.py")
    assert (migration.revision, migration.down_revision) == ("0036", "0035")
    assert load("0035_colour.py").revision == "0035"
    # One file per number, and only one file after 0035: anything else is
    # two heads and an upgrade that will not run.
    assert [path.name for path in VERSIONS.glob("0036_*.py")] == ["0036_cut_drafts.py"]
    after = [
        path.name
        for path in sorted(VERSIONS.glob("0*.py"))
        if load(path.name).down_revision == "0035"
    ]
    assert after == ["0036_cut_drafts.py"]


def test_the_database_it_starts_from_has_cut_history_as_it_was(database: sa.Engine) -> None:
    with database.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        assert not tables & set(NEW_TABLES)
        for table, added in NEW_COLUMNS.items():
            made, wanted = shape(connection, table), modelled(table)
            assert set(made["columns"]) == set(wanted["columns"]) - added, table
            assert made["indexes"] == {
                index for index in wanted["indexes"] if index[0] not in NEW_INDEXES.get(table, ())
            }, table


def test_the_migration_makes_what_the_models_describe(database: sa.Engine) -> None:
    with database.begin() as connection:
        run(connection, "upgrade")
        agrees_with_the_models(connection)


def test_cuts_made_by_0028_are_kept_as_first_versions_that_count_as_before(
    recorded: sa.Engine,
) -> None:
    with recorded.begin() as connection:
        run(connection, "upgrade")
        agrees_with_the_models(connection)
        cuts = {
            row["title"]: row
            for row in connection.execute(sa.text("SELECT * FROM cuts")).mappings()
        }
        notes = connection.execute(
            sa.text("SELECT position, note, in_ms, shot FROM cut_pieces ORDER BY position")
        ).all()

    quick = cuts["Brookside Quick 30"]
    assert (quick["client"], quick["source"], quick["piece_count"], quick["duration_s"]) == (
        "GELCO",
        "mcp",
        2,
        9.0,
    )
    for row in cuts.values():
        # A family of its own, never sent from the Cuts page, drafted by nobody.
        assert (row["root_id"], row["parent_id"], row["version"], row["sent_at"]) == (
            None,
            None,
            1,
            None,
        )
        provenance = ("model", "model_revision", "runtime", "runtime_version", "prompt_version")
        assert [row[name] for name in provenance] == [None] * 5
        assert row["variant"] == ""
    assert [(position, note, in_ms) for position, note, in_ms, _ in notes] == [
        (0, "", 3000),
        (1, "", 3000),
    ]
    assert json.loads(notes[0][3]) == {"move": "orbit", "light": "golden hour"}

    # Claude's cuts and imported timelines count exactly as before.
    with Session(recorded) as session:
        counted = session.scalars(
            sa.select(models.Cut.title).where(shotlist.counts_as_used())
        ).all()
        assert sorted(counted) == ["Brookside Quick 30", "Members' day"]
        cut = session.get(models.Cut, uuid.UUID(hexid("5")))
        assert cut is not None
        assert [(piece.position, piece.note) for piece in cut.pieces] == [(0, ""), (1, "")]


def test_a_draft_written_with_only_what_it_needs_takes_the_defaults(database: sa.Engine) -> None:
    with database.begin() as connection:
        run(connection, "upgrade")
        connection.execute(sa.text("INSERT INTO cut_drafts (id) VALUES (:id)"), {"id": hexid("9")})
        draft = connection.execute(sa.text("SELECT * FROM cut_drafts")).mappings().one()

    # Waiting for the job, asking about nothing yet, from the web page.
    assert (draft["state"], draft["message"], draft["title"], draft["brief"]) == (
        "queued",
        "",
        "",
        "",
    )
    assert (draft["target_s"], draft["min_shot_s"], draft["max_shot_s"]) == (30.0, 1.5, 8.0)
    assert (draft["exclude_used"], draft["shots_in_scope"], draft["clips"]) == (0, 0, 0)
    assert [json.loads(draft[name]) for name in ("scope", "candidates", "problems")] == [
        {},
        [],
        [],
    ]
    assert (draft["origin"], draft["demo"], draft["variant"]) == ("web", 0, "")
    nothing = (
        "answer_text",
        "cut_id",
        "revises_cut_id",
        "job_id",
        "model",
        "model_revision",
        "runtime",
        "runtime_version",
        "prompt_version",
        "seconds",
        "requested_by",
        "started_at",
        "finished_at",
    )
    assert [draft[name] for name in nothing] == [None] * len(nothing)
    assert draft["created_at"] is not None


def test_the_models_read_and_write_the_migrated_tables(database: sa.Engine) -> None:
    with database.begin() as connection:
        run(connection, "upgrade")
        catalogue(connection)
        # Written by hand: the database's defaults must read back as the
        # models' types, not as text.
        connection.execute(sa.text("INSERT INTO cut_drafts (id) VALUES (:id)"), {"id": hexid("9")})

    with Session(database) as session:
        by_hand = session.get(models.CutDraft, uuid.UUID(hexid("9")))
        assert by_hand is not None
        assert (by_hand.scope, by_hand.candidates, by_hand.problems) == ({}, [], [])
        assert (by_hand.exclude_used, by_hand.demo) == (False, False)

        first = models.Cut(title="Pete Dye, first try", source="local")
        session.add(first)
        session.flush()
        second = models.Cut(
            title="Pete Dye, reviewed",
            source="mcp",
            root_id=first.id,
            parent_id=first.id,
            version=2,
            model="mlx-community/gemma-4-12B-it-OptiQ-4bit",
            model_revision="a" * 40,
            variant="Q4_K_M",
            runtime="mlx-vlm",
            runtime_version="0.4.0",
            prompt_version="editor-1",
            sent_at=datetime(2026, 11, 13, 9, 0, tzinfo=UTC),
        )
        second.pieces.append(
            models.CutPiece(
                asset_id=uuid.UUID(hexid("3")),
                filename="DJI_0639.MP4",
                in_ms=3000,
                out_ms=7000,
                note="opens on the green",
            )
        )
        draft = models.CutDraft(
            title="Pete Dye, first try",
            brief="Thirty seconds of golden orbits",
            scope={"kind": "folder", "folder": "2026/Pete Dye", "label": "GELCO › 2026/Pete Dye"},
            candidates=[
                {
                    "n": 1,
                    "asset_id": hexid("3"),
                    "filename": "DJI_0639.MP4",
                    "start_ms": 2000,
                    "end_ms": 14000,
                    "view": "aerial",
                    "line": "1 DJI_0639 [aerial] 2.0-14.0s orbit right smooth .95",
                }
            ],
            problems=["Shot 7 is not on the list; it was left out."],
            cut_id=first.id,
            requested_by=uuid.UUID(hexid("1")),
            origin="chat",
        )
        session.add_all([second, draft])
        session.commit()
        ids = (first.id, second.id, draft.id)

    with Session(database) as session:
        first_back, second_back = (session.get(models.Cut, cut_id) for cut_id in ids[:2])
        draft_back = session.get(models.CutDraft, ids[2])
        assert first_back is not None and second_back is not None and draft_back is not None
        assert (first_back.version, first_back.root_id, first_back.variant) == (1, None, "")
        assert (second_back.root_id, second_back.parent_id, second_back.version) == (
            ids[0],
            ids[0],
            2,
        )
        assert (second_back.variant, second_back.prompt_version) == ("Q4_K_M", "editor-1")
        assert second_back.sent_at is not None
        assert [piece.note for piece in second_back.pieces] == ["opens on the green"]
        assert draft_back.scope["label"] == "GELCO › 2026/Pete Dye"
        assert draft_back.candidates[0]["n"] == 1
        assert draft_back.problems == ["Shot 7 is not on the list; it was left out."]
        assert (draft_back.cut_id, draft_back.origin, draft_back.state) == (
            ids[0],
            "chat",
            "queued",
        )


def test_a_deleted_cut_leaves_its_request_and_its_later_versions(database: sa.Engine) -> None:
    with database.begin() as connection:
        run(connection, "upgrade")
    with keys_on(database) as connection, connection.begin():
        catalogue(connection)
        put(connection, "cuts", id=hexid("5"), title="First try", source="local")
        put(
            connection,
            "cuts",
            id=hexid("6"),
            title="Second try",
            source="local",
            root_id=hexid("5"),
            parent_id=hexid("5"),
            version=2,
        )
        put(
            connection,
            "cut_drafts",
            id=hexid("9"),
            cut_id=hexid("5"),
            revises_cut_id=hexid("6"),
            job_id=hexid("a"),
            requested_by=hexid("1"),
            state="done",
        )

    with keys_on(database) as connection, connection.begin():
        connection.execute(sa.text("DELETE FROM cuts WHERE id = :id"), {"id": hexid("5")})
        # Version 2 is still in the family of the deleted first version ...
        later = connection.execute(sa.text("SELECT root_id, parent_id, version FROM cuts")).one()
        assert tuple(later) == (hexid("5"), hexid("5"), 2)
        # ... and the request says what it asked for, no longer what it became.
        draft = connection.execute(sa.text("SELECT * FROM cut_drafts")).mappings().one()
        assert (draft["cut_id"], draft["state"], draft["revises_cut_id"]) == (
            None,
            "done",
            hexid("6"),
        )
        # Who asked leaves; the request stays.
        connection.execute(sa.text("DELETE FROM users"))
        assert connection.execute(sa.text("SELECT requested_by FROM cut_drafts")).scalar() is None
        # Neither the cut it revises nor its job row is a foreign key.
        connection.execute(sa.text("DELETE FROM cuts"))
        assert connection.execute(sa.text("SELECT job_id FROM cut_drafts")).scalar() == hexid("a")


def test_downgrade_takes_away_exactly_what_upgrade_made(database: sa.Engine) -> None:
    with database.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        before = {table: shape(connection, table) for table in NEW_COLUMNS}
    with database.begin() as connection:
        catalogue(connection)
        put(connection, "cuts", id=hexid("5"), title="Quick 30")
        put(connection, "cut_pieces", id=hexid("7"), cut_id=hexid("5"), asset_id=hexid("3"))
        run(connection, "upgrade")
        assert set(sa.inspect(connection).get_table_names()) == tables | set(NEW_TABLES)
        run(connection, "downgrade")
        assert set(sa.inspect(connection).get_table_names()) == tables
        assert {table: shape(connection, table) for table in NEW_COLUMNS} == before
        # Going back keeps the history that was there.
        assert connection.execute(sa.text("SELECT title FROM cuts")).scalar() == "Quick 30"
        assert connection.execute(sa.text("SELECT cut_id FROM cut_pieces")).scalar() == hexid("5")
