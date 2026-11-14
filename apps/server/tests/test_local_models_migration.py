"""Migration 0032 (migrations/versions/0032_local_models.py) makes the tables
the models describe, and takes them away again.

The file is loaded by itself and run on a plain SQLite file (the pattern of
tests/test_chat.py), so it needs none of the migrations before it. The
tests elsewhere build the schema from the models, so a column the migration
makes differently from the model would only show up on a real upgrade:
that is what this file is for.
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

TABLES = ("local_models", "local_ai_roles", "local_ai_jobs")
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
    yield engine
    engine.dispose()


def upgraded(connection: sa.Connection) -> ModuleType:
    migration = load("0032_local_models.py")
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    return migration


def type_text(kind: Any) -> str:
    return str(kind.compile(dialect=sqlite.dialect()))


def test_it_follows_the_chat_migration_and_nothing_follows_it_yet() -> None:
    migration = load("0032_local_models.py")
    assert (migration.revision, migration.down_revision) == ("0032", "0031")
    # 0031 is the chat panel's, and is what this one names as before it.
    assert load("0031_chat.py").revision == "0031"
    # One file per number: a second 0032 would make two heads.
    assert [path.name for path in VERSIONS.glob("0032_*.py")] == ["0032_local_models.py"]


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
                # server) fail. JSON and the keys are always written.
                needs_default = (
                    not column.nullable
                    and not column.primary_key
                    and not isinstance(column.type, sa.JSON)
                    and column.name not in ("repo_id", "repo_key", "revision", "publisher")
                    and column.name not in ("runtime", "kind")
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
            assert wanted_indexes <= made_indexes, table


def test_a_build_is_one_row_whatever_the_capitals_of_its_name(database: sa.Engine) -> None:
    with database.begin() as connection:
        upgraded(connection)
        unique = sa.inspect(connection).get_unique_constraints("local_models")
    assert [(key["name"], key["column_names"]) for key in unique] == [
        ("uq_local_models_build", ["repo_key", "revision", "variant"])
    ]
    modelled = {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in Base.metadata.tables["local_models"].constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert modelled == {("uq_local_models_build", ("repo_key", "revision", "variant"))}

    insert = sa.text(
        "INSERT INTO local_models (id, repo_id, repo_key, revision, publisher, chain, runtime,"
        " takes, files) VALUES (:id, :repo_id, :repo_key, :revision, 'mlx-community', '[]',"
        " 'mlx-vlm', '[]', '[]')"
    )
    with database.begin() as connection:
        connection.execute(
            insert,
            {"id": "1" * 32, "repo_id": "a/B", "repo_key": "a/b", "revision": "c" * 40},
        )
    with pytest.raises(sa.exc.IntegrityError), database.begin() as connection:
        connection.execute(
            insert,
            {"id": "2" * 32, "repo_id": "A/b", "repo_key": "a/b", "revision": "c" * 40},
        )


def test_a_row_written_with_only_what_it_needs_takes_the_defaults(database: sa.Engine) -> None:
    with database.begin() as connection:
        upgraded(connection)
        connection.execute(
            sa.text(
                "INSERT INTO local_models (id, repo_id, repo_key, revision, publisher, chain,"
                " runtime, takes, files) VALUES ('"
                + "1" * 32
                + "', 'a/b', 'a/b', '"
                + "c" * 40
                + "', 'a', '[]', 'mlx-vlm', '[]', '[]')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO local_ai_jobs (id, kind, payload) VALUES ('"
                + "2" * 32
                + "', 'download', '{}')"
            )
        )
        connection.execute(sa.text("INSERT INTO local_ai_roles (role) VALUES ('looker')"))
        model = connection.execute(sa.text("SELECT * FROM local_models")).mappings().one()
        job = connection.execute(sa.text("SELECT * FROM local_ai_jobs")).mappings().one()
        role = connection.execute(sa.text("SELECT * FROM local_ai_roles")).mappings().one()

    assert (model["variant"], model["state"], model["gated"], model["licence"]) == (
        "",
        "downloading",
        "",
        "",
    )
    assert (model["size_bytes"], model["memory_gb"], model["measured_memory_gb"]) == (0, 0, None)
    assert model["created_at"] is not None and model["updated_at"] is not None
    assert (job["state"], job["priority"], job["phase"], job["message"]) == ("queued", 5, "", "")
    assert (job["done"], job["total"], job["cancel_requested"], job["subject"]) == (0, 0, 0, "")
    assert job["created_at"] is not None and job["not_before"] is None
    assert (role["model_id"], role["pending_model_id"]) == (None, None)


def test_downgrade_takes_away_exactly_what_upgrade_made(database: sa.Engine) -> None:
    with database.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id CHAR(32) PRIMARY KEY)"))
        before = set(sa.inspect(connection).get_table_names())
        migration = upgraded(connection)
        assert set(sa.inspect(connection).get_table_names()) == before | set(TABLES)
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert set(sa.inspect(connection).get_table_names()) == before
        # The indexes went with their table, and no other was touched.
        assert sa.inspect(connection).get_indexes("users") == []
