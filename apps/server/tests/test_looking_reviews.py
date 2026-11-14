"""The review service: who may start what, what the dialog is told, how a review reads.

No job is run here: `look_folder` and `look_prepare` are registered as
stand-ins that would fail the test if the lane ever ran them. What is
checked is the rows a start writes, each refusal in its exact words, and the
sentence a review shows for every state its jobs can be in.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import (
    AppSetting,
    Asset,
    Derivative,
    Library,
    LocalAiJob,
    LocalAiRole,
    LocalModel,
    Look,
    LookRequest,
    ShotWindow,
    User,
)
from framefound.local_models import UNAVAILABLE, jobs, registry, schedule
from framefound.local_models.ask import NO_SUCH_MODEL
from framefound.looking import PRIORITY_REVIEW, reviews, store
from framefound.looking.prompt import PROMPT_VERSION

Factory = async_sessionmaker[AsyncSession]

BASE = datetime(2026, 6, 1, tzinfo=UTC)
# The Mac's wall clock, naive, as L1's Deps.now gives it.
DAY = datetime(2026, 11, 12, 14, 0)
NIGHT = datetime(2026, 11, 12, 23, 0)
KINDS = ("mlx-vlm",)
REVISION = "475b9088" + "0" * 32
WINDOWS = ((0, 4000), (4000, 9000))


def _model(
    repo: str = "mlx-community/gemma-4-e4b-it-4bit",
    *,
    memory_gb: float = 5.0,
    takes: tuple[str, ...] = ("text", "image"),
    state: str = registry.INSTALLED,
    runtime: str = "mlx-vlm",
) -> LocalModel:
    publisher = repo.split("/")[0]
    return LocalModel(
        repo_id=repo,
        repo_key=repo.casefold(),
        revision=REVISION,
        variant="",
        publisher=publisher,
        chain=[
            {
                "repo_id": repo,
                "publisher": publisher,
                "bases": ["google/gemma-4-E4B-it"],
                "found": True,
            },
            {"repo_id": "google/gemma-4-E4B-it", "publisher": "google", "bases": [], "found": True},
        ],
        runtime=runtime,
        takes=list(takes),
        state=state,
        memory_gb=memory_gb,
    )


async def _never_run(_ctx: jobs.JobContext, _job: LocalAiJob) -> dict[str, Any] | None:
    raise AssertionError("no job is run in these tests")


@pytest.fixture(autouse=True)
def _kinds_are_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kinds S6 and S7 register; nothing here runs them."""
    for kind, needs_model in ((reviews.LOOK_KIND, True), (reviews.PREPARE_KIND, False)):
        monkeypatch.setitem(
            jobs._KINDS, kind, jobs.Kind(_never_run, needs_model, title=lambda job: job.subject)
        )
    # Whether the drive for playable copies is there is asked of the disk;
    # here it is always there unless a test says otherwise.
    monkeypatch.setattr(reviews.stores, "away", lambda kind, settings=None: None)


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'reviews.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_RUNTIMES_DIR", str(tmp_path / "runtimes"))
    monkeypatch.delenv("FRAMEFOUND_LOCAL_AI_DEMO", raising=False)
    get_settings.cache_clear()
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path))
        admin = User(email="owner@example.com", password_hash="x", role="admin")
        member = User(email="crew@example.com", password_hash="x", role="user")
        model = _model()
        db.add_all([library, admin, member, model])
        await db.flush()
        env: dict[str, Any] = {
            "factory": factory,
            "library": library.id,
            "admin": admin,
            "member": member,
            "model": model,
        }
        await db.commit()
    yield env
    await engine.dispose()
    get_settings.cache_clear()


async def _asset(
    env: dict[str, Any],
    relative_path: str,
    media_type: str,
    derived: Sequence[str],
    spans: Sequence[tuple[int, int]] = (),
) -> uuid.UUID:
    async with env["factory"]() as db:
        asset = Asset(
            library_id=env["library"],
            relative_path=relative_path,
            filename=relative_path.rsplit("/", 1)[-1],
            extension=relative_path.rsplit(".", 1)[-1].lower(),
            media_type=media_type,
            size_bytes=1000,
            mtime=BASE,
            availability="online",
            first_indexed_at=BASE,
            duration_s=30.0 if media_type == "video" else None,
        )
        db.add(asset)
        await db.flush()
        for kind in derived:
            db.add(
                Derivative(
                    asset_id=asset.id,
                    kind=kind,
                    relative_path=f"derivatives/{asset.id}/{kind}.jpeg",
                    media_format="jpeg",
                    status="ready",
                )
            )
        for idx, (start, end) in enumerate(spans):
            db.add(ShotWindow(asset_id=asset.id, idx=idx, start_ms=start, end_ms=end, move="orbit"))
        await db.commit()
        return asset.id


async def _photo(env: dict[str, Any], relative_path: str = "Pete Dye/IMG_0001.JPG") -> uuid.UUID:
    return await _asset(env, relative_path, "image", ("thumbnail", "preview"))


async def _clip(env: dict[str, Any], relative_path: str = "Pete Dye/DJI_0001.MP4") -> uuid.UUID:
    return await _asset(
        env, relative_path, "video", ("poster", "thumbnail", "proxy", "motion"), WINDOWS
    )


async def _clip_without_copy(env: dict[str, Any], relative_path: str) -> uuid.UUID:
    return await _asset(env, relative_path, "video", ("poster", "thumbnail"))


async def _looker(env: dict[str, Any], model: LocalModel | None = None) -> None:
    async with env["factory"]() as db:
        db.add(LocalAiRole(role="looker", model_id=(model or env["model"]).id))
        await db.commit()


async def _add_model(env: dict[str, Any], **more: Any) -> LocalModel:
    async with env["factory"]() as db:
        model = _model(**more)
        db.add(model)
        await db.commit()
        return model


async def _looked_at(
    env: dict[str, Any],
    asset_id: uuid.UUID,
    spans: Sequence[tuple[int | None, int | None]],
    model: LocalModel,
) -> None:
    """Every span of the asset looked at by `model`, and the roll-up written."""
    async with env["factory"]() as db:
        for start, end in spans:
            db.add(
                Look(
                    asset_id=asset_id,
                    start_ms=start,
                    end_ms=end,
                    key=store.key_for(model),
                    model=model.repo_id,
                    model_revision=model.revision,
                    prompt_version=PROMPT_VERSION,
                    score=70,
                    seconds=8.0 if start is None else 12.0,
                    created_at=datetime.now(UTC),
                )
            )
        await db.flush()
        await store.roll_up(db, asset_id)
        await db.commit()


async def _start(
    env: dict[str, Any],
    *,
    folder: str = "Pete Dye",
    when: str = "now",
    model_id: uuid.UUID | None = None,
    redo: bool = False,
    prepare: bool = True,
    now: datetime = DAY,
    user: User | None = None,
    library_id: uuid.UUID | None = None,
    missing: dict[str, str] | None = None,
) -> LookRequest:
    async with env["factory"]() as db:
        request = await reviews.start(
            db,
            user or env["admin"],
            library_id=library_id or env["library"],
            folder=folder,
            when=when,  # type: ignore[arg-type]
            model_id=model_id,
            redo=redo,
            prepare=prepare,
            now=now,
            known_kinds=KINDS,
            missing=missing,
        )
        await db.commit()
        return request


