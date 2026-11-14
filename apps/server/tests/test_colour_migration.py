"""Migration 0035 (migrations/versions/0035_colour.py) makes colour's two
tables as the models describe them, keeps their one-writer rules and their
cascades, and takes them away again.

The file is loaded by itself (the pattern of tests/test_looks_migration.py),
so it needs none of the migrations before it. The database it starts from
is the schema the models describe with 0035's own downgrade run over it:
that is the schema 0035 upgrades from, and a downgrade that missed anything
shows up as an upgrade that cannot make it again.
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

TABLES = ("clip_looks", "listing_edit_suggestions")
# NOT NULL columns every writer fills in: a database default would only
# hide a writer that forgot which clip, which photograph or which kind.
ALWAYS_WRITTEN = {"asset_id", "kind", "listing_id", "item_id"}
VERSIONS = Path(__file__).parents[1] / "migrations" / "versions"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{name[:4]}", VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(connection: sa.Connection, step: str) -> None:
    migration = load("0035_colour.py")
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, step)()


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[sa.Engine]:
    """A database as it stood at 0034: the models' schema less colour's tables."""
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        run(connection, "downgrade")
    yield engine
    engine.dispose()


@pytest.fixture()
def upgraded(database: sa.Engine) -> sa.Engine:
    with database.begin() as connection:
        run(connection, "upgrade")
    return database


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
    """Insert a catalogue row the way the application writes it: every NOT
    NULL column left out gets the model's Python default."""
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
    """A user, a library, two clips, a photograph, a listing holding it."""
    put(connection, "users", id=hexid("1"), email="owner@example.com", password_hash="x")
    put(connection, "libraries", id=hexid("2"), name="GELCO", root_path="/m")
    for digit, name, kind in (("3", "DJI_0639.MP4", "video"), ("4", "DJI_0640.MP4", "video")):
        put(
            connection,
            "assets",
            id=hexid(digit),
            library_id=hexid("2"),
            relative_path=f"Pete Dye/{name}",
            filename=name,
            extension="mp4",
            media_type=kind,
            size_bytes=1,
            mtime=datetime(2026, 10, 3, 17, 42, 10, tzinfo=UTC),
        )
    put(
        connection,
        "assets",
        id=hexid("5"),
        library_id=hexid("2"),
        relative_path="130 Davis Rd/IMG_0412.jpg",
        filename="IMG_0412.jpg",
        extension="jpg",
        media_type="image",
        size_bytes=1,
        mtime=datetime(2026, 10, 3, 17, 42, 10, tzinfo=UTC),
    )
    put(connection, "listings", id=hexid("6"), name="130 Davis Rd")
    put(connection, "listing_items", id=hexid("7"), listing_id=hexid("6"), asset_id=hexid("5"))


def look(connection: sa.Connection, digit: str, clip: str, kind: str, **values: Any) -> None:
    """Write a look by hand: only the columns named, the rest the database's."""
    made = sa.Table("clip_looks", sa.MetaData(), autoload_with=connection)
    connection.execute(made.insert().values(id=hexid(digit), asset_id=clip, kind=kind, **values))


def suggestion(connection: sa.Connection, digit: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO listing_edit_suggestions (id, listing_id, item_id, asset_id)"
            " VALUES (:id, :listing, :item, :asset)"
        ),
        {"id": hexid(digit), "listing": hexid("6"), "item": hexid("7"), "asset": hexid("5")},
    )


def test_it_follows_the_looking_migration_and_has_its_number_to_itself() -> None:
    migration = load("0035_colour.py")
    assert (migration.revision, migration.down_revision) == ("0035", "0034")
    assert load("0034_looks.py").revision == "0034"
    # One file per number, and only one file after 0034: anything else is
    # two heads and an upgrade that will not run.
    assert [path.name for path in VERSIONS.glob("0035_*.py")] == ["0035_colour.py"]
    after = [
        path.name
        for path in sorted(VERSIONS.glob("0*.py"))
        if load(path.name).down_revision == "0034"
    ]
    assert after == ["0035_colour.py"]


def test_the_database_it_starts_from_has_no_colour_tables(database: sa.Engine) -> None:
    with database.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
    assert not tables & set(TABLES)
    assert {"assets", "listings", "listing_items", "users", "looks"} <= tables


