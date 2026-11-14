"""Migration 0034 (migrations/versions/0034_looks.py) makes looking's two
tables and its columns on `assets`, `asset_tags` and `libraries` as the
models describe them, keeps every existing row, and takes it all away again.

The file is loaded by itself (the pattern of
tests/test_local_ai_comparisons_migration.py), so it needs none of the
migrations before it. The database it starts from is the schema the models
describe with 0034's own downgrade run over it: that is the schema 0034
upgrades from, and a downgrade that missed anything shows up as an upgrade
that cannot add it again.
"""

import importlib.util
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

from framefound.db import models  # noqa: F401  (register every table)
from framefound.db.base import Base

NEW_TABLES = ("look_requests", "looks")
# What 0034 adds to tables that already exist, and nothing else.
NEW_COLUMNS = {
    "assets": {"look_score", "look_caption", "look_reason", "looked_at", "looked_by"},
    "asset_tags": {"look_id", "at_ms"},
    "libraries": {"look_at_ingest", "look_since"},
}
NEW_INDEXES = {"assets": {"ix_assets_looked_at"}, "asset_tags": {"ix_asset_tags_look_id"}}
TOUCHED = (*NEW_TABLES, *NEW_COLUMNS)
# NOT NULL columns every writer fills in: a database default would only
# hide a writer that forgot.
ALWAYS_WRITTEN = {"library_id", "asset_id"}
VERSIONS = Path(__file__).parents[1] / "migrations" / "versions"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{name[:4]}", VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(connection: sa.Connection, step: str) -> None:
    migration = load("0034_looks.py")
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, step)()


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[sa.Engine]:
    """A database as it stood at 0033: the models' schema less 0034's part.

    Foreign keys are off while migrating, as under alembic: SQLite rebuilds
    a table to alter it, and dropping the old copy with them on would
    delete the rows that point at it.
    """
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        run(connection, "downgrade")
    yield engine
    engine.dispose()


def keys_on(engine: sa.Engine) -> sa.Connection:
    connection = engine.connect()
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    connection.commit()
    return connection


def put(connection: sa.Connection, table: str, **values: Any) -> None:
    """Insert a catalogue row the way the application writes it.

    The models carry Python defaults the database does not have, so every
    NOT NULL column left out is given the model's default. The table is
    read from the database, so a row can be written before the upgrade.
    """
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


def library(connection: sa.Connection, digit: str) -> str:
    put(connection, "libraries", id=hexid(digit), name=f"Library {digit}", root_path="/m")
    return hexid(digit)


def asset(connection: sa.Connection, digit: str, library_id: str, path: str) -> str:
    put(
        connection,
        "assets",
        id=hexid(digit),
        library_id=library_id,
        relative_path=path,
        filename=path.rsplit("/", 1)[-1],
        extension="jpg",
        media_type="image",
        size_bytes=1,
        mtime=datetime(2026, 10, 3, 17, 42, 10, tzinfo=UTC),
    )
    return hexid(digit)


def tag(connection: sa.Connection, digit: str) -> str:
    put(connection, "tags", id=hexid(digit), name="Green", slug="green")
    return hexid(digit)


def type_text(kind: Any) -> str:
    return str(kind.compile(dialect=sqlite.dialect()))


def hexid(digit: str) -> str:
    return digit * 32


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


def test_it_follows_the_comparisons_migration_and_has_its_number_to_itself() -> None:
    migration = load("0034_looks.py")
    assert (migration.revision, migration.down_revision) == ("0034", "0033")
    assert load("0033_local_ai_comparisons.py").revision == "0033"
    # One file per number: a second 0034 would make two heads.
    assert [path.name for path in VERSIONS.glob("0034_*.py")] == ["0034_looks.py"]


def test_the_database_it_starts_from_is_the_models_without_looking(database: sa.Engine) -> None:
    with database.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        assert not tables & set(NEW_TABLES)
        for table, added in NEW_COLUMNS.items():
            made = shape(connection, table)
            wanted = modelled(table)
            assert set(made["columns"]) == set(wanted["columns"]) - added, table
            assert made["indexes"] == {
                index for index in wanted["indexes"] if index[0] not in NEW_INDEXES.get(table, ())
            }, table
            assert made["foreign"] == {key for key in wanted["foreign"] if key[0] not in added}


def test_the_migration_makes_what_the_models_describe(database: sa.Engine) -> None:
    with database.begin() as connection:
        run(connection, "upgrade")
        for table in TOUCHED:
            made, wanted = shape(connection, table), modelled(table)
            for part in ("columns", "primary", "foreign", "indexes", "unique"):
                assert made[part] == wanted[part], f"{table} {part}"

        # A NOT NULL value with no default of its own in the database would
        # make every row written by hand, or by an older server, fail.
        inspector = sa.inspect(connection)
        for table in TOUCHED:
            described = Base.metadata.tables[table]
            defaults = {
                column["name"]: column["default"] for column in inspector.get_columns(table)
            }
            for column in described.columns:
                new_here = table in NEW_TABLES or column.name in NEW_COLUMNS[table]
                needs_default = (
                    new_here
                    and not column.nullable
                    and not column.primary_key
                    and column.name not in ALWAYS_WRITTEN
                )
                if needs_default:
                    assert defaults[column.name] is not None, f"{table}.{column.name}"