async def _refused(env: dict[str, Any], **more: Any) -> reviews.ReviewRefused:
    with pytest.raises(reviews.ReviewRefused) as caught:
        await _start(env, **more)
    return caught.value


async def _jobs(env: dict[str, Any]) -> list[LocalAiJob]:
    async with env["factory"]() as db:
        return list((await db.scalars(select(LocalAiJob).order_by(LocalAiJob.created_at))).all())


async def _request(env: dict[str, Any], request_id: uuid.UUID) -> LookRequest:
    async with env["factory"]() as db:
        found = await db.get(LookRequest, request_id)
        assert found is not None
        return found


def _utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


# --- who may start what -----------------------------------------------------------------


async def test_a_review_needs_a_looker_or_a_named_model(env: dict[str, Any]) -> None:
    await _photo(env)
    refused = await _refused(env)
    assert (str(refused), refused.status) == (
        "No model has the Looker role yet. Give one the role on the AI models page.",
        409,
    )
    gone = await _refused(env, model_id=uuid.uuid4())
    assert (str(gone), gone.status) == (NO_SUCH_MODEL, 404)

    named = await _start(env, model_id=env["model"].id)
    [job] = await _jobs(env)
    assert (job.kind, job.model_id, job.role) == (reviews.LOOK_KIND, env["model"].id, None)
    assert (named.model_id, named.model_name) == (env["model"].id, "gemma-4-e4b-it-4bit")


async def test_a_review_by_the_looker_asks_for_the_role_not_the_model(
    env: dict[str, Any],
) -> None:
    """NULL is "whoever is the Looker when it runs": giving the role to another
    model before 21:30 changes who looks tonight."""
    await _photo(env)
    await _looker(env)
    request = await _start(env)
    [job] = await _jobs(env)
    assert (job.model_id, job.role, request.model_id) == (None, "looker", None)
    assert job.payload["request_id"] == str(request.id)
    assert (job.priority, job.subject, job.requested_by) == (
        PRIORITY_REVIEW,
        "GELCO / Pete Dye",
        env["admin"].id,
    )
    assert (request.look_job_id, request.state, request.total) == (job.id, "active", 1)


