"""The database side of local models and roles (local_models/registry.py).

File-backed SQLite, as tests/test_jobs.py: several of these rules are about
two sessions, and the second has to see what the first committed. Every
model is a fixture page from `local_models/demo_data`, put through the real
check; nothing reaches a network and no file is downloaded.

No blocked publisher's model is named: a test that needs a blocked publisher
uses `policy.DEFAULT_BLOCKED[0]`.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import AppSetting, LocalAiJob, LocalAiRole, LocalModel, User
from framefound.local_models import fake, policy, preflight, registry, schedule
from framefound.local_models.kinds import LlamaCpp, MlxVlm
from framefound.local_models.preflight import Installed, Preflight
from framefound.local_models.settings_store import LocalModelsConfig

Factory = async_sessionmaker[AsyncSession]

GB = 1024**3
FIRST = "mlx-community/gemma-4-e4b-it-4bit"
BUILD = "475b9088d29754a3379866cf5aeb6b41acd313c2"
TWELVE = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
LARGE = "mlx-community/demo-large-4bit"
BLOCKED = policy.DEFAULT_BLOCKED[0]
DEFAULTS = LocalModelsConfig()
KINDS = ("mlx-vlm",)

ALREADY = "This exact build is already installed."
BAD_RECORD = (
    "FrameFound's record of what this model was built from cannot be read, so it cannot be "
    "checked. Remove the model and add it again."
)
BAD_FILES = (
    "FrameFound's record of this model's files cannot be read. Remove the model and add it again."
)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


def checked(name: str = FIRST, *, site: fake.FakeHub | None = None, **more: Any) -> Preflight:
    """The check as the Mac would run it: MLX-VLM installed, plenty of room, nothing here yet."""
    return preflight.preflight(
        site or fake.FakeHub.from_fixtures(),
        name,
        config=DEFAULTS,
        kinds={"mlx-vlm": MlxVlm()},
        installed={"mlx-vlm": "0.7.12"},
        free_bytes=812 * GB,
        min_free_bytes=5 * GB,
        **more,
    )


async def added(factory: Factory, name: str = FIRST, *, state: str = "installed") -> LocalModel:
    """A model with a row, as if its download had finished."""
    async with factory() as db:
        model = await registry.add_model(db, checked(name), None)
        model.state = state
        await db.commit()
        return model


async def role_rows(factory: Factory) -> dict[str, tuple[uuid.UUID | None, uuid.UUID | None]]:
    """What the database holds: role -> (who has it, who is waiting for it)."""
    async with factory() as db:
        rows = (await db.execute(select(LocalAiRole))).scalars().all()
        return {row.role: (row.model_id, row.pending_model_id) for row in rows}


async def count(factory: Factory, table: Any) -> int:
    async with factory() as db:
        return int((await db.execute(select(func.count()).select_from(table))).scalar_one())


# --- adding a model ----------------------------------------------------------------


async def test_what_is_stored_is_what_the_check_read(factory: Factory) -> None:
    found = checked()
    assert found.ok and found.variant is not None
    async with factory() as db:
        user = User(email="owner@example.com", password_hash="x", role="admin")
        db.add(user)
        await db.flush()
        model = await registry.add_model(db, found, user.id)
        await db.commit()
        model_id = model.id

    async with factory() as db:
        stored = await registry.get_model(db, model_id)
        assert stored is not None
        assert (stored.repo_id, stored.repo_key) == (FIRST, FIRST.casefold())
        assert (stored.revision, stored.variant) == (BUILD, "")
        assert stored.publisher == "mlx-community"
        assert (stored.licence, stored.gated, stored.runtime) == ("gemma", "", "mlx-vlm")
        assert stored.takes == ["text", "image"]
        assert stored.size_bytes == found.size_bytes > 4 * GB
        assert stored.memory_gb == 5.8
        assert stored.measured_memory_gb is None
        # Nothing is on disk yet: the download has not run.
        assert (stored.state, stored.error) == ("downloading", None)
        assert stored.added_by == user.id
        assert stored.hub_modified_at is not None
        assert stored.created_at is not None

        # The files are the chosen build's, to the byte and the checksum: this
        # list is what will be fetched and what every file is checked against.
        assert registry.files_of(stored) == found.variant.files
        assert not any(file.path.endswith(".py") for file in registry.files_of(stored))
        # The chain is kept whole, so approval can be judged again from it.
        assert registry.nodes(stored) == found.chain
        assert [node.repo_id for node in registry.nodes(stored)] == [
            FIRST,
            "google/gemma-4-E4B-it",
            "google/gemma-4-E4B",
        ]
        assert registry.judged(stored, DEFAULTS.lists()).allowed


async def test_the_same_build_cannot_be_added_twice_whatever_its_case(factory: Factory) -> None:
    found = checked()
    async with factory() as db:
        await registry.add_model(db, found, None)
        await db.commit()

    # Hugging Face tells capitals apart; the Mac's disk does not. The second
    # name would be the first one's folder.
    shouted = replace(found, info=replace(found.info, repo_id=FIRST.upper()))
    assert not shouted.problems, "the registry has to refuse this by itself"
    for again in (found, shouted):
        async with factory() as db:
            db.add(AppSetting(key=f"beside-{again.info.repo_id}", value={"kept": True}))
            await db.flush()
            with pytest.raises(registry.Refusal) as refused:
                await registry.add_model(db, again, None)
            assert str(refused.value) == ALREADY
            # The refusal did not take the caller's transaction with it.
            await db.commit()

    assert await count(factory, LocalModel) == 1
    assert await count(factory, AppSetting) == 2
    async with factory() as db:
        kept = await registry.find_build(db, FIRST.upper(), BUILD.upper())
        assert kept is not None and kept.repo_id == FIRST


async def test_two_requests_adding_one_build_together_leave_one_row(factory: Factory) -> None:
    found = checked()
    async with factory() as first, factory() as second:
        # Both looked before either wrote: neither sees a build that is there.
        assert await registry.find_build(first, FIRST, BUILD) is None
        assert await registry.find_build(second, FIRST, BUILD) is None
        await registry.add_model(first, found, None)
        await first.commit()
        with pytest.raises(registry.Refusal, match=ALREADY):
            await registry.add_model(second, found, None)
        await second.commit()
    assert await count(factory, LocalModel) == 1


async def test_a_newer_build_of_the_same_model_is_a_second_row(factory: Factory) -> None:
    site = fake.FakeHub.from_fixtures()
    old = await added(factory)
    site.payloads[FIRST]["sha"] = "c" * 40
    async with factory() as db:
        newer = await registry.add_model(db, checked(site=site), None)
        # Rows made within the same second: say which came first.
        newer.created_at = datetime.now(UTC) + timedelta(minutes=5)
        await db.commit()

    async with factory() as db:
        builds = await registry.builds_of(db, FIRST.upper())
        assert builds == (Installed(newer.id, "c" * 40, ""), Installed(old.id, BUILD, ""))
        assert await registry.builds_of(db, TWELVE) == ()
        found_old = await registry.find_build(db, FIRST, BUILD)
        found_new = await registry.find_build(db, FIRST, "c" * 40)
        assert found_old is not None and found_old.id == old.id
        assert found_new is not None and found_new.id == newer.id
        assert await registry.find_build(db, FIRST, BUILD, "Q4_K_M") is None
        assert await registry.find_build(db, FIRST, "d" * 40) is None

        # The check is told about every build, so it calls the one it is
        # looking at "already installed" and no other.
        again = checked(existing=lambda _name: builds)
        assert again.same_build and again.existing == old.id
        with pytest.raises(registry.Refusal, match=ALREADY):
            await registry.add_model(db, again, None)


async def test_two_builds_of_one_gguf_repository_are_two_rows_with_their_own_files(
    factory: Factory,
) -> None:
    vision = "google/demo-vision-gguf"
    site = fake.FakeHub.from_fixtures()

    def gguf(variant: str | None, existing: Any = preflight._none_installed) -> Preflight:
        return preflight.preflight(
            site,
            vision,
            config=DEFAULTS,
            kinds={"mlx-vlm": MlxVlm(), "llama-cpp": LlamaCpp()},
            installed={"llama-cpp": "0.9.0"},
            free_bytes=812 * GB,
            min_free_bytes=5 * GB,
            existing=existing,
            variant=variant,
        )

    async with factory() as db:
        suggested = await registry.add_model(db, gguf(None), None)
        await db.commit()
    async with factory() as db:
        builds = await registry.builds_of(db, vision)
        larger = await registry.add_model(db, gguf("Q8_0", lambda _name: builds), None)
        await db.commit()

    async with factory() as db:
        rows = {row.variant: row for row in (await db.scalars(select(LocalModel))).all()}
        assert set(rows) == {"Q4_K_M", "Q8_0"}
        assert {rows["Q4_K_M"].id, rows["Q8_0"].id} == {suggested.id, larger.id}
        # Each row holds its own build's files and nothing of the other's.
        assert [file["path"] for file in rows["Q4_K_M"].files] == [
            "demo-vision-Q4_K_M.gguf",
            "mmproj-demo-vision-F16.gguf",
            "README.md",
            "LICENSE",
        ]
        assert rows["Q8_0"].files[0]["path"] == "demo-vision-Q8_0.gguf"
        assert all(row.runtime == "llama-cpp" for row in rows.values())
        # What the build takes is the build's: it has a picture reader.
        assert rows["Q4_K_M"].takes == ["text", "image"]
        assert rows["Q4_K_M"].size_bytes < rows["Q8_0"].size_bytes

        # The same build again is refused; it is the build, not the repository.
        builds = await registry.builds_of(db, vision)
        again = gguf("Q8_0", lambda _name: builds)
        assert again.same_build and again.existing == larger.id
        with pytest.raises(registry.Refusal, match=ALREADY):
            await registry.add_model(db, again, None)


@pytest.mark.parametrize(
    "name",
    ["example-lab/some-model", "allenai/demo-needs-code", "mlx-community/orphan-4bit"],
)
async def test_a_model_the_check_refused_is_never_stored(factory: Factory, name: str) -> None:
    found = checked(name)
    assert found.problems
    async with factory() as db:
        with pytest.raises(registry.Refusal) as refused:
            await registry.add_model(db, found, None)
        assert str(refused.value) == found.problems[0]
        await db.commit()
    assert await count(factory, LocalModel) == 0


async def test_a_check_with_nothing_to_download_adds_nothing(factory: Factory) -> None:
    found = checked()
    assert found.variant is not None
    for nothing in (
        replace(found, variant=None),
        replace(found, runtime=None),
        replace(found, variant=replace(found.variant, files=())),
    ):
        async with factory() as db:
            with pytest.raises(registry.Refusal, match="found nothing in this model"):
                await registry.add_model(db, nothing, None)
    assert await count(factory, LocalModel) == 0


async def test_a_name_too_long_to_store_is_refused_not_cut(factory: Factory) -> None:
    found = checked()
    assert found.variant is not None
    long_name = "mlx-community/" + "a" * 187
    for too_long in (
        replace(found, info=replace(found.info, repo_id=long_name)),
        replace(found, variant=replace(found.variant, id="q" * 101)),
    ):
        async with factory() as db:
            db.add(AppSetting(key="beside", value={"kept": True}))
            await db.flush()
            with pytest.raises(registry.Refusal, match="longer than FrameFound can store"):
                await registry.add_model(db, too_long, None)
            # A cut name would be another model's key; a refused insert on
            # PostgreSQL would have ended the caller's transaction.
            await db.rollback()
    assert await count(factory, LocalModel) == 0

    # The longest that fits is stored whole.
    fits = replace(found, info=replace(found.info, repo_id=long_name[:200]))
    async with factory() as db:
        model = await registry.add_model(db, fits, None)
        await db.commit()
    assert model.repo_id == long_name[:200] and model.repo_key == long_name[:200].casefold()


async def test_text_hugging_face_sets_no_length_on_cannot_fail_the_add(factory: Factory) -> None:
    found = checked()
    long_link = "https://example.com/" + "a" * 600
    wordy = replace(
        found,
        info=replace(
            found.info,
            licence="l" * 400,
            licence_name="n" * 400,
            licence_url=long_link,
            gated="by-application-only",
            publisher="p" * 400,
        ),
    )
    async with factory() as db:
        model = await registry.add_model(db, wordy, None)
        await db.commit()
    assert (len(model.licence), len(model.licence_name), len(model.gated)) == (100, 200, 10)
    # Half a link leads somewhere else: it is kept whole or not at all.
    assert model.licence_url is None
    # Shown only; approval is judged from the chain, which is kept as read
    # (its publishers come from the names, not from what the page reported).
    assert model.publisher == "mlx-community"
    assert registry.nodes(model) == found.chain


# --- approval, judged again --------------------------------------------------------


async def test_a_publisher_blocked_after_a_model_was_added_stops_it(factory: Factory) -> None:
    model = await added(factory)
    assert registry.judged(model, DEFAULTS.lists()).allowed

    # The maker the build was made from is blocked today.
    maker_blocked = replace_lists(blocked_added=["google"], makers=["allenai", "nvidia", "apple"])
    verdict = registry.judged(model, maker_blocked)
    assert not verdict.allowed and verdict.offender == "google"
    assert "blocked" in verdict.reason

    # The packager is taken off the converters list.
    assert not registry.judged(model, replace_lists(converters=[])).allowed

    # Nothing was deleted: put the lists back and it is approved again.
    assert registry.judged(model, DEFAULTS.lists()).allowed
    assert await count(factory, LocalModel) == 1


def replace_lists(**changed: list[str]) -> policy.PublisherLists:
    return replace(DEFAULTS, **changed).lists()


@pytest.mark.parametrize(
    "chain",
    [
        None,
        "mlx-community/gemma-4-e4b-it-4bit",
        [],
        {"repo_id": FIRST},
        [FIRST, "google/gemma-4-E4B-it"],
        [{"repo_id": FIRST, "publisher": "mlx-community", "bases": "google/x", "found": True}],
        [{"repo_id": FIRST, "publisher": 7, "bases": [], "found": True}],
        [{"repo_id": FIRST, "publisher": "google", "bases": [], "found": "yes"}],
        [{"repo_id": "", "publisher": "google", "bases": [], "found": True}],
        [{"publisher": "google", "bases": [], "found": True}],
        # A maker's own model, whole and readable, but not this model.
        [{"repo_id": "google/gemma-4-E4B", "publisher": "google", "bases": [], "found": True}],
    ],
)
async def test_a_stored_chain_that_cannot_be_read_approves_nothing(
    factory: Factory, chain: Any
) -> None:
    model = await added(factory)
    model.chain = chain
    verdict = registry.judged(model, DEFAULTS.lists())
    assert (verdict.allowed, verdict.reason) == (False, BAD_RECORD)


async def test_a_chain_with_a_link_missing_is_not_approved(factory: Factory) -> None:
    model = await added(factory)
    good = list(model.chain)

    # One unreadable entry among good ones: the rest is not judged without it.
    model.chain = [good[0], {"repo_id": 5}, good[2]]
    assert registry.judged(model, DEFAULTS.lists()).reason == BAD_RECORD
    assert [node.repo_id for node in registry.nodes(model)] == [FIRST, "google/gemma-4-E4B"]

    # The model it was built from is named but its own entry is gone: the
    # rule itself refuses a chain it cannot follow to the end.
    model.chain = [good[0], good[1]]
    assert not registry.judged(model, DEFAULTS.lists()).allowed

    # A maker's name on the row does not approve it: only the chain is judged.
    model.chain = [{**good[0], "repo_id": f"{BLOCKED}/demo-model", "publisher": BLOCKED}]
    model.publisher = "google"
    model.repo_key = f"{BLOCKED}/demo-model".casefold()
    assert not registry.judged(model, DEFAULTS.lists()).allowed


# --- the stored file list ----------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        None,
        [],
        "model.safetensors",
        [["model.safetensors", 5]],
        [{"path": "../escape.safetensors", "size": 5, "sha256": None, "blob_id": None}],
        [{"path": "/etc/passwd", "size": 5, "sha256": None, "blob_id": None}],
        [{"path": "run.py:stream", "size": 5, "sha256": None, "blob_id": None}],
        [{"path": "", "size": 5, "sha256": None, "blob_id": None}],
        [{"path": "config.json", "size": -1, "sha256": None, "blob_id": None}],
        [{"path": "config.json", "size": "5", "sha256": None, "blob_id": None}],
        [{"path": "config.json", "size": True, "sha256": None, "blob_id": None}],
        [{"path": "model.safetensors", "size": 5, "sha256": "abc", "blob_id": None}],
        [{"path": "model.safetensors", "size": 5, "sha256": "Z" * 64, "blob_id": None}],
        [{"path": "config.json", "size": 5, "sha256": None, "blob_id": "1234"}],
        [
            {"path": "config.json", "size": 5, "sha256": None, "blob_id": None},
            {"path": "Config.JSON", "size": 5, "sha256": None, "blob_id": None},
        ],
        [
            {"path": "config.json", "size": 5, "sha256": None, "blob_id": None},
            {"size": 5},
        ],
    ],
)
async def test_a_stored_file_list_that_cannot_be_read_is_refused_whole(
    factory: Factory, stored: Any
) -> None:
    model = await added(factory)
    assert len(registry.files_of(model)) > 3
    model.files = stored
    # Never a shorter list: a file left out is one nobody fetches or checks.
    with pytest.raises(ValueError) as refused:
        registry.files_of(model)
    assert str(refused.value) == BAD_FILES


# --- memory, and what last loaded it -----------------------------------------------


async def test_the_measured_memory_only_goes_up(factory: Factory) -> None:
    model = await added(factory)
    assert registry.memory_gb(model) == 5.8, "the estimate, until something is measured"

    async with factory() as db:
        held = await registry.get_model(db, model.id)
        assert held is not None
        await registry.note_memory(db, model.id, 4.9)
        # This session's own copy of the row is brought up to date.
        assert held.measured_memory_gb == 4.9
        # Less than the estimate was seen on a short test: the estimate stands.
        assert registry.memory_gb(held) == 5.8

        await registry.note_memory(db, model.id, 6.4)
        await registry.note_memory(db, model.id, 6.1)
        assert held.measured_memory_gb == 6.4, "the highest figure ever seen, not the last"
        assert registry.memory_gb(held) == 6.4
        await db.commit()

    async with factory() as db:
        for nonsense in (0.0, -3.0, float("nan"), float("inf"), 6.4e9):
            await registry.note_memory(db, model.id, nonsense)
        await registry.note_memory(db, uuid.uuid4(), 99.0)
        await db.commit()
        again = await registry.get_model(db, model.id)
        # A figure in bytes taken for gigabytes would otherwise keep the
        # model from ever running again, with nothing on the page to undo it.
        assert again is not None and again.measured_memory_gb == 6.4


async def test_the_program_a_model_last_loaded_under_is_remembered(factory: Factory) -> None:
    model = await added(factory)
    other = await added(factory, TWELVE)
    async with factory() as db:
        held = await registry.get_model(db, model.id)
        assert held is not None and held.last_loaded_with is None
        await registry.note_loaded(db, model.id, "mlx-vlm", "0.7.11")
        await registry.note_loaded(db, model.id, "mlx-vlm", "0.7.12")
        assert held.last_loaded_with == "mlx-vlm/0.7.12"
        await registry.note_loaded(db, model.id, "mlx-vlm", "9" * 200)
        assert held.last_loaded_with is not None and len(held.last_loaded_with) == 60
        await db.commit()
    async with factory() as db:
        untouched = await registry.get_model(db, other.id)
        assert untouched is not None and untouched.last_loaded_with is None


async def test_a_row_is_read_as_the_database_has_it_now(factory: Factory) -> None:
    model = await added(factory, state="downloading")
    async with factory() as reader, factory() as writer:
        seen = await registry.get_model(reader, model.id)
        assert seen is not None and seen.state == "downloading"

        finished = await registry.get_model(writer, model.id)
        assert finished is not None
        finished.state = "installed"
        await writer.commit()

        # A long-lived session (the lane's) must not go on believing what it
        # read before the download finished.
        again = await registry.get_model(reader, model.id)
        assert again is seen and again.state == "installed"
    async with factory() as db:
        assert await registry.get_model(db, uuid.uuid4()) is None


# --- roles ------------------------------------------------------------------------


async def test_a_role_nobody_was_given_reads_as_empty_and_writes_nothing(
    factory: Factory,
) -> None:
    async with factory() as db:
        roles = await registry.roles(db)
        assert list(roles) == ["looker", "editor"]
        for role, row in roles.items():
            assert row.role == role
            assert (row.model_id, row.pending_model_id, row.assigned_at) == (None, None, None)
        assert await registry.holders(db) == {}
        await db.commit()
    # Looking is not a write: the page polls this.
    assert await role_rows(factory) == {}


async def test_a_model_waiting_for_a_role_does_not_take_it_from_the_holder(
    factory: Factory,
) -> None:
    first = await added(factory)
    second = await added(factory, LARGE)
    async with factory() as db:
        owner = User(email="owner@example.com", password_hash="x", role="admin")
        db.add(owner)
        await db.flush()
        await registry.set_role(db, "looker", first, pending=False, user_id=owner.id)
        given = (await registry.roles(db))["looker"]
        assert (given.model_id, given.pending_model_id) == (first.id, None)
        assert given.assigned_by == owner.id and given.assigned_at is not None
        when = given.assigned_at

        # The second model's files still have to be copied to this machine.
        await registry.set_role(db, "looker", second, pending=True, user_id=owner.id)
        await db.commit()

    assert await role_rows(factory) == {"looker": (first.id, second.id)}
    async with factory() as db:
        assert await registry.holders(db) == {first.id: ["looker"], second.id: ["looker"]}
        waiting = (await registry.roles(db))["looker"]
        # Still the first model's role, given when it was given.
        assert same_moment(waiting.assigned_at, when)

        # The copy is whole.
        done = await registry.complete_role(db, "looker", second.id, user_id=owner.id)
        assert done == registry.Handover(True, first.id)
        await db.commit()

    assert await role_rows(factory) == {"looker": (second.id, None)}
    async with factory() as db:
        now = (await registry.roles(db))["looker"]
        assert now.assigned_by == owner.id
        assert await registry.holders(db) == {second.id: ["looker"]}


def same_moment(left: datetime | None, right: datetime | None) -> bool:
    """SQLite hands a stored time back without its zone."""
    assert left is not None and right is not None
    return left.replace(tzinfo=None) == right.replace(tzinfo=None)


async def test_the_first_model_given_a_role_has_nobody_to_take_it_from(factory: Factory) -> None:
    model = await added(factory)
    async with factory() as db:
        await registry.set_role(db, "editor", model, pending=True, user_id=None)
        assert (await registry.roles(db))["editor"].model_id is None
        done = await registry.complete_role(db, "editor", model.id)
        # It changed, and nobody had it before: two separate answers.
        assert done == registry.Handover(True, None)
        await db.commit()
    assert await role_rows(factory) == {"editor": (model.id, None)}

    # Its copy on this machine went missing and is made again.
    async with factory() as db:
        await registry.set_role(db, "editor", model, pending=True, user_id=None)
        assert await registry.holders(db) == {model.id: ["editor"]}
        again = await registry.complete_role(db, "editor", model.id)
        assert again == registry.Handover(True, model.id)
        await db.commit()
    assert await role_rows(factory) == {"editor": (model.id, None)}


async def test_a_role_only_changes_hands_for_the_model_it_was_waiting_for(
    factory: Factory,
) -> None:
    holder = await added(factory)
    slow = await added(factory, LARGE)
    other = await added(factory, TWELVE)
    async with factory() as db:
        await registry.set_role(db, "editor", holder, pending=False, user_id=None)
        await registry.set_role(db, "editor", slow, pending=True, user_id=None)
        await db.commit()

    async with factory() as lane, factory() as page:
        # The lane read the role when it started copying the slow model.
        before = (await registry.roles(lane))["editor"]
        assert before.pending_model_id == slow.id

        # Meanwhile the operator changed their mind.
        await registry.set_role(page, "editor", other, pending=True, user_id=None)
        await page.commit()

        done = await registry.complete_role(lane, "editor", slow.id)
        assert done == registry.Handover(False, None)
        # The lane's own copy of the row now says what is true.
        assert (before.model_id, before.pending_model_id) == (holder.id, other.id)
        await lane.commit()
    assert await role_rows(factory) == {"editor": (holder.id, other.id)}

    # The role was taken away altogether while a copy ran.
    async with factory() as db:
        await registry.set_role(db, "editor", None, pending=False, user_id=None)
        await db.commit()
        assert await registry.complete_role(db, "editor", other.id) == registry.Handover(False)
        await db.commit()
    assert await role_rows(factory) == {"editor": (None, None)}

    # A role that was never given to anyone has no row to change.
    async with factory() as db:
        assert await registry.complete_role(db, "looker", slow.id) == registry.Handover(False)
        await db.commit()
    assert "looker" not in await role_rows(factory)


async def test_a_cancelled_copy_stops_only_its_own_model_waiting(factory: Factory) -> None:
    holder = await added(factory)
    slow = await added(factory, LARGE)
    other = await added(factory, TWELVE)
    async with factory() as db:
        await registry.set_role(db, "editor", holder, pending=False, user_id=None)
        await registry.set_role(db, "editor", slow, pending=True, user_id=None)
        # The operator picked another model before the cancel arrived.
        await registry.set_role(db, "editor", other, pending=True, user_id=None)
        assert await registry.drop_pending(db, "editor", slow.id) is False
        await db.commit()
    assert await role_rows(factory) == {"editor": (holder.id, other.id)}

    async with factory() as db:
        assert await registry.drop_pending(db, "editor", other.id) is True
        assert await registry.drop_pending(db, "editor", other.id) is False
        await db.commit()
    # Whoever had the role keeps it.
    assert await role_rows(factory) == {"editor": (holder.id, None)}


async def test_two_roles_written_at_once_do_not_overwrite_each_other(factory: Factory) -> None:
    looker = await added(factory)
    editor = await added(factory, TWELVE)
    slow = await added(factory, LARGE)
    async with factory() as db:
        await registry.set_role(db, "looker", looker, pending=False, user_id=None)
        await registry.set_role(db, "looker", slow, pending=True, user_id=None)
        await registry.set_role(db, "editor", editor, pending=False, user_id=None)
        await db.commit()

    # The page clears the Editor while the lane completes the Looker. Each
    # read both rows first, so each holds a copy of the row the other writes.
    async with factory() as page, factory() as lane:
        page_saw = await registry.roles(page)
        lane_saw = await registry.roles(lane)
        assert page_saw["looker"].pending_model_id == slow.id
        assert lane_saw["editor"].model_id == editor.id

        await registry.set_role(page, "editor", None, pending=False, user_id=None)
        await page.commit()
        done = await registry.complete_role(lane, "looker", slow.id)
        assert done == registry.Handover(True, looker.id)
        await lane.commit()
    assert await role_rows(factory) == {"looker": (slow.id, None), "editor": (None, None)}

    # And within one role: the page lines a model up for the Looker while
    # another session, which read the row earlier, gives the Editor away.
    async with factory() as page, factory() as second:
        await registry.roles(page)
        await registry.roles(second)
        await registry.set_role(second, "looker", looker, pending=True, user_id=None)
        await second.commit()
        await registry.set_role(page, "editor", editor, pending=False, user_id=None)
        await page.commit()
    assert await role_rows(factory) == {
        "looker": (slow.id, looker.id),
        "editor": (editor.id, None),
    }


async def test_one_model_can_hold_both_roles(factory: Factory) -> None:
    model = await added(factory)
    async with factory() as db:
        await registry.set_role(db, "looker", model, pending=False, user_id=None)
        await registry.set_role(db, "editor", model, pending=True, user_id=None)
        assert await registry.holders(db) == {model.id: ["looker", "editor"]}
        await db.commit()


async def test_a_role_that_does_not_exist_is_a_mistake_not_a_row(factory: Factory) -> None:
    model = await added(factory)
    async with factory() as db:
        for call in (
            registry.set_role(db, "director", model, pending=False, user_id=None),
            registry.complete_role(db, "director", model.id),
            registry.drop_pending(db, "director", model.id),
        ):
            with pytest.raises(ValueError, match="no role called 'director'"):
                await call
        with pytest.raises(ValueError, match="no role called"):
            registry.role_problem(model, "director", DEFAULTS.lists(), DEFAULTS.budgets(), KINDS)
        await db.commit()
    assert await role_rows(factory) == {}


# --- may this model be given the role? ---------------------------------------------


async def test_a_model_that_does_not_read_pictures_cannot_be_the_looker(
    factory: Factory,
) -> None:
    reads_text = await added(factory, TWELVE)
    reads_pictures = await added(factory)
    lists, budgets = DEFAULTS.lists(), DEFAULTS.budgets()

    assert reads_text.takes == ["text"]
    assert (
        registry.role_problem(reads_text, "looker", lists, budgets, KINDS)
        == "It does not read pictures."
    )
    # The Editor is given a shot list as text, which it does take.
    assert registry.role_problem(reads_text, "editor", lists, budgets, KINDS) is None
    assert registry.role_problem(reads_pictures, "looker", lists, budgets, KINDS) is None
    assert registry.role_problem(reads_pictures, "editor", lists, budgets, KINDS) is None

    # A stored list that cannot be read is not "takes everything".
    reads_pictures.takes = "image"  # type: ignore[assignment]
    assert (
        registry.role_problem(reads_pictures, "looker", lists, budgets, KINDS)
        == "It does not read pictures."
    )


async def test_a_model_that_is_not_whole_cannot_be_given_a_role(factory: Factory) -> None:
    lists, budgets = DEFAULTS.lists(), DEFAULTS.budgets()
    arriving = await added(factory, state="downloading")
    assert (
        registry.role_problem(arriving, "looker", lists, budgets, KINDS)
        == "It is still downloading."
    )
    broken = await added(factory, TWELVE, state="incomplete")
    assert (
        registry.role_problem(broken, "editor", lists, budgets, KINDS)
        == "Its download did not finish. Resume the download first."
    )
    # A state this version does not know is not "installed".
    broken.state = "quarantined"
    assert registry.role_problem(broken, "editor", lists, budgets, KINDS) is not None


async def test_a_model_no_longer_approved_cannot_be_given_a_role(factory: Factory) -> None:
    model = await added(factory)
    lists = replace_lists(converters=[])
    problem = registry.role_problem(model, "looker", lists, DEFAULTS.budgets(), KINDS)
    assert problem == f"It is no longer approved: {registry.judged(model, lists).reason}"
    assert "mlx-community" in problem


async def test_a_model_too_large_for_every_budget_cannot_be_given_a_role(
    factory: Factory,
) -> None:
    large = await added(factory, LARGE)
    lists = DEFAULTS.lists()
    assert large.memory_gb == 18.8
    # Overnight only at the defaults: allowed, and it will wait for the window.
    assert registry.role_problem(large, "looker", lists, DEFAULTS.budgets(), KINDS) is None

    tight = schedule.Budgets(8.0, 16.0, time(21, 30), time(5, 30))
    assert registry.role_problem(large, "looker", lists, tight, KINDS) == (
        "It needs about 18.8 GB, more than the overnight budget (16 GB). "
        "Raise the budget under Sharing this Mac."
    )
    # It fits the overnight budget, and there is no overnight to run in.
    no_window = schedule.Budgets(8.0, 20.0, time(21, 30), time(21, 30))
    problem = registry.role_problem(large, "looker", lists, no_window, KINDS)
    assert problem is not None and "there is no overnight window" in problem

    # What decides is the most it has been seen to use, not the estimate.
    small = await added(factory)
    assert registry.role_problem(small, "looker", lists, tight, KINDS) is None
    small.measured_memory_gb = 17.2
    assert registry.role_problem(small, "looker", lists, tight, KINDS) == (
        "It needs about 17.2 GB, more than the overnight budget (16 GB). "
        "Raise the budget under Sharing this Mac."
    )


async def test_a_model_for_a_program_this_install_lacks_cannot_be_given_a_role(
    factory: Factory,
) -> None:
    model = await added(factory)
    lists, budgets = DEFAULTS.lists(), DEFAULTS.budgets()
    for known in ((), ("demo",), ["llama-cpp"]):
        assert (
            registry.role_problem(model, "looker", lists, budgets, known)
            == "It needs a program this install does not have."
        )
    assert registry.role_problem(model, "looker", lists, budgets, {"mlx-vlm": object()}) is None


# --- removing a model --------------------------------------------------------------


async def test_a_model_with_a_role_or_waiting_for_one_is_not_removed(factory: Factory) -> None:
    looker = await added(factory)
    waiting = await added(factory, LARGE)
    both = await added(factory, TWELVE)
    async with factory() as db:
        await registry.set_role(db, "looker", looker, pending=False, user_id=None)
        await registry.set_role(db, "looker", waiting, pending=True, user_id=None)
        await db.commit()

    async with factory() as db:
        with pytest.raises(registry.Refusal) as refused:
            await registry.delete_model(db, looker.id)
        assert str(refused.value) == (
            "It is the Looker. Give that role to another model, or take it away, first."
        )
        assert await registry.remove_problem(db, looker.id) == str(refused.value)

        with pytest.raises(registry.Refusal) as refused:
            await registry.delete_model(db, waiting.id)
        assert str(refused.value) == (
            "It is being made the Looker: its files are being copied to this machine. "
            "Cancel that first."
        )
        await db.commit()
    assert await count(factory, LocalModel) == 3

    async with factory() as db:
        await registry.set_role(db, "looker", both, pending=False, user_id=None)
        await registry.set_role(db, "editor", both, pending=False, user_id=None)
        assert await registry.remove_problem(db, both.id) == (
            "It is the Looker and the Editor. Give those roles to other models, or take them "
            "away, first."
        )
        # They no longer have or await a role, so nothing is in their way.
        assert await registry.remove_problem(db, looker.id) is None
        assert await registry.remove_problem(db, waiting.id) is None
        await db.rollback()


async def test_removing_a_model_keeps_its_jobs_as_history(factory: Factory) -> None:
    model = await added(factory)
    other = await added(factory, TWELVE)
    async with factory() as db:
        db.add(LocalAiJob(kind="download", subject=f"{FIRST}@{BUILD[:8]}", model_id=model.id))
        db.add(LocalAiJob(kind="test", subject="another model", model_id=other.id))
        await db.commit()

    async with factory() as db:
        held = await registry.get_model(db, model.id)
        assert held is not None
        await registry.delete_model(db, model.id)
        # This session no longer hands out the row it has just deleted.
        assert await registry.get_model(db, model.id) is None
        await db.commit()
        # Removing it twice (two tabs) is not an error.
        await registry.delete_model(db, model.id)
        await registry.delete_model(db, uuid.uuid4())
        await db.commit()

    async with factory() as db:
        assert await registry.get_model(db, model.id) is None
        assert await registry.get_model(db, other.id) is not None
        jobs = (await db.execute(select(LocalAiJob))).scalars().all()
        # SQLite does not act on the foreign key; the registry does it itself.
        assert {job.kind: job.model_id for job in jobs} == {"download": None, "test": other.id}
        assert next(job for job in jobs if job.kind == "download").subject.startswith(FIRST)
        # The same build can be added again afterwards.
        await registry.add_model(db, checked(), None)
        await db.commit()


async def test_a_role_given_in_the_same_moment_still_stops_the_removal(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = await added(factory)
    async with factory() as db:
        await registry.set_role(db, "editor", model, pending=True, user_id=None)
        await db.commit()

    # The look before the delete saw no role (it was given a moment later):
    # the statement itself has to refuse.
    real = registry.remove_problem
    looks: list[uuid.UUID] = []

    async def looked_too_early(db: AsyncSession, model_id: uuid.UUID) -> str | None:
        looks.append(model_id)
        return None if len(looks) == 1 else await real(db, model_id)

    monkeypatch.setattr(registry, "remove_problem", looked_too_early)
    async with factory() as db:
        with pytest.raises(registry.Refusal, match="It is being made the Editor"):
            await registry.delete_model(db, model.id)
        await db.commit()
    assert len(looks) == 2
    assert await count(factory, LocalModel) == 1
    assert await role_rows(factory) == {"editor": (None, model.id)}
