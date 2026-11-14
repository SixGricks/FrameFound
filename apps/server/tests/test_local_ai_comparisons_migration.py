"""Migration 0033 (migrations/versions/0033_local_ai_comparisons.py) makes the
four tables of Compare as the models describe them, and takes them away again.

The file is loaded by itself and run on a plain SQLite file (the pattern of
tests/test_local_models_migration.py), so it needs none of the migrations
before it. Every other test builds the schema from the models, so a column
the migration makes differently would only show up on a real upgrade: that
is what this file is for.
"""

import importlib.util
from collections.abc import Iterator
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

TABLES = (
    "local_ai_comparisons",
    "local_ai_comparison_items",
    "local_ai_comparison_models",
    "local_ai_comparison_answers",
)
# NOT NULL columns that every writer fills in, so a database default would
# only hide a writer that forgot: the keys, what the sample is and which
# build answered under which question.
ALWAYS_WRITTEN = {
    "name",
    "source_kind",
    "prompt_version",
    "comparison_id",
    "position",
    "kind",
    "model",
    "model_revision",
    "entry_id",
    "item_id",
}
VERSIONS = Path(__file__).parents[1] / "migrations" / "versions"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{name[:4]}", VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[sa.Engine]:
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}")

    # SQLite enforces foreign keys only when asked, per connection.
    @sa.event.listens_for(engine, "connect")
    def _keys_on(connection: Any, _record: Any) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        # What the new tables point at, as far as they need it.
        for table in ("users", "assets", "local_models"):
            connection.execute(sa.text(f"CREATE TABLE {table} (id CHAR(32) PRIMARY KEY)"))
    yield engine
    engine.dispose()


def upgraded(connection: sa.Connection) -> ModuleType:
    migration = load("0033_local_ai_comparisons.py")
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    return migration


def type_text(kind: Any) -> str:
    return str(kind.compile(dialect=sqlite.dialect()))


def hexid(digit: str) -> str:
    return digit * 32


def test_it_follows_the_local_models_migration_and_has_its_number_to_itself() -> None:
    migration = load("0033_local_ai_comparisons.py")
    assert (migration.revision, migration.down_revision) == ("0033", "0032")
    assert load("0032_local_models.py").revision == "0032"
    # One file per number: a second 0033 would make two heads.
    assert [path.name for path in VERSIONS.glob("0033_*.py")] == ["0033_local_ai_comparisons.py"]


def test_the_migration_makes_the_tables_the_models_describe(database: sa.Engine) -> None:
    with database.begin() as connection:
        upgraded(connection)
        inspector = sa.inspect(connection)
        for table in TABLES:
            modelled = Base.metadata.tables[table]
            made = {column["name"]: column for column in inspector.get_columns(table)}
            assert set(made) == {column.name for column in modelled.columns}, table
            for column in modelled.columns:
                found = made[column.name]
                where = f"{table}.{column.name}"
                assert found["nullable"] == column.nullable, where
                assert type_text(found["type"]) == type_text(column.type), where
                # A NOT NULL value with no default of its own in the database
                # would make every row written by hand (or by an older
                # server) fail. JSON is always written by the ORM.
                needs_default = (
                    not column.nullable
                    and not column.primary_key
                    and not isinstance(column.type, sa.JSON)
                    and column.name not in ALWAYS_WRITTEN
                )
                if needs_default:
                    assert found["default"] is not None, where

            primary = inspector.get_pk_constraint(table)["constrained_columns"]
            assert primary == [column.name for column in modelled.primary_key.columns], table

            wanted_keys = {
                (key.parent.name, key.column.table.name, key.ondelete)
                for key in modelled.foreign_keys
            }
            made_keys = {
                (
                    key["constrained_columns"][0],
                    key["referred_table"],
                    key["options"].get("ondelete"),
                )
                for key in inspector.get_foreign_keys(table)
            }
            assert made_keys == wanted_keys, table

            wanted_indexes = {
                (index.name, tuple(column.name for column in index.columns))
                for index in modelled.indexes
            }
            made_indexes = {
                (index["name"], tuple(index["column_names"]))
                for index in inspector.get_indexes(table)
            }
            assert made_indexes == wanted_indexes, table

            wanted_unique = {
                (constraint.name, tuple(column.name for column in constraint.columns))
                for constraint in modelled.constraints
                if isinstance(constraint, sa.UniqueConstraint)
            }
            made_unique = {
                (key["name"], tuple(key["column_names"]))
                for key in inspector.get_unique_constraints(table)
            }
            assert made_unique == wanted_unique, table