async def test_every_refusal_says_why_in_its_own_words(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _photo(env)
    await _looker(env)
    blind = await _add_model(env, repo="google/gemma-4-text-only", takes=("text",))
    blocked = await _add_model(env, repo="mlx-community/someone-else")
    async with env["factory"]() as db:
        await db.execute(
            update(LocalModel)
            .where(LocalModel.id == blocked.id)
            .values(
                chain=[
                    {
                        "repo_id": blocked.repo_id,
                        "publisher": "unknown-lab",
                        "bases": [],
                        "found": True,
                    }
                ]
            )
        )
        await db.commit()

    cases: list[tuple[dict[str, Any], str, int]] = [
        ({"user": env["member"]}, "Only an administrator can start a review.", 403),
        ({"when": "soon"}, 'Choose "now" or "tonight".', 400),
        (
            {"folder": "Pete Dye/../Other"},
            "A folder is a path inside the library; it cannot use . or .. to point elsewhere.",
            400,
        ),
        ({"library_id": uuid.uuid4()}, "No such library.", 404),
        ({"model_id": blind.id}, "It does not read pictures.", 409),
        ({"folder": "Nowhere"}, "There is nothing here a model can look at.", 409),
    ]
    for more, sentence, status in cases:
        refused = await _refused(env, **more)
        assert (str(refused), refused.status) == (sentence, status), more
    not_approved = await _refused(env, model_id=blocked.id)
    assert str(not_approved).startswith("It is no longer approved: ")
    assert not_approved.status == 409
    assert await _jobs(env) == []

    monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR")
    get_settings.cache_clear()
    unavailable = await _refused(env)
    assert (str(unavailable), unavailable.status) == (UNAVAILABLE, 409)


async def test_a_model_that_only_fits_overnight_cannot_start_now(env: dict[str, Any]) -> None:
    await _photo(env)
    large = await _add_model(env, repo="mlx-community/gemma-4-12B-it-OptiQ-4bit", memory_gb=12.0)
    refused = await _refused(env, model_id=large.id, now=DAY)
    assert (str(refused), refused.status) == (
        "This model only runs overnight (needs about 12 GB; the daytime budget is 8 GB). "
        "Choose Tonight.",
        409,
    )
    started = await _start(env, model_id=large.id, now=NIGHT)
    assert started.run_when == "now"


async def test_a_model_too_large_even_for_the_night_is_not_offered(env: dict[str, Any]) -> None:
    await _add_model(env, repo="mlx-community/demo-large", memory_gb=40.0)
    small = env["model"]
    night_only = await _add_model(env, repo="google/gemma-4-12B-it", memory_gb=12.0)
    await _add_model(env, repo="google/gemma-4-text", takes=("text",))
    await _add_model(env, repo="google/gemma-4-half", state=registry.DOWNLOADING)
    await _looker(env, night_only)
    async with env["factory"]() as db:
        by_day = await reviews.candidates(db, DAY, known_kinds=KINDS)
        by_night = await reviews.candidates(db, NIGHT, known_kinds=KINDS)
        no_program = await reviews.candidates(db, DAY, known_kinds=())
    assert [(c.model.id, c.fits, c.is_looker) for c in by_day] == [
        (night_only.id, "night", True),
        (small.id, "day", False),
    ]
    assert [c.why_not_now for c in by_day] == [
        "This model only runs overnight (needs about 12 GB; the daytime budget is 8 GB).",
        None,
    ]
    assert [c.why_not_now for c in by_night] == [None, None]
    assert no_program == []


async def test_a_model_whose_program_is_missing_stays_a_choice_but_cannot_start(
    env: dict[str, Any],
) -> None:
    """Offered with the reason (so the dialog can say what to install), and
    refused with it on Start now and Tonight alike, before anything is made."""
    await _photo(env)
    await _looker(env)
    said = (
        "MLX-VLM is not installed. Install it under Software that runs models on the AI "
        "models page."
    )
    missing = {"mlx-vlm": said}
    async with env["factory"]() as db:
        offered = await reviews.candidates(db, DAY, known_kinds=KINDS, missing=missing)
        as_before = await reviews.candidates(db, DAY, known_kinds=KINDS)
    assert [(c.model.id, c.program_missing) for c in offered] == [(env["model"].id, said)]
    assert [c.program_missing for c in as_before] == [None]
    for when in ("now", "tonight"):
        refused = await _refused(env, when=when, missing=missing)
        assert (str(refused), refused.status) == (said, 409)
    assert await _jobs(env) == []


async def test_a_tonight_review_is_created_waiting_for_the_window(env: dict[str, Any]) -> None:
    await _photo(env)
    await _photo(env, "Brookside/IMG_0002.JPG")
    await _looker(env)
    request = await _start(env, when="tonight", now=DAY)
    [job] = await _jobs(env)
    assert (request.run_when, job.state) == ("tonight", "waiting")
    assert _utc(job.not_before) == datetime(2026, 11, 12, 21, 30).astimezone(UTC)

    # Asked for while the window is open: it may start at once, and the
    # job still stops at 05:30 because the request says tonight.
    early = await _start(env, folder="Brookside", when="tonight", now=NIGHT)
    assert early.run_when == "tonight"
    latest = (await _jobs(env))[-1]
    assert (latest.state, latest.not_before) == ("queued", None)


async def test_tonight_needs_a_window_to_wait_for(env: dict[str, Any]) -> None:
    await _photo(env)
    await _looker(env)
    async with env["factory"]() as db:
        db.add(AppSetting(key="local_models", value={"night_start": "21:30", "night_end": "21:30"}))
        await db.commit()
    refused = await _refused(env, when="tonight")
    assert str(refused) == (
        "There is no overnight window: it starts and ends at 21:30. Change it under Sharing "
        "this Mac, or choose Start now."
    )


async def test_a_folder_with_nothing_new_is_not_reviewed_again(env: dict[str, Any]) -> None:
    photo = await _photo(env)
    clip = await _clip(env)
    await _looker(env)
    older = await _add_model(env, repo="google/gemma-4-E4B-it")
    await _looked_at(env, photo, [(None, None)], env["model"])
    await _looked_at(env, clip, WINDOWS, older)

    async with env["factory"]() as db:
        await db.execute(update(LocalAiRole).values(model_id=env["model"].id))
        await db.commit()
    # The clip was looked at by another model: only redo asks about it again.
    refused = await _refused(env)
    assert (str(refused), refused.status) == ("Everything here has been looked at.", 409)
    redone = await _start(env, redo=True)
    assert (redone.redo, redone.total) == (True, 2)


async def test_one_review_at_a_time_for_a_folder(env: dict[str, Any]) -> None:
    await _photo(env)
    await _photo(env, "Pete Dye/Drone/IMG_0003.JPG")
    await _photo(env, "Brookside/IMG_0002.JPG")
    await _looker(env)
    first = await _start(env)
    refused = await _refused(env)
    assert (str(refused), refused.status) == ("A review of this folder is already running.", 409)
    # A folder inside it, and another folder, may be reviewed meanwhile:
    # each item is looked at once, and the second finds less to do.
    await _start(env, folder="Pete Dye/Drone")
    await _start(env, folder="Brookside")
    async with env["factory"]() as db:
        await reviews.cancel(db, first.id)
        await db.commit()
    again = await _start(env)
    assert again.id != first.id


async def test_a_long_folder_name_is_cut_in_the_subject_and_kept_whole_in_the_request(
    env: dict[str, Any],
) -> None:
    folder = "/".join(["A very long folder name for a shoot"] * 12)
    await _photo(env, f"{folder}/IMG_0001.JPG")
    await _looker(env)
    request = await _start(env, folder=folder)
    [job] = await _jobs(env)
    assert request.folder == folder
    assert len(job.subject) == reviews.SUBJECT_CHARS
    assert job.subject.endswith(f"… {request.id.hex[:8]}")
    # The next request's subject would differ (its own digits), so only the
    # request table can say this folder is already under review.
    refused = await _refused(env, folder=folder)
    assert str(refused) == "A review of this folder is already running."


async def test_a_review_whose_job_ended_elsewhere_does_not_block_the_next(
    env: dict[str, Any],
) -> None:
    await _photo(env)
    await _looker(env)
    first = await _start(env)
    async with env["factory"]() as db:
        await jobs.request_cancel(db, first.look_job_id)  # type: ignore[arg-type]
        await db.commit()
    second = await _start(env)
    assert (await _request(env, first.id)).state == "cancelled"
    assert second.state == "active"


async def test_a_folder_holding_nothing_a_model_looks_at_is_not_called_reviewed(
    env: dict[str, Any],
) -> None:
    """A folder of sound files was never looked at: "everything here has been
    looked at" would be false, so the refusal says what is there instead."""
    await _asset(env, "Interviews/owner.mp3", "audio", ())
    await _asset(env, "Interviews/agent.wav", "audio", ())
    await _looker(env)
    refused = await _refused(env, folder="Interviews")
    assert (str(refused), refused.status) == (
        "There is nothing here a model can look at. Of the files here, 2 are sound only.",
        409,
    )


async def test_a_review_whose_worker_died_does_not_block_the_next(env: dict[str, Any]) -> None:
    """The worker stopped mid-review and nothing has ticked since. The page
    already reads the review as stopped; Start agrees, ends the dead job as
    the lane's tick would, and begins a new review of the folder."""
    await _photo(env)
    await _looker(env)
    first = await _start(env)
    gone = DAY.astimezone(UTC) - timedelta(hours=1)
    async with env["factory"]() as db:
        await db.execute(update(LocalAiJob).values(state="running", started_at=gone, beat_at=gone))
        await db.commit()
    second = await _start(env)
    old = await _request(env, first.id)
    assert (old.state, old.message) == ("failed", jobs.STOPPED)
    assert second.state == "active"
    by_id = {job.id: job for job in await _jobs(env)}
    assert by_id[first.look_job_id].state == "failed"  # type: ignore[index]
    assert by_id[second.look_job_id].state == "queued"  # type: ignore[index]


async def test_cancelling_a_review_whose_worker_died_frees_the_folder(
    env: dict[str, Any],
) -> None:
    """Asking a dead job to stop would leave its row running for ever, and the
    next review of the folder refused by it as already running."""
    await _photo(env)
    await _looker(env)
    first = await _start(env)
    gone = datetime.now(UTC) - timedelta(hours=1)
    async with env["factory"]() as db:
        await db.execute(update(LocalAiJob).values(state="running", started_at=gone, beat_at=gone))
        await db.commit()
        cancelled = await reviews.cancel(db, first.id)
        await db.commit()
    assert cancelled.state == "cancelled"
    [job] = await _jobs(env)
    assert (job.state, job.message) == ("failed", jobs.STOPPED)
    second = await _start(env)
    assert second.state == "active"


async def test_a_dead_job_of_a_review_cancelled_earlier_does_not_refuse_the_next(
    env: dict[str, Any],
) -> None:
    """Cancelled while its worker was alive (the job only asked to stop), and
    then the worker died: the job row is still running under the folder's
    subject, and is ended before the new job is made."""
    await _photo(env)
    await _looker(env)
    first = await _start(env)
    gone = DAY.astimezone(UTC) - timedelta(hours=1)
    async with env["factory"]() as db:
        await db.execute(
            update(LocalAiJob).values(
                state="running", started_at=gone, beat_at=gone, cancel_requested=True
            )
        )
        await db.execute(
            update(LookRequest).where(LookRequest.id == first.id).values(state="cancelled")
        )
        await db.commit()
    second = await _start(env)
    assert second.state == "active"


async def test_clips_whose_drive_is_away_are_still_worth_a_review(env: dict[str, Any]) -> None:
    """No copy can be made while the original is away, so nothing is queued for
    them; the review judges each from one still."""
    away = await _clip_without_copy(env, "Pete Dye/DJI_0009.MP4")
    async with env["factory"]() as db:
        await db.execute(update(Asset).where(Asset.id == away).values(availability="unmounted"))
        await db.commit()
    await _looker(env)
    request = await _start(env)
    assert (request.prepare_job_id, request.prep_state, request.total) == (None, "", 0)


async def test_clips_that_are_not_ready_get_a_make_ready_job_beside_the_review(
    env: dict[str, Any],
) -> None:
    await _clip_without_copy(env, "Pete Dye/DJI_0002.MP4")
    await _photo(env, "Brookside/IMG_0002.JPG")
    await _clip_without_copy(env, "Barns/DJI_0003.MP4")
    await _looker(env)

    request = await _start(env)
    prepare_job, look_job = await _jobs(env)
    assert (prepare_job.kind, look_job.kind) == (reviews.PREPARE_KIND, reviews.LOOK_KIND)
    assert prepare_job.subject == look_job.subject == "GELCO / Pete Dye"
    assert (prepare_job.priority, prepare_job.state, prepare_job.not_before) == (5, "queued", None)
    assert (request.prepare_job_id, request.prep_state) == (prepare_job.id, "running")

    # Without making ready, the review is still started: those clips are
    # judged from one still at the end of its pass.
    skipped = await _start(env, folder="Barns", prepare=False)
    assert (skipped.prepare_job_id, skipped.prep_state, skipped.prepare) == (None, "", False)
    # Nothing to make ready: no second job.
    plain = await _start(env, folder="Brookside")
    assert (plain.prepare_job_id, plain.prep_state) == (None, "")
    assert [j.kind for j in await _jobs(env)].count(reviews.PREPARE_KIND) == 1


async def test_a_review_that_could_not_be_queued_is_withdrawn_whole(env: dict[str, Any]) -> None:
    await _clip_without_copy(env, "Pete Dye/DJI_0002.MP4")
    await _looker(env)
    request = await _start(env)
    async with env["factory"]() as db:
        await reviews.withdraw(db, request.id)
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(LookRequest)) == 0
    assert await _jobs(env) == []