def test_what_was_there_before_is_kept_and_every_library_starts_switched_off(
    database: sa.Engine,
) -> None:
    with database.begin() as connection:
        photo = asset(connection, "2", library(connection, "1"), "Pete Dye/a.jpg")
        put(
            connection,
            "asset_tags",
            asset_id=photo,
            tag_id=tag(connection, "3"),
            source="suggested",
            origin="claude",
        )
        run(connection, "upgrade")
        kept = connection.execute(sa.text("SELECT * FROM libraries")).mappings().one()
        photo_row = connection.execute(sa.text("SELECT * FROM assets")).mappings().one()
        link = connection.execute(sa.text("SELECT * FROM asset_tags")).mappings().one()

    # Turning looking on is a decision somebody makes; no upgrade makes it.
    assert (kept["name"], kept["look_at_ingest"], kept["look_since"]) == ("Library 1", 0, None)
    assert photo_row["relative_path"] == "Pete Dye/a.jpg"
    assert [photo_row[name] for name in sorted(NEW_COLUMNS["assets"])] == [None] * 5
    assert (link["source"], link["origin"], link["look_id"], link["at_ms"]) == (
        "suggested",
        "claude",
        None,
        None,
    )


def test_a_row_written_with_only_what_it_needs_takes_the_defaults(database: sa.Engine) -> None:
    with database.begin() as connection:
        run(connection, "upgrade")
        asset(connection, "2", library(connection, "1"), "a.jpg")
        connection.execute(
            sa.text("INSERT INTO look_requests (id, library_id) VALUES (:id, :library)"),
            {"id": hexid("4"), "library": hexid("1")},
        )
        connection.execute(
            sa.text("INSERT INTO looks (id, asset_id) VALUES (:id, :asset)"),
            {"id": hexid("5"), "asset": hexid("2")},
        )
        request = connection.execute(sa.text("SELECT * FROM look_requests")).mappings().one()
        look = connection.execute(sa.text("SELECT * FROM looks")).mappings().one()

    assert (request["folder"], request["run_when"], request["state"]) == ("", "now", "active")
    assert (request["redo"], request["prepare"], request["prep_state"]) == (0, 1, "")
    counters = (
        "done",
        "total",
        "failed",
        "tags_suggested",
        "new_tags",
        "held",
        "stills_only",
        "prep_total",
        "prep_ready",
        "prep_gave_up",
    )
    assert [request[name] for name in counters] == [0] * len(counters)
    assert (request["model_id"], request["look_job_id"], request["requested_by"]) == (
        None,
        None,
        None,
    )
    assert request["created_at"] is not None and request["finished_at"] is None

    # A row with no times is a look at the whole item; it counts as a
    # success until somebody says otherwise, and as the current one.
    assert (look["start_ms"], look["end_ms"], look["ok"], look["current"]) == (None, None, 1, 1)
    assert (look["caption"], look["key"], look["model"], look["demo"]) == ("", "", "", 0)
    # A repository with one build has no variant; "" rather than NULL.
    assert (look["model_revision"], look["variant"]) == ("", "")
    assert (look["score"], look["answer"], look["request_id"]) == (None, None, None)
    assert look["created_at"] is not None


def test_what_points_at_a_look_or_a_review_outlives_it_and_an_asset_takes_its_looks(
    database: sa.Engine,
) -> None:
    with database.begin() as connection:
        run(connection, "upgrade")
    with keys_on(database) as connection, connection.begin():
        library(connection, "1")
        for digit in "23":
            asset(connection, digit, hexid("1"), f"{digit}.jpg")
        connection.execute(
            sa.text("INSERT INTO look_requests (id, library_id) VALUES (:id, :library)"),
            {"id": hexid("4"), "library": hexid("1")},
        )
        for look, owner in (("5", "2"), ("6", "3")):
            connection.execute(
                sa.text(
                    "INSERT INTO looks (id, asset_id, request_id) VALUES (:id, :asset, :request)"
                ),
                {"id": hexid(look), "asset": hexid(owner), "request": hexid("4")},
            )
        put(
            connection,
            "asset_tags",
            asset_id=hexid("3"),
            tag_id=tag(connection, "9"),
            source="suggested",
            origin="local",
            look_id=hexid("6"),
            at_ms=8000,
        )

    with keys_on(database) as connection, connection.begin():
        # The review's record goes; the looks it made are history and stay.
        connection.execute(sa.text("DELETE FROM look_requests"))
        assert connection.execute(sa.text("SELECT request_id FROM looks")).scalars().all() == [
            None,
            None,
        ]
        # A look removed by hand leaves the suggestion it made for a person to judge.
        connection.execute(sa.text("DELETE FROM looks WHERE id = :id"), {"id": hexid("6")})
        link = connection.execute(sa.text("SELECT origin, look_id, at_ms FROM asset_tags")).one()
        assert tuple(link) == ("local", None, 8000)
        # An asset that goes takes its looks with it.
        connection.execute(sa.text("DELETE FROM assets WHERE id = :id"), {"id": hexid("2")})
        assert connection.execute(sa.text("SELECT count(*) FROM looks")).scalar_one() == 0

        connection.execute(
            sa.text("INSERT INTO look_requests (id, library_id) VALUES (:id, :library)"),
            {"id": hexid("7"), "library": hexid("1")},
        )
        # A library that goes takes its reviews with it.
        connection.execute(sa.text("DELETE FROM libraries"))
        assert connection.execute(sa.text("SELECT count(*) FROM look_requests")).scalar_one() == 0


def test_downgrade_takes_away_exactly_what_upgrade_made(database: sa.Engine) -> None:
    with database.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        before = {table: shape(connection, table) for table in NEW_COLUMNS}
    with database.begin() as connection:
        run(connection, "upgrade")
        assert set(sa.inspect(connection).get_table_names()) == tables | set(NEW_TABLES)
        run(connection, "downgrade")
        assert set(sa.inspect(connection).get_table_names()) == tables
        assert {table: shape(connection, table) for table in NEW_COLUMNS} == before