def _comparison(connection: sa.Connection, digit: str = "1") -> str:
    connection.execute(
        sa.text(
            "INSERT INTO local_ai_comparisons (id, name, source_kind, source, prompt_version,"
            " vocabulary, counted_cuts, counts) VALUES (:id, 'Brookside', 'folder', '{}',"
            " 'look-1', '[]', '[]', '{}')"
        ),
        {"id": hexid(digit)},
    )
    return hexid(digit)


def _item(connection: sa.Connection, comparison: str, digit: str, position: int) -> str:
    connection.execute(
        sa.text(
            "INSERT INTO local_ai_comparison_items (id, comparison_id, position, kind, facts,"
            " picture_ms, truth_yes, truth_no, used_in) VALUES (:id, :comparison, :position,"
            " 'shot', '{}', '[]', '[]', '[]', '[]')"
        ),
        {"id": hexid(digit), "comparison": comparison, "position": position},
    )
    return hexid(digit)


def _column(
    connection: sa.Connection,
    comparison: str,
    digit: str,
    *,
    variant: str = "",
    model_id: str | None = None,
) -> str:
    connection.execute(
        sa.text(
            "INSERT INTO local_ai_comparison_models (id, comparison_id, model_id, model,"
            " model_revision, variant, prompt_version) VALUES (:id, :comparison, :model_id,"
            " 'mlx-community/demo-small-4bit', :revision, :variant, 'look-1')"
        ),
        {
            "id": hexid(digit),
            "comparison": comparison,
            "model_id": model_id,
            "revision": "c" * 40,
            "variant": variant,
        },
    )
    return hexid(digit)


def _answer(connection: sa.Connection, entry: str, item: str, digit: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO local_ai_comparison_answers (id, entry_id, item_id, tags)"
            " VALUES (:id, :entry, :item, '[]')"
        ),
        {"id": hexid(digit), "entry": entry, "item": item},
    )


def test_a_row_written_with_only_what_it_needs_takes_the_defaults(database: sa.Engine) -> None:
    with database.begin() as connection:
        upgraded(connection)
        comparison = _comparison(connection)
        item = _item(connection, comparison, "2", 0)
        entry = _column(connection, comparison, "3")
        _answer(connection, entry, item, "4")
        made = connection.execute(sa.text("SELECT * FROM local_ai_comparisons")).mappings().one()
        shot = connection.execute(sa.text("SELECT * FROM local_ai_comparison_items")).mappings()
        column = connection.execute(sa.text("SELECT * FROM local_ai_comparison_models"))
        answer = connection.execute(sa.text("SELECT * FROM local_ai_comparison_answers"))
        shot_row, column_row, answer_row = (
            shot.one(),
            column.mappings().one(),
            answer.mappings().one(),
        )

    assert (made["what"], made["size"], made["stills"], made["seed"]) == ("both", 0, 3, 0)
    assert (made["verdict"], made["verdict_note"], made["source_label"]) == ("", "", "")
    assert made["truth_at"] is not None and made["created_at"] is not None
    assert (made["verdict_entry_id"], made["verdict_at"], made["verdict_by"]) == (None, None, None)

    assert (shot_row["filename"], shot_row["facts_line"], shot_row["media_type"]) == ("", "", "")
    assert (shot_row["asset_id"], shot_row["used"], shot_row["start_ms"]) == (None, None, None)

    assert (column_row["state"], column_row["message"], column_row["model_name"]) == (
        "queued",
        "",
        "",
    )
    assert (column_row["done"], column_row["failed"], column_row["variant"]) == (0, 0, "")
    assert (column_row["job_id"], column_row["runtime"], column_row["memory_gb"]) == (
        None,
        None,
        None,
    )
    assert column_row["created_at"] is not None

    assert (answer_row["ok"], answer_row["attempts"], answer_row["message"]) == (0, 1, "")
    assert (answer_row["score"], answer_row["problem"], answer_row["raw"]) == (None, None, None)
    assert answer_row["created_at"] is not None