async def test_a_review_the_lane_has_already_taken_is_not_withdrawn(env: dict[str, Any]) -> None:
    """The review is committed before the lane is woken, so the lane's own
    tick can claim one of its jobs while the wake-up is failing. Taking the
    review back then deleted that job's row under it, while the playable
    copies it had asked for were being made."""
    await _clip_without_copy(env, "Pete Dye/DJI_0002.MP4")
    await _clip_without_copy(env, "Barns/DJI_0003.MP4")
    await _looker(env)
    taken = await _start(env)
    waiting = await _start(env, folder="Barns")
    alive = datetime.now(UTC)
    async with env["factory"]() as db:
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == taken.prepare_job_id)
            .values(state="running", started_at=alive, beat_at=alive)
        )
        await db.commit()

        # Whatever else the caller wrote in the same transaction is kept.
        db.add(Library(name="Written meanwhile", root_path="/elsewhere"))
        await db.flush()
        assert await reviews.withdraw(db, taken.id) is False
        await db.commit()
    assert (await _request(env, taken.id)).state == "active"
    states = {job.id: job.state for job in await _jobs(env)}
    assert (states[taken.prepare_job_id], states[taken.look_job_id]) == ("running", "queued")  # type: ignore[index]
    async with env["factory"]() as db:
        assert await db.scalar(select(func.count()).select_from(Library)) == 2

        # One whose jobs both still wait is taken back whole, and says so;
        # so does one that is already gone.
        assert await reviews.withdraw(db, waiting.id) is True
        assert await reviews.withdraw(db, uuid.uuid4()) is True
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(LookRequest)) == 1
    assert len(await _jobs(env)) == 2


async def test_a_review_whose_make_ready_job_has_run_once_is_not_withdrawn(
    env: dict[str, Any],
) -> None:
    """The make-ready job's first run asks the pipeline for the playable
    copies and steps back to `waiting`, which is where it rests. With the
    look job waiting too (for tonight, or paused), both rows read as if
    neither had run: the review was taken back, and the operator told
    nothing had started while the copies were being made."""
    await _clip_without_copy(env, "Pete Dye/DJI_0002.MP4")
    await _clip_without_copy(env, "Barns/DJI_0003.MP4")
    await _clip_without_copy(env, "Brookside/DJI_0004.MP4")
    await _looker(env)
    ran_once = await _start(env, when="tonight")
    ran = datetime.now(UTC)
    async with env["factory"]() as db:
        # As the lane leaves it: claimed (`started_at`), then `Later` in two minutes.
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == ran_once.prepare_job_id)
            .values(
                state="waiting",
                started_at=ran,
                beat_at=ran,
                not_before=ran + timedelta(minutes=2),
                payload={"request_id": str(ran_once.id), "_asked": ["a clip"]},
            )
        )
        await db.commit()
        assert await reviews.withdraw(db, ran_once.id) is False
        await db.commit()
    assert (await _request(env, ran_once.id)).state == "active"
    assert {job.kind: job.state for job in await _jobs(env)} == {
        reviews.PREPARE_KIND: "waiting",
        reviews.LOOK_KIND: "waiting",
    }

    # The same for a look job that looked at what was ready and waits for clips.
    looked = await _start(env, folder="Barns")
    async with env["factory"]() as db:
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == looked.look_job_id)
            .values(state="waiting", started_at=ran, not_before=ran + timedelta(minutes=5))
        )
        await db.commit()
        assert await reviews.withdraw(db, looked.id) is False

    # A job set to wait before the lane ever ran it (tonight; a pause) has
    # not started anything: that review is still taken back whole.
    untouched = await _start(env, folder="Brookside", when="tonight")
    async with env["factory"]() as db:
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == untouched.prepare_job_id)
            .values(state="waiting", message=schedule.PAUSED, not_before=ran)
        )
        await db.commit()
        assert await reviews.withdraw(db, untouched.id) is True
        await db.commit()
    assert {job.id for job in await _jobs(env)} == {
        ran_once.prepare_job_id,
        ran_once.look_job_id,
        looked.prepare_job_id,
        looked.look_job_id,
    }


async def test_a_review_that_ended_without_its_handler_takes_its_make_ready_job_with_it(
    env: dict[str, Any],
) -> None:
    """Its look job was cancelled on the AI models page before it ran, or
    crashed, or its record was pruned. The review is ended when that is next
    noticed — and its make-ready job used to wait on for two more minutes
    under the folder's name, so starting the folder again was refused as
    "already running" when nothing was."""
    await _looker(env)

    async def cancelled(db: AsyncSession, job_id: uuid.UUID) -> None:
        await jobs.request_cancel(db, job_id)

    async def crashed(db: AsyncSession, job_id: uuid.UUID) -> None:
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == job_id)
            .values(
                state="failed",
                message="It failed unexpectedly (KeyError).",
                finished_at=datetime.now(UTC),
            )
        )

    async def pruned(db: AsyncSession, job_id: uuid.UUID) -> None:
        await db.execute(LocalAiJob.__table__.delete().where(LocalAiJob.id == job_id))

    for folder, ending, state in (
        ("Pete Dye", cancelled, "cancelled"),
        ("Barns", crashed, "failed"),
        ("Brookside", pruned, "failed"),
    ):
        await _clip_without_copy(env, f"{folder}/DJI_0002.MP4")
        first = await _start(env, folder=folder)
        assert first.look_job_id is not None and first.prepare_job_id is not None
        async with env["factory"]() as db:
            await ending(db, first.look_job_id)
            await db.commit()
            if ending is pruned:
                # Noticed by the Processing page rather than by Start.
                await reviews.listing(db, DAY)
                await db.commit()

        # Start, pressed at once: nothing of the old review stands in its way.
        second = await _start(env, folder=folder)
        old = await _request(env, first.id)
        assert (old.state, old.prep_state) == (state, "done"), folder
        by_id = {job.id: job for job in await _jobs(env)}
        over = by_id[first.prepare_job_id]
        assert (over.state, over.message) == ("done", reviews.PREPARE_OVER), folder
        assert reviews.PREPARE_OVER == "The review it was for has ended."
        assert second.prepare_job_id is not None
        assert by_id[second.prepare_job_id].state == "queued", folder


