"""The AI models page's stored settings, and "can this install run models at all".

File-backed SQLite, as tests/test_jobs.py: the store commits, and a second
session has to see what the first wrote.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound import local_models
from framefound.auth.crypto import SecretUnavailable
from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.models import AppSetting
from framefound.local_models import policy, schedule, settings_store
from framefound.local_models.settings_store import LocalModelsConfig

Factory = async_sessionmaker[AsyncSession]

# What a developer's or a CI machine's own environment might already say.
OURS = (
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
    "FRAMEFOUND_MODEL_SERVER_PORT",
    "FRAMEFOUND_MODEL_RUNTIME_PORT",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in OURS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "local-models-test-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
async def factory(tmp_path: Path) -> AsyncIterator[Factory]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def stored(factory: Factory, key: str) -> dict[str, Any]:
    async with factory() as db:
        row = await db.get(AppSetting, key)
        assert row is not None
        return dict(row.value)


# --- what is stored ----------------------------------------------------------


async def test_nothing_saved_means_the_built_in_lists_and_the_plans_budgets(
    factory: Factory,
) -> None:
    async with factory() as db:
        config = await settings_store.load_config(db)

    assert config.makers == ["google", "allenai", "nvidia", "apple"]
    assert config.converters == ["mlx-community"]
    assert config.blocked_added == []
    assert config.budgets() == schedule.Budgets(8.0, 20.0, time(21, 30), time(5, 30))
    assert config.idle_unload_minutes == 5
    assert config.paused is False
    assert config.hf_token() is None
    lists = config.lists()
    assert lists.status("google") == "maker"
    assert lists.status("mlx-community") == "converter"
    assert lists.status(policy.DEFAULT_BLOCKED[0]) == "blocked"


async def test_what_was_saved_is_what_another_session_reads(factory: Factory) -> None:
    async with factory() as db:
        config = await settings_store.load_config(db)
        config.makers = ["google"]
        config.blocked_added = ["example-lab"]
        config.day_budget_gb = 11.0
        config.night_start = "22:00"
        config.paused = True
        config.paused_at = "2026-11-10T14:02:00"
        config.paused_by = "owner@example.com"
        await settings_store.save_config(db, config)

    async with factory() as db:
        again = await settings_store.load_config(db)

    assert again == config
    assert again.budgets().day_gb == 11.0
    assert again.budgets().night_start == time(22, 0)
    assert again.lists().status("nvidia") == "unknown"
    assert again.lists().status("Example-Lab") == "blocked"


async def test_a_list_changed_in_place_is_still_saved(factory: Factory) -> None:
    async with factory() as db:
        await settings_store.save_config(db, LocalModelsConfig())

    # One session loads, edits the list it was handed, and saves: the way a
    # request handler does it. If the settings shared the row's own list and
    # anything in the request still held the row (`held`), the save would
    # look like no change and the new maker would be silently lost.
    async with factory() as db:
        held = await db.get(AppSetting, settings_store.CONFIG_KEY)
        config = await settings_store.load_config(db)
        config.makers.append("example-maker")
        await settings_store.save_config(db, config)
        assert held is not None

    async with factory() as db:
        assert "example-maker" in (await settings_store.load_config(db)).makers


async def test_keys_this_version_does_not_know_are_dropped(factory: Factory) -> None:
    async with factory() as db:
        db.add(
            AppSetting(
                key=settings_store.CONFIG_KEY,
                value={"night_budget_gb": 24.0, "from_a_newer_version": True},
            )
        )
        await db.commit()

    async with factory() as db:
        config = await settings_store.load_config(db)

    assert config.night_budget_gb == 24.0
    assert not hasattr(config, "from_a_newer_version")
    # And what the row did not hold is the default, not missing.
    assert config.day_budget_gb == 8.0
    assert config.makers == list(policy.DEFAULT_MAKERS)


async def test_the_built_in_blocked_names_are_in_force_whatever_was_saved(
    factory: Factory,
) -> None:
    blocked = policy.DEFAULT_BLOCKED[0]
    async with factory() as db:
        # Only additions are stored, so an emptied list takes nothing away;
        # and a blocked name written straight into the makers still loses.
        await settings_store.save_config(
            db, LocalModelsConfig(makers=["google", blocked], blocked_added=[])
        )

    async with factory() as db:
        lists = (await settings_store.load_config(db)).lists()

    assert (await stored(factory, settings_store.CONFIG_KEY))["blocked_added"] == []
    assert all(lists.status(name) == "blocked" for name in policy.DEFAULT_BLOCKED)
    assert not policy.judge([policy.Node(f"{blocked}/demo-model", blocked)], lists).allowed


def test_a_stored_window_that_cannot_be_read_falls_back_to_the_default() -> None:
    # This runs before every question to a model: a damaged row must not
    # turn into an exception there.
    config = LocalModelsConfig(night_start="half nine", night_end="", day_budget_gb=0)

    assert config.budgets() == schedule.Budgets()


async def test_stored_lists_in_a_shape_this_version_cannot_read_approve_nobody(
    factory: Factory,
) -> None:
    # A later version may store the lists differently (a name and when it
    # was added, say). After a rollback this version reads that row before
    # every check and every question to a model: it must not raise there,
    # and what it cannot read must not approve anyone.
    async with factory() as db:
        db.add(
            AppSetting(
                key=settings_store.CONFIG_KEY,
                value={
                    "makers": [{"name": "google"}, ["nvidia"]],
                    "converters": None,
                    "blocked_added": "example-lab",
                },
            )
        )
        await db.commit()

    async with factory() as db:
        lists = (await settings_store.load_config(db)).lists()

    assert lists.status("google") == lists.status("nvidia") == "unknown"
    assert lists.status("mlx-community") == "unknown"
    assert lists.status(policy.DEFAULT_BLOCKED[0]) == "blocked"
    assert not policy.judge([policy.Node("google/demo-model", "google")], lists).allowed
    assert LocalModelsConfig(makers=None).lists().makers == frozenset()  # type: ignore[arg-type]


# --- the Hugging Face token ---------------------------------------------------


async def test_the_token_is_sealed_in_the_row(factory: Factory) -> None:
    async with factory() as db:
        config = await settings_store.load_config(db)
        config.with_hf_token("not-a-real-token")
        await settings_store.save_config(db, config)

    row = await stored(factory, settings_store.CONFIG_KEY)
    assert "not-a-real-token" not in str(row)
    assert row["hf_token_sealed"]

    async with factory() as db:
        again = await settings_store.load_config(db)
    assert again.hf_token() == "not-a-real-token"

    again.with_hf_token("")
    assert again.hf_token_sealed == ""
    assert again.hf_token() is None


async def test_a_token_sealed_under_another_key_says_so_instead_of_reading_as_none(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with factory() as db:
        config = LocalModelsConfig()
        config.with_hf_token("not-a-real-token")
        await settings_store.save_config(db, config)

    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "a-different-secret")
    get_settings.cache_clear()

    async with factory() as db:
        config = await settings_store.load_config(db)
    # "None" would send a gated download off with no token and report the
    # wrong problem; the page has to ask for the token to be pasted again.
    with pytest.raises(SecretUnavailable):
        config.hf_token()


# --- the newest runtime version found ------------------------------------------


async def test_the_newest_version_found_is_remembered_per_runtime(factory: Factory) -> None:
    looked = datetime(2026, 11, 12, 18, 39, 10, tzinfo=UTC)
    async with factory() as db:
        assert await settings_store.load_latest(db) == {}
        await settings_store.save_latest(db, "mlx-vlm", "0.7.12", None, now=looked)
        await settings_store.save_latest(db, "another-kind", "b1", None, now=looked)

    async with factory() as db:
        latest = await settings_store.load_latest(db)

    assert latest == {
        "mlx-vlm": {"version": "0.7.12", "checked_at": "2026-11-12T18:39:10Z", "problem": None},
        "another-kind": {"version": "b1", "checked_at": "2026-11-12T18:39:10Z", "problem": None},
    }


async def test_a_look_that_failed_keeps_the_version_found_before(factory: Factory) -> None:
    async with factory() as db:
        await settings_store.save_latest(db, "mlx-vlm", "0.7.12", None)
        await settings_store.save_latest(
            db, "mlx-vlm", None, "The package index could not be reached."
        )

    async with factory() as db:
        entry = (await settings_store.load_latest(db))["mlx-vlm"]

    assert entry["version"] == "0.7.12"
    assert entry["problem"] == "The package index could not be reached."


# --- can this install run models -------------------------------------------------


def test_with_nothing_set_models_are_unavailable_and_the_sentence_says_why() -> None:
    found = local_models.availability(Settings())

    assert found.mode == "unavailable"
    assert not found.ok
    assert found.reason == local_models.UNAVAILABLE
    assert "Mac install" in local_models.UNAVAILABLE


def test_a_runtimes_folder_makes_the_install_ready(tmp_path: Path) -> None:
    found = local_models.availability(Settings(runtimes_dir=tmp_path / "runtimes"))

    assert found.mode == "ready"
    assert found.ok and found.reason is None


def test_the_demo_needs_its_flag_and_no_runtimes_folder_and_no_domain(tmp_path: Path) -> None:
    assert local_models.availability(Settings(local_ai_demo=True)).mode == "demo"
    assert local_models.availability(Settings(local_ai_demo=True)).ok
    # A real install is never a demonstration, whatever the flag says.
    real = Settings(local_ai_demo=True, runtimes_dir=tmp_path)
    assert local_models.availability(real).mode == "ready"
    public = Settings(local_ai_demo=True, domain="media.example.com")
    assert local_models.availability(public).mode == "unavailable"


def test_availability_follows_the_environment_when_no_settings_are_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert local_models.availability().mode == "unavailable"

    monkeypatch.setenv("FRAMEFOUND_RUNTIMES_DIR", str(tmp_path))
    get_settings.cache_clear()

    assert local_models.availability().mode == "ready"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_runtimes_dir_line_means_unset(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    # Left to pydantic, `FRAMEFOUND_RUNTIMES_DIR=` becomes Path(""), the
    # working directory: "ready", with runtimes installed wherever the
    # service happened to start.
    monkeypatch.setenv("FRAMEFOUND_RUNTIMES_DIR", blank)

    settings = Settings()

    assert settings.runtimes_dir is None
    assert local_models.availability(settings).mode == "unavailable"


def test_the_two_ports_are_not_the_apps_own() -> None:
    settings = Settings()

    assert settings.model_server_port == 54805
    assert settings.model_runtime_port == 54806
    assert settings.local_ai_demo is False
    # A runtime left on its own default would answer on FrameFound's address.
    assert len({settings.http_port, settings.model_server_port, settings.model_runtime_port}) == 3


def test_the_roles_are_the_two_of_the_plan() -> None:
    assert local_models.ROLES == ("looker", "editor")
    assert local_models.ROLE_NEEDS == {"looker": "image", "editor": "text"}
    assert set(local_models.ROLE_TITLES) == set(local_models.ROLES)