def test_a_model_answers_an_item_once(database: sa.Engine) -> None:
    with database.begin() as connection:
        upgraded(connection)
        comparison = _comparison(connection)
        item = _item(connection, comparison, "2", 0)
        entry = _column(connection, comparison, "3")
        _answer(connection, entry, item, "4")
    # A second delivery that asked again must not leave two answers.
    with pytest.raises(sa.exc.IntegrityError), database.begin() as connection:
        _answer(connection, entry, item, "5")


def test_two_items_cannot_share_a_position_because_it_names_their_pictures(
    database: sa.Engine,
) -> None:
    with database.begin() as connection:
        upgraded(connection)
        comparison = _comparison(connection)
        _item(connection, comparison, "2", 0)
        other = _comparison(connection, "9")
        # Another comparison's item 0 is its own file in its own folder.
        _item(connection, other, "3", 0)
    with pytest.raises(sa.exc.IntegrityError), database.begin() as connection:
        _item(connection, comparison, "4", 0)


def test_the_same_build_is_one_column_but_two_builds_of_one_commit_are_two(
    database: sa.Engine,
) -> None:
    with database.begin() as connection:
        upgraded(connection)
        comparison = _comparison(connection)
        _column(connection, comparison, "2", variant="Q4_K_M")
        # One repository at one commit holding two quantisations is two builds.
        _column(connection, comparison, "3", variant="Q8_0")
    with pytest.raises(sa.exc.IntegrityError), database.begin() as connection:
        _column(connection, comparison, "4", variant="Q4_K_M")


def test_a_removed_model_keeps_its_column_and_a_deleted_comparison_takes_everything(
    database: sa.Engine,
) -> None:
    with database.begin() as connection:
        upgraded(connection)
        connection.execute(
            sa.text("INSERT INTO local_models (id) VALUES (:id)"), {"id": hexid("8")}
        )
        connection.execute(sa.text("INSERT INTO assets (id) VALUES (:id)"), {"id": hexid("7")})
        comparison = _comparison(connection)
        item = _item(connection, comparison, "2", 0)
        connection.execute(
            sa.text("UPDATE local_ai_comparison_items SET asset_id = :asset"),
            {"asset": hexid("7")},
        )
        entry = _column(connection, comparison, "3", model_id=hexid("8"))
        _answer(connection, entry, item, "4")

    with database.begin() as connection:
        connection.execute(sa.text("DELETE FROM local_models"))
        connection.execute(sa.text("DELETE FROM assets"))
        column = connection.execute(
            sa.text("SELECT model_id, model FROM local_ai_comparison_models")
        ).one()
        shot = connection.execute(
            sa.text("SELECT asset_id, kind FROM local_ai_comparison_items")
        ).one()
        answers = connection.execute(
            sa.text("SELECT count(*) FROM local_ai_comparison_answers")
        ).scalar_one()
    # The column, the item and what was answered all stay; only the links go.
    assert tuple(column) == (None, "mlx-community/demo-small-4bit")
    assert tuple(shot) == (None, "shot")
    assert answers == 1

    with database.begin() as connection:
        connection.execute(sa.text("DELETE FROM local_ai_comparisons"))
        left = {
            table: connection.execute(
                sa.text(f"SELECT count(*) FROM {table}")  # noqa: S608 - names from TABLES above
            ).scalar_one()
            for table in TABLES
        }
    assert left == dict.fromkeys(TABLES, 0)


def test_downgrade_takes_away_exactly_what_upgrade_made(database: sa.Engine) -> None:
    with database.begin() as connection:
        before = set(sa.inspect(connection).get_table_names())
        migration = upgraded(connection)
        assert set(sa.inspect(connection).get_table_names()) == before | set(TABLES)
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert set(sa.inspect(connection).get_table_names()) == before
        # The indexes went with their tables, and no other table was touched.
        for table in before:
            assert sa.inspect(connection).get_indexes(table) == []