# --- cancelling ---------------------------------------------------------------------------


async def test_cancelling_a_review_cancels_both_its_jobs(env: dict[str, Any]) -> None:
    await _clip_without_copy(env, "Pete Dye/DJI_0002.MP4")
    await _looker(env)
    request = await _start(env)
    async with env["factory"]() as db:
        await db.execute(
            update(LookRequest)
            .where(LookRequest.id == request.id)
            .values(done=212, total=505, prep_queued_at=datetime.now(UTC))
        )
        await db.commit()
        cancelled = await reviews.cancel(db, request.id)
        await db.commit()
    assert cancelled.state == "cancelled"
    assert cancelled.message == (
        "Cancelled. 212 of 505 were looked at; their results are kept. "
        "The playable copies already asked for are still being made."
    )
    assert [j.state for j in await _jobs(env)] == ["cancelled", "cancelled"]

    async with env["factory"]() as db:
        with pytest.raises(reviews.ReviewRefused) as again:
            await reviews.cancel(db, request.id)
        with pytest.raises(reviews.ReviewRefused) as unknown:
            await reviews.cancel(db, uuid.uuid4())
    assert (str(again.value), again.value.status) == ("It has already finished.", 409)
    assert (str(unknown.value), unknown.value.status) == ("There is no such review.", 404)


async def test_a_finished_review_cannot_be_cancelled_and_its_jobs_are_left_alone(
    env: dict[str, Any],
) -> None:
    await _clip_without_copy(env, "Pete Dye/DJI_0002.MP4")
    await _looker(env)
    request = await _start(env)
    async with env["factory"]() as db:
        await db.execute(
            update(LookRequest).where(LookRequest.id == request.id).values(state="done")
        )
        await db.commit()
        with pytest.raises(reviews.ReviewRefused) as refused:
            await reviews.cancel(db, request.id)
        await db.commit()
    assert str(refused.value) == "It has already finished."
    # The copies still being made are ordinary copies: nothing stops them.
    assert [j.state for j in await _jobs(env)] == ["queued", "queued"]


async def test_cancelling_a_running_review_asks_its_job_to_stop(env: dict[str, Any]) -> None:
    await _photo(env)
    await _looker(env)
    request = await _start(env)
    async with env["factory"]() as db:
        await db.execute(update(LocalAiJob).values(state="running", started_at=datetime.now(UTC)))
        await db.commit()
        await reviews.cancel(db, request.id)
        await db.commit()
    [job] = await _jobs(env)
    # It stops between two items, at its next progress; what it did is kept.
    assert (job.state, job.cancel_requested) == ("running", True)
    assert (await _request(env, request.id)).state == "cancelled"


# --- how a review reads --------------------------------------------------------------------


def _req(**more: Any) -> LookRequest:
    values: dict[str, Any] = {
        "state": "active",
        "run_when": "now",
        "done": 120,
        "total": 505,
        "message": "",
        "prep_queued_at": None,
    }
    values.update(more)
    return LookRequest(**values)


def _job(state: str, message: str = "", **more: Any) -> LocalAiJob:
    return LocalAiJob(kind=reviews.LOOK_KIND, state=state, message=message, **more)


def test_a_review_reads_as_paused_waiting_looking_or_tonight_from_its_job() -> None:
    now = datetime.now()
    alive = datetime.now(UTC)
    window = "Waiting for the overnight window (starts 21:30): it needs about 12 GB and the …"
    cases: list[tuple[LookRequest, LocalAiJob | None, LocalAiJob | None, tuple[str, str]]] = [
        (_req(run_when="tonight"), _job("waiting"), None, ("tonight", "Starts tonight at 21:30.")),
        (
            _req(run_when="tonight"),
            _job("waiting", schedule.PAUSED),
            None,
            ("paused", "Paused. It carries on when local AI is resumed."),
        ),
        (_req(), _job("waiting", window, started_at=alive), None, ("waiting", window)),
        (
            _req(run_when="tonight"),
            _job("waiting", "Stopped for the day at 05:30 — 212 of 505 done.", started_at=alive),
            None,
            ("waiting", "Stopped for the day at 05:30 — 212 of 505 done."),
        ),
        (_req(), _job("queued"), None, ("waiting", "Waiting its turn.")),
        (
            _req(),
            _job("queued"),
            _job("waiting", "Getting 12 clips ready (9 to go)."),
            ("waiting", "Getting 12 clips ready (9 to go)."),
        ),
        (
            _req(),
            _job("running", "DJI_0639.MP4", started_at=alive, beat_at=alive),
            _job("done", "Ready."),
            ("looking", "DJI_0639.MP4"),
        ),
        (
            _req(),
            _job("running", "DJI_0639.MP4", started_at=alive, beat_at=alive - timedelta(hours=1)),
            None,
            ("failed", jobs.STOPPED),
        ),
        (_req(), _job("failed", "It would not load."), None, ("failed", "It would not load.")),
        (
            _req(),
            _job("cancelled", jobs.CANCELLED),
            None,
            ("cancelled", "Cancelled. 120 of 505 were looked at; their results are kept."),
        ),
        (_req(), None, None, ("failed", "Its record is gone; start it again.")),
        (
            _req(state="done", message="Looked at 505 items."),
            _job("running", "x", started_at=alive, beat_at=alive),
            None,
            ("done", "Looked at 505 items."),
        ),
        (_req(state="failed", message="Stopped: …"), None, None, ("failed", "Stopped: …")),
        (
            _req(state="cancelled", done=121),
            None,
            None,
            ("cancelled", "Cancelled. 121 of 505 were looked at; their results are kept."),
        ),
    ]
    for request, look_job, prepare_job, expected in cases:
        assert reviews.shown(request, look_job, prepare_job, now) == expected, expected

    later = schedule.Budgets(night_start=schedule.parse_clock("22:15"))
    assert reviews.shown(_req(run_when="tonight"), _job("waiting"), None, now, budgets=later) == (
        "tonight",
        "Starts tonight at 22:15.",
    )