def test_the_migration_makes_the_tables_the_models_describe(upgraded: sa.Engine) -> None:
    with upgraded.connect() as connection:
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
                # would make every row written by hand, or by an older
                # server, fail.
                needs_default = (
                    not column.nullable
                    and not column.primary_key
                    and column.name not in ALWAYS_WRITTEN
                )
                if needs_default:
                    assert found["default"] is not None, where
                if column.name in ALWAYS_WRITTEN:
                    assert found["default"] is None, where

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


def test_a_row_written_with_only_what_it_needs_is_an_empty_look_from_nobody(
    upgraded: sa.Engine,
) -> None:
    with upgraded.begin() as connection:
        catalogue(connection)
        look(connection, "a", hexid("3"), "suggested")
        suggestion(connection, "b")
        clip = connection.execute(sa.text("SELECT * FROM clip_looks")).mappings().one()
        photo = connection.execute(sa.text("SELECT * FROM listing_edit_suggestions")).mappings()
        photo_row = photo.one()

    # "As shot": no sliders, nothing frozen, nothing left out.
    assert (clip["source"], clip["still_ts_ms"], clip["reason"], clip["demo"]) == (
        "model",
        0,
        "",
        0,
    )
    assert [json.loads(clip[name]) for name in ("recipe", "measured", "left_out", "features")] == [
        {},
        {},
        [],
        [],
    ]
    provenance = ("model", "model_revision", "runtime", "runtime_version", "prompt_version")
    assert [clip[name] for name in provenance] == [None] * 5
    # The build's variant is "" rather than NULL, as on the comparisons'
    # columns, so a writer that copies a build never has to tell the two apart.
    assert clip["variant"] == ""
    assert clip["decided_by"] is None
    assert clip["created_at"] is not None and clip["updated_at"] is not None

    # A proposal that changes nothing, waiting for somebody.
    assert (photo_row["rotate"], photo_row["keystone"], photo_row["sky_worth_replacing"]) == (
        0,
        0,
        0,
    )
    assert (photo_row["caption"], photo_row["slug"], photo_row["note"]) == ("", "", "")
    assert (photo_row["state"], photo_row["applied_version"], photo_row["demo"]) == (
        "proposed",
        None,
        0,
    )
    assert [photo_row[name] for name in provenance] == [None] * 5
    assert photo_row["variant"] == ""
    assert (photo_row["decided_at"], photo_row["decided_by"]) == (None, None)
    assert photo_row["created_at"] is not None


def test_the_models_read_and_write_the_migrated_tables(upgraded: sa.Engine) -> None:
    with upgraded.begin() as connection:
        catalogue(connection)
        # Written by hand: the database's defaults must read back as the
        # models' types, not as text.
        look(connection, "a", hexid("3"), "suggested")
    with Session(upgraded) as session:
        by_hand = session.get(models.ClipLook, uuid.UUID(hexid("a")))
        assert by_hand is not None
        assert (by_hand.recipe, by_hand.measured, by_hand.left_out, by_hand.features) == (
            {},
            {},
            [],
            [],
        )
        assert by_hand.demo is False

        chosen = models.ClipLook(
            asset_id=uuid.UUID(hexid("3")),
            kind="chosen",
            source="custom",
            recipe={"exposure": 0.25, "auto_wb": 0.6},
            measured={"wb_gains": [1.062, 1.0, 0.921]},
            left_out=["window_pull"],
            still_ts_ms=7000,
            features=[0.5] * 17,
            decided_by=uuid.UUID(hexid("1")),
        )
        proposal = models.ListingEditSuggestion(
            listing_id=uuid.UUID(hexid("6")),
            item_id=uuid.UUID(hexid("7")),
            asset_id=uuid.UUID(hexid("5")),
            rotate=-1.5,
            keystone=0.2,
            sky_worth_replacing=True,
            caption="Kitchen with island",
            slug="kitchen-island",
            model="mlx-community/gemma-4-e4b-it-4bit",
            model_revision="475b9088",
            variant="Q8_0",
            demo=True,
        )
        session.add_all([chosen, proposal])
        session.commit()
        chosen_id, proposal_id = chosen.id, proposal.id

    with Session(upgraded) as session:
        chosen_back = session.get(models.ClipLook, chosen_id)
        proposal_back = session.get(models.ListingEditSuggestion, proposal_id)
        assert chosen_back is not None and proposal_back is not None
        assert chosen_back.recipe == {"exposure": 0.25, "auto_wb": 0.6}
        assert chosen_back.measured == {"wb_gains": [1.062, 1.0, 0.921]}
        assert (chosen_back.left_out, chosen_back.still_ts_ms) == (["window_pull"], 7000)
        assert chosen_back.created_at is not None and chosen_back.reason == ""
        assert (proposal_back.rotate, proposal_back.keystone) == (-1.5, 0.2)
        assert (proposal_back.state, proposal_back.sky_worth_replacing) == ("proposed", True)
        assert (proposal_back.model_revision, proposal_back.demo) == ("475b9088", True)
        # Which quantisation proposed it survives the round trip.
        assert (proposal_back.variant, chosen_back.variant) == ("Q8_0", "")