def test_a_review_waiting_for_clips_whose_drive_is_away_names_the_drive() -> None:
    """ "Waiting for 3 clips to be made ready." while the drive they are made
    on is unplugged left the operator waiting for something only they could
    put right."""
    now = datetime.now()
    alive = datetime.now(UTC)
    clips = "Waiting for 3 clips to be made ready."
    away = "The proxy store is not connected. Reconnect the drive or share and try again."
    silent = (
        "The drive for playable copies is not answering. The clips are made ready once it does."
    )
    getting = "Getting 3 clips ready (3 to go)."
    night_over = "Stopped for the day at 05:30 — 212 of 505 done. Carries on at 21:30."

    def for_clips(message: str = clips) -> LocalAiJob:
        return _job("waiting", message, started_at=alive, phase=reviews.PHASE_CLIPS)

    def held(message: str, state: str = "waiting", **more: Any) -> LocalAiJob:
        return _job(state, message, phase=reviews.PHASE_DRIVE, **more)

    long_ago = alive - timedelta(hours=1)
    cases: list[tuple[LookRequest, LocalAiJob, LocalAiJob | None, tuple[str, str]]] = [
        (_req(), for_clips(), held(away), ("waiting", away)),
        (_req(), for_clips(), held(silent), ("waiting", silent)),
        # Its five minutes are up and it is about to look again, or is
        # looking this moment: the drive is still the last thing known.
        (_req(), for_clips(), held(away, "queued"), ("waiting", away)),
        (
            _req(),
            for_clips(),
            held(away, "running", started_at=alive, beat_at=alive),
            ("waiting", away),
        ),
        # Its worker died while it looked: it reads as stopped, and says nothing.
        (
            _req(),
            for_clips(),
            held(away, "running", started_at=long_ago, beat_at=long_ago),
            ("waiting", clips),
        ),
        # The clips are on their way: the review's own sentence, as before.
        (_req(), for_clips(), _job("waiting", getting, phase="preparing"), ("waiting", clips)),
        (_req(), for_clips(), None, ("waiting", clips)),
        # The make-ready job has ended: what it last said is not news.
        (_req(), for_clips(), held(away, "done"), ("waiting", clips)),
        # A wait of the review's own is not hidden behind the drive.
        (
            _req(run_when="tonight"),
            _job("waiting", night_over, started_at=alive, phase="looking"),
            held(away),
            ("waiting", night_over),
        ),
        (
            _req(),
            for_clips(schedule.PAUSED),
            held(away),
            ("paused", "Paused. It carries on when local AI is resumed."),
        ),
    ]
    for request, look_job, prepare_job, expected in cases:
        assert reviews.shown(request, look_job, prepare_job, now) == expected, expected


async def test_a_review_whose_job_record_is_gone_reads_as_failed(env: dict[str, Any]) -> None:
    await _photo(env)
    await _looker(env)
    request = await _start(env)
    async with env["factory"]() as db:
        await db.execute(LocalAiJob.__table__.delete())
        await db.commit()
        body = await reviews.listing(db, DAY)
        await db.commit()
    [shown] = body["reviews"]
    assert (shown["state"], shown["message"], shown["can_cancel"]) == (
        "failed",
        "Its record is gone; start it again.",
        False,
    )
    # Written back: the folder can be reviewed again.
    assert (await _request(env, request.id)).state == "failed"
    await _start(env)


async def test_a_job_cancelled_elsewhere_does_not_leave_the_review_active(
    env: dict[str, Any],
) -> None:
    await _clip_without_copy(env, "Pete Dye/DJI_0002.MP4")
    await _looker(env)
    request = await _start(env)
    async with env["factory"]() as db:
        # From the AI models page: the job only, not the review.
        await jobs.request_cancel(db, request.look_job_id)  # type: ignore[arg-type]
        await jobs.request_cancel(db, request.prepare_job_id)  # type: ignore[arg-type]
        await db.commit()
        body = await reviews.listing(db, DAY)
        await db.commit()
    assert [r["state"] for r in body["reviews"]] == ["cancelled"]
    settled = await _request(env, request.id)
    assert (settled.state, settled.prep_state) == ("cancelled", "done")
    assert settled.finished_at is not None


async def test_the_listing_shows_progress_with_who_asked_and_the_time_left(
    env: dict[str, Any],
) -> None:
    await _photo(env)
    await _photo(env, "Brookside/IMG_0002.JPG")
    done = await _photo(env, "Barns/IMG_0003.JPG")
    await _looker(env)
    await _looked_at(env, done, [(None, None)], env["model"])  # 8 s a photograph, measured
    running = await _start(env)
    finished = await _start(env, folder="Brookside")
    async with env["factory"]() as db:
        await db.execute(
            update(LookRequest).where(LookRequest.id == running.id).values(done=2, total=12)
        )
        await db.execute(
            update(LookRequest)
            .where(LookRequest.id == finished.id)
            .values(state="done", message="Looked at 1 item.", finished_at=datetime.now(UTC))
        )
        await db.commit()
        body = await reviews.listing(db, DAY)
    assert body["available"] is True
    first, second = body["reviews"]
    assert (first["id"], first["state"], first["can_cancel"]) == (str(running.id), "waiting", True)
    assert (first["library_name"], first["folder"], first["requested_by"]) == (
        "GELCO",
        "Pete Dye",
        "owner@example.com",
    )
    # 10 items at the mean of 8 s and the borrowed 12 s.
    assert first["seconds_left"] == 100
    assert (second["state"], second["message"], second["seconds_left"]) == (
        "done",
        "Looked at 1 item.",
        None,
    )
    assert first["created_at"].endswith("Z")


async def test_libraries_that_look_at_new_footage_have_a_line_each(env: dict[str, Any]) -> None:
    await _photo(env)
    await _photo(env, "Pete Dye/IMG_0002.JPG")
    await _looker(env)
    async with env["factory"]() as db:
        await db.execute(update(Library).values(look_at_ingest=True, look_since=BASE))
        db.add(Library(name="Quiet", root_path="/elsewhere"))
        await db.commit()
        body = await reviews.listing(db, DAY)
    assert body["ingest"] == [
        {
            "library_id": str(env["library"]),
            "library_name": "GELCO",
            "waiting": 2,
            "state": "idle",
            "message": "",
            "done": 0,
            "total": 0,
        }
    ]


async def test_a_library_left_alone_after_a_failed_job_says_so_and_when_it_is_tried_again(
    env: dict[str, Any],
) -> None:
    """After a job that could not use the Looker, nothing is made for the
    library for six hours. Its line read "idle" with files waiting, as if
    they were about to be looked at."""
    await _photo(env)
    await _looker(env)
    failed_at = DAY.astimezone(UTC)

    async def held(cause: str | None) -> uuid.UUID:
        """The library's newest ingest job, failed and keeping the hold's mark."""
        async with env["factory"]() as db:
            looker = await registry.get_model(db, env["model"].id)
            assert looker is not None
            mark = reviews.hold_mark(looker, cause or reviews.HELD_BY_MODEL)
            if cause is None:
                del mark["why"]  # a mark written before the cause was kept
            job = LocalAiJob(
                kind=reviews.INGEST_KIND,
                subject="GELCO",
                role="looker",
                state="failed",
                message="Stopped: the model could not be used — It would not load.",
                payload={"library_id": str(env["library"]), reviews.HELD: mark},
                created_at=datetime.now(UTC),
                finished_at=failed_at,
            )
            db.add(job)
            await db.commit()
            return job.id

    async def line(now: datetime = DAY) -> dict[str, Any]:
        async with env["factory"]() as db:
            [found] = (await reviews.listing(db, now))["ingest"]
            return dict(found)

    async with env["factory"]() as db:
        await db.execute(update(Library).values(look_at_ingest=True, look_since=BASE))
        await db.commit()
    idle = await line()
    assert (idle["state"], idle["waiting"]) == ("idle", 1) and "retry_at" not in idle

    await held(reviews.HELD_BY_MODEL)
    because_of_the_model = "Looking at new footage stopped because of a problem with the model"
    tried_again = (failed_at + reviews.INGEST_HOLD).isoformat(timespec="seconds")
    assert await line() == {
        "library_id": str(env["library"]),
        "library_name": "GELCO",
        "waiting": 1,
        "state": "failed",
        "message": f"{because_of_the_model}, and is tried again in about 6 hours.",
        "done": 0,
        "total": 0,
        "retry_at": tried_again.replace("+00:00", "Z"),
    }
    for after, when in (
        (timedelta(hours=2, minutes=20), "in about 4 hours"),
        (timedelta(hours=5, minutes=5), "in about an hour"),
        (timedelta(hours=5, minutes=38), "in about 20 minutes"),
        (timedelta(hours=5, minutes=57), "in a few minutes"),
    ):
        said = await line(DAY + after)
        assert said["message"] == f"{because_of_the_model}, and is tried again {when}.", after
    # The six hours are up: the next scanner pass makes a job, and the line
    # reads as any library's with files waiting.
    assert await line(DAY + reviews.INGEST_HOLD) == idle

    # What stopped it is said truly: not every hold is the model's doing.
    for cause, sentence in (
        (None, because_of_the_model),
        (reviews.HELD_BY_STOPPING, "Looking at new footage stopped after something went wrong"),
        (reviews.HELD_BY_ERROR, "Looking at new footage stopped after something went wrong"),
    ):
        await held(cause)
        said = await line()
        assert said["message"] == f"{sentence}, and is tried again in about 6 hours.", cause

    async def answers_something(minutes: int) -> None:
        """The Looker answers anything anywhere (a Test, a review, another
        library's footage): every answer writes its row."""
        async with env["factory"]() as db:
            await db.execute(
                update(LocalModel).values(updated_at=datetime.now(UTC) + timedelta(minutes=minutes))
            )
            await db.commit()

    # A hold that was not the model's doing is not lifted by the model
    # answering something else: that says nothing about the footage that
    # stopped the worker here, and a Looker busy with a review would lift it
    # at every answer, for the next scanner pass to stop the worker again.
    for minutes, cause in enumerate((reviews.HELD_BY_STOPPING, reviews.HELD_BY_ERROR), start=1):
        await held(cause)
        await answers_something(minutes)
        said = await line()
        assert (said["state"], said["retry_at"]) == ("failed", tried_again.replace("+00:00", "Z"))
    assert await line(DAY + reviews.INGEST_HOLD) == idle

    # One the model caused is: it answers something (a Test passes), so it
    # works again, and the line no longer says otherwise.
    await held(reviews.HELD_BY_MODEL)
    assert (await line())["state"] == "failed"
    await answers_something(5)
    assert await line() == idle

    # Every hold is over when the role has gone to another model since
    # (even one whose row was last written at the very same moment).
    other = await _add_model(env, repo="mlx-community/gemma-4-12B-it-OptiQ-4bit")
    for cause in (reviews.HELD_BY_MODEL, reviews.HELD_BY_STOPPING, reviews.HELD_BY_ERROR):
        async with env["factory"]() as db:
            await db.execute(update(LocalAiRole).values(model_id=env["model"].id))
            await db.execute(update(LocalModel).values(updated_at=datetime.now(UTC)))
            await db.commit()
        await held(cause)
        assert (await line())["state"] == "failed", cause
        async with env["factory"]() as db:
            await db.execute(update(LocalAiRole).values(model_id=other.id))
            await db.commit()
        assert (await line())["state"] == "idle", cause


async def test_a_library_whose_looker_may_no_longer_look_is_not_promised_another_try(
    env: dict[str, Any],
) -> None:
    """A publisher is taken off the approved list while new footage is being
    looked at: the job fails, and the library is held. Nothing is made for a
    Looker that may not look, so the line's "tried again in about 6 hours"
    counted down to nothing — and then read idle, with the files waiting for
    good."""
    await _photo(env)
    await _looker(env)
    async with env["factory"]() as db:
        await db.execute(update(Library).values(look_at_ingest=True, look_since=BASE))
        looker = await registry.get_model(db, env["model"].id)
        assert looker is not None
        db.add(
            LocalAiJob(
                kind=reviews.INGEST_KIND,
                subject="GELCO",
                role="looker",
                state="failed",
                message="It is no longer approved.",
                payload={
                    "library_id": str(env["library"]),
                    reviews.HELD: reviews.hold_mark(looker, reviews.HELD_BY_MODEL),
                },
                created_at=datetime.now(UTC),
                finished_at=DAY.astimezone(UTC),
            )
        )
        await db.commit()

    async def line(now: datetime = DAY) -> dict[str, Any]:
        async with env["factory"]() as db:
            [found] = (await reviews.listing(db, now))["ingest"]
            return dict(found)

    held = await line()
    assert held["state"] == "failed" and held["message"].endswith("tried again in about 6 hours.")

    # Its files are no longer whole (the same for a publisher no longer
    # approved, a budget it no longer fits, a program this install lacks).
    # Its row is otherwise as it was: the hold itself still stands.
    async with env["factory"]() as db:
        await db.execute(
            update(LocalModel)
            .where(LocalModel.id == env["model"].id)
            .values(state=registry.INCOMPLETE, updated_at=LocalModel.updated_at)
        )
        await db.commit()
    stopped = {
        "library_id": str(env["library"]),
        "library_name": "GELCO",
        "waiting": 1,
        "state": "failed",
        "message": (
            "Looking at new footage is stopped until the Looker can be used. "
            "Its download did not finish. Resume the download first."
        ),
        "done": 0,
        "total": 0,
    }
    # No time is promised, during the hold or after it: nothing is tried
    # until somebody puts the model right.
    assert await line() == stopped
    assert await line(DAY + reviews.INGEST_HOLD + timedelta(hours=1)) == stopped

    # A job still at work reads as itself; the sentence is for when nothing is coming.
    async with env["factory"]() as db:
        db.add(
            LocalAiJob(
                kind=reviews.INGEST_KIND,
                subject="GELCO",
                role="looker",
                state="queued",
                payload={"library_id": str(env["library"])},
                created_at=datetime.now(UTC) + timedelta(seconds=5),
            )
        )
        await db.commit()
    assert (await line())["state"] == "waiting"
    async with env["factory"]() as db:
        await db.execute(
            update(LocalAiJob).where(LocalAiJob.state == "queued").values(state="cancelled")
        )
        # Put right: the line reads as any library's with files waiting.
        await db.execute(
            update(LocalModel)
            .where(LocalModel.id == env["model"].id)
            .values(state=registry.INSTALLED)
        )
        await db.commit()
    again = await line(DAY + reviews.INGEST_HOLD + timedelta(hours=1))
    assert (again["state"], again["message"]) == ("idle", "") and "retry_at" not in again