def test_a_clip_has_at_most_one_suggested_and_one_chosen_look(upgraded: sa.Engine) -> None:
    with upgraded.begin() as connection:
        catalogue(connection)
        look(connection, "a", hexid("3"), "suggested")
        # The other writer's row sits beside it, never over it.
        look(connection, "b", hexid("3"), "chosen", source="none")
        # Another clip has looks of its own.
        look(connection, "c", hexid("4"), "suggested")
    for kind in ("suggested", "chosen"):
        with pytest.raises(sa.exc.IntegrityError), upgraded.begin() as connection:
            look(connection, "d", hexid("3"), kind)


def test_a_photograph_has_one_proposal_and_asking_again_must_replace_it(
    upgraded: sa.Engine,
) -> None:
    with upgraded.begin() as connection:
        catalogue(connection)
        suggestion(connection, "a")
    with pytest.raises(sa.exc.IntegrityError), upgraded.begin() as connection:
        suggestion(connection, "b")


def test_looks_and_proposals_go_with_what_they_are_about_and_outlive_who_chose(
    upgraded: sa.Engine,
) -> None:
    with keys_on(upgraded) as connection, connection.begin():
        catalogue(connection)
        look(connection, "a", hexid("3"), "chosen", decided_by=hexid("1"))
        look(connection, "b", hexid("4"), "suggested")
        suggestion(connection, "c")
        connection.execute(
            sa.text("UPDATE listing_edit_suggestions SET decided_by = :user"), {"user": hexid("1")}
        )

    with keys_on(upgraded) as connection, connection.begin():
        # The person who chose leaves; what they chose stays.
        connection.execute(sa.text("DELETE FROM users"))
        assert connection.execute(sa.text("SELECT decided_by FROM clip_looks")).scalars().all() == [
            None,
            None,
        ]
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM listing_edit_suggestions")
            ).scalar_one()
            == 1
        )
        # A clip that leaves the catalogue takes its looks.
        connection.execute(sa.text("DELETE FROM assets WHERE id = :id"), {"id": hexid("3")})
        assert connection.execute(sa.text("SELECT asset_id FROM clip_looks")).scalars().all() == [
            hexid("4")
        ]
        # A photograph taken out of the listing takes its proposal.
        connection.execute(sa.text("DELETE FROM listing_items"))
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM listing_edit_suggestions")
            ).scalar_one()
            == 0
        )

        put(connection, "listing_items", id=hexid("7"), listing_id=hexid("6"), asset_id=hexid("5"))
        suggestion(connection, "d")
        # So does a listing that is deleted ...
        connection.execute(sa.text("DELETE FROM listings"))
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM listing_edit_suggestions")
            ).scalar_one()
            == 0
        )

        put(connection, "listings", id=hexid("6"), name="130 Davis Rd")
        put(connection, "listing_items", id=hexid("7"), listing_id=hexid("6"), asset_id=hexid("5"))
        suggestion(connection, "e")
        # ... and a photograph deleted from the catalogue.
        connection.execute(sa.text("DELETE FROM assets WHERE id = :id"), {"id": hexid("5")})
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM listing_edit_suggestions")
            ).scalar_one()
            == 0
        )


def test_downgrade_takes_away_exactly_what_upgrade_made(database: sa.Engine) -> None:
    def everything(connection: sa.Connection) -> dict[str, Any]:
        inspector = sa.inspect(connection)
        return {
            table: (
                [column["name"] for column in inspector.get_columns(table)],
                sorted(index["name"] or "" for index in inspector.get_indexes(table)),
            )
            for table in inspector.get_table_names()
        }

    with database.connect() as connection:
        before = everything(connection)
    with database.begin() as connection:
        run(connection, "upgrade")
        assert set(sa.inspect(connection).get_table_names()) == set(before) | set(TABLES)
        run(connection, "downgrade")
        # No other table was touched on the way up or down.
        assert everything(connection) == before