async def test_nothing_is_listed_where_models_cannot_run(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR")
    get_settings.cache_clear()
    async with env["factory"]() as db:
        assert await reviews.listing(db, DAY) == {"available": False, "reviews": [], "ingest": []}


# --- what the dialog is told ----------------------------------------------------------------


async def test_the_dialog_counts_what_is_new_and_what_must_be_made_first(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _photo(env)
    await _clip(env)
    await _clip_without_copy(env, "Pete Dye/Drone/DJI_0002.MP4")
    await _photo(env, "Pete Dye/Barns/IMG_0004.JPG")
    await _looker(env)
    monkeypatch.setattr(
        reviews.stores, "away", lambda kind, settings=None: f"The {kind} drive is away."
    )
    async with env["factory"]() as db:
        library = await db.get(Library, env["library"])
        assert library is not None
        body = await reviews.preview(
            db, library, "/Pete Dye/", model_id=None, redo=False, now=DAY, known_kinds=KINDS
        )
    assert (body["available"], body["folder"], body["folders_inside"]) == (True, "Pete Dye", 2)
    assert body["todo"] == {"photos": 2, "clips": 1, "shots": 2, "stills_only": 0}
    assert body["first"]["need_copy"] == 1
    assert body["first"]["problem"] == "The proxies drive is away."
    assert body["model"]["name"] == "gemma-4-e4b-it-4bit"
    assert (body["model"]["fits"], body["model"]["why_not_now"]) == ("day", None)
    assert body["estimate"]["basis"] == "none"
    assert body["estimate"]["seconds"] is None
    assert body["estimate"]["words"] == (
        "No estimate yet: nothing has been looked at with this model. The first few will tell."
    )
    assert body["tonight"] == {
        "starts_at": "2026-11-12T21:30:00",
        "open_now": False,
        "problem": None,
    }
    assert body["active"] is None


async def test_the_dialog_says_what_was_already_looked_at_by_kind(env: dict[str, Any]) -> None:
    # Two photographs and two clips of two shots: `already` in items is 6,
    # which the dialog said as "6 items" in a folder of 4 files. By kind it
    # is the folder's own 2 photographs and 2 clips, and their 4 shots; one
    # of those clips was looked at by another model, which redo would add.
    first = await _photo(env)
    second = await _photo(env, "Pete Dye/Barns/IMG_0004.JPG")
    clip = await _clip(env)
    other = await _clip(env, "Pete Dye/DJI_0002.MP4")
    await _looker(env)
    older = await _add_model(env, repo="google/gemma-4-E4B-it")
    for photo in (first, second):
        await _looked_at(env, photo, [(None, None)], env["model"])
    await _looked_at(env, clip, WINDOWS, env["model"])
    await _looked_at(env, other, WINDOWS, older)
    async with env["factory"]() as db:
        library = await db.get(Library, env["library"])
        assert library is not None
        body = await reviews.preview(
            db, library, "Pete Dye", model_id=None, redo=False, now=DAY, known_kinds=KINDS
        )
        zeros = reviews._zeros(library, "Pete Dye", UNAVAILABLE)
    # What read `already` before reads the same numbers.
    assert body["already"] == {"items": 6, "by_another_model": 2}
    assert body["already_kinds"] == {"photos": 2, "clips": 2, "shots": 4}
    assert body["by_another_kinds"] == {"photos": 0, "clips": 1, "shots": 2}
    assert (
        zeros["already_kinds"]
        == zeros["by_another_kinds"]
        == {
            "photos": 0,
            "clips": 0,
            "shots": 0,
        }
    )


async def test_the_dialog_opens_on_a_review_already_running(env: dict[str, Any]) -> None:
    await _photo(env)
    await _looker(env)
    request = await _start(env)
    async with env["factory"]() as db:
        library = await db.get(Library, env["library"])
        assert library is not None
        body = await reviews.preview(
            db, library, "Pete Dye", model_id=None, redo=False, now=DAY, known_kinds=KINDS
        )
        other = await reviews.preview(
            db, library, "Pete Dye/Drone", model_id=None, redo=False, now=DAY, known_kinds=KINDS
        )
    assert body["active"]["id"] == str(request.id)
    assert body["active"]["state"] == "waiting"
    assert other["active"] is None


async def test_the_dialog_without_a_looker_says_so_and_counts_anyway(env: dict[str, Any]) -> None:
    await _photo(env)
    async with env["factory"]() as db:
        library = await db.get(Library, env["library"])
        assert library is not None
        body = await reviews.preview(
            db, library, "", model_id=None, redo=False, now=DAY, known_kinds=KINDS
        )
        with pytest.raises(reviews.ReviewRefused) as gone:
            await reviews.preview(
                db, library, "", model_id=uuid.uuid4(), redo=False, now=DAY, known_kinds=KINDS
            )
        with pytest.raises(reviews.ReviewRefused) as bad:
            await reviews.preview(
                db, library, "a//b", model_id=None, redo=False, now=DAY, known_kinds=KINDS
            )
    assert (body["model"], body["no_looker"]) == (None, reviews.NO_LOOKER)
    assert body["todo"]["photos"] == 1
    assert (gone.value.status, bad.value.status) == (404, 400)


async def test_the_dialog_says_one_sentence_where_models_cannot_run(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _photo(env)
    monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR")
    get_settings.cache_clear()
    async with env["factory"]() as db:
        library = await db.get(Library, env["library"])
        assert library is not None
        body = await reviews.preview(db, library, "", model_id=None, redo=False, now=DAY)
    assert (body["available"], body["reason"]) == (False, UNAVAILABLE)
    assert body["todo"] == {"photos": 0, "clips": 0, "shots": 0, "stills_only": 0}
    assert body["model"] is None


async def test_a_folder_of_clips_still_to_be_made_ready_is_not_called_empty(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drone clips with no playable copy and a model whose speed is measured:
    nothing can be timed yet, but a review will make them ready and look at
    every shot, so the estimate's words must not say nothing is waiting."""
    await _clip_without_copy(env, "Pete Dye/Drone/DJI_0002.MP4")
    await _clip_without_copy(env, "Pete Dye/Drone/DJI_0003.MP4")
    looked = await _photo(env, "Barns/IMG_0003.JPG")
    await _looker(env)
    await _looked_at(env, looked, [(None, None)], env["model"])
    monkeypatch.setattr(reviews.stores, "away", lambda kind, settings=None: None)
    async with env["factory"]() as db:
        library = await db.get(Library, env["library"])
        assert library is not None
        body = await reviews.preview(
            db, library, "Pete Dye/Drone", model_id=None, redo=False, now=DAY, known_kinds=KINDS
        )
    assert body["first"]["need_copy"] == 2
    assert (body["estimate"]["basis"], body["estimate"]["seconds"]) == ("measured", 0)
    assert body["estimate"]["words"] == (
        "The clips here have to be made ready first; how long looking at them takes is "
        "known once their shots are found."
    )
    started = await _start(env, folder="Pete Dye/Drone")
    assert started.state == "active", "the dialog and Start agree there is work"


async def test_the_estimate_comes_from_what_this_model_was_measured_doing(
    env: dict[str, Any],
) -> None:
    await _photo(env)
    await _clip(env)
    looked = await _photo(env, "Barns/IMG_0003.JPG")
    await _looker(env)
    await _looked_at(env, looked, [(None, None)], env["model"])
    async with env["factory"]() as db:
        library = await db.get(Library, env["library"])
        assert library is not None
        body = await reviews.preview(
            db, library, "Pete Dye", model_id=None, redo=False, now=DAY, known_kinds=KINDS
        )
    # One photograph at 8 s and two shots at the borrowed 12 s.
    assert body["estimate"] == {
        "seconds": 32,
        "basis": "measured",
        "per_photo_s": 8.0,
        "per_shot_s": 12.0,
        "sample": 1,
        "words": (
            "Under a minute at this model's measured speed (8 s a photograph, 12 s a shot, "
            "from the one answer timed so far). Loading the model and making playable copies "
            "are extra."
        ),
    }
