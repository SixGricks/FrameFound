"""Demo mode: the AI models page against made-up models (D23; CONTRIBUTING.md).

`FRAMEFOUND_LOCAL_AI_DEMO=1` lets a developer review the local-AI pages in a
browser on a machine that cannot run a model (Windows, the containers'
SQLite review copy). It is honoured only where no runtimes folder and no
public address are configured (`local_models.availability`), so a real
install can never be made to show invented models by one stray variable.

Everything here is reached only through the demo branches of
`deps.deps_for` and `processing.local_ai_tasks.wake`, and each function
refuses to act anywhere else. In demo mode:

- Hugging Face is `fake.FakeHub` over the pages in `demo_data/` — the same
  pages the tests read. A download writes a few kilobytes per file and
  reports progress against the real sizes, slowly enough that the bar moves.
- The software that runs models is `fake.DemoKind` for MLX builds and
  `fake.DemoGgufKind` for GGUF builds, by llama.cpp's rules (installing
  either makes a folder), the installer `fake.FakeRunner`, the model server
  the one process-wide `fake.FakeModelServer` (`ask.server_for`).
- There is no queue. The lane runs inside the API process, on a thread of its
  own with its own event loop and its own database sessions: `wake_inline`
  is what `wake()` does here. It runs the same `jobs.run_next` the worker
  runs, so every job kind — this phase's and every later phase's — works in
  the demo with no code of its own. While anything still waits it looks
  again once a minute, as beat does on the Mac.

`seed` puts a page's worth of rows in a review catalogue (installed models,
the two roles, the software, a finished Test, a failed download, a test
waiting for the overnight window, a GGUF build whose program is not
installed), made by running the real handlers against these stand-ins
rather than written by hand. Run again on a catalogue it has filled it never
runs the lane: a reviewer's work is waiting there, and the review server
does it. What it has added once it never adds again (`seeded_parts`), so a
reviewer who removes something keeps it removed.
"""

import asyncio
import functools
import platform
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import Settings
from framefound.db.models import AppSetting, LocalAiJob, LocalModel
from framefound.local_models import availability
from framefound.local_models.deps import Deps
from framefound.local_models.files import WEIGHTS
from framefound.local_models.hub import UNREACHABLE, HubError

log = structlog.get_logger()

# The banner on every demo page.
DEMO_NOTE = (
    "Demonstration only. Nothing on this page is real: no model is downloaded or run, the "
    "answers are made up, and the sizes and times are invented."
)
NOT_DEMO = "Demo mode is not on here, so none of its stand-ins may be used."

# How a demo download moves: each file is SMALL_FILE_BYTES on disk, served
# CHUNK_BYTES at a time with SECONDS_PER_CHUNK between pieces, so a model of a
# dozen files takes a quarter of a minute or so — long enough to see the bar,
# Cancel and Resume. Tests set the pause to 0.
SMALL_FILE_BYTES = 4096
CHUNK_BYTES = 512
SECONDS_PER_CHUNK = 0.25
# How long the made-up model takes to answer a Test, so "running" can be seen.
ANSWER_SECONDS = 2.0
# What the made-up model is reported to use, as a share of the estimate the
# check made from its size. Below 1, so a demo Test never makes a model look
# as though it needs more than it was judged to (which would move it between
# "by day" and "overnight only" on a review copy for no reason).
MEASURED_SHARE = 0.9
# The beat's minute: while a job waits (the overnight window, a pause), the
# demo's lane looks again this often.
TICK_S = 60.0


# --- the stand-ins ------------------------------------------------------------------


@functools.cache
def _pages() -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], bytes]]:
    """Every model page in `demo_data`, and the file texts they carry. Read once.

    The GGUF repositories (`demo_data/gguf/`) and Compare's demo models
    (`demo_data/compare/`) are read too, though both are kept out of
    `fixture_names` so L1's every-fixture tests keep their list: a reviewer
    checks the GGUF ones, and the seed installs some of each, so Resume,
    Download again and a Test's memory must know them.
    """
    from framefound.local_models import fake

    pages: dict[str, dict[str, Any]] = {}
    contents: dict[tuple[str, str], bytes] = {}
    compare = sorted(f"compare/{path.stem}" for path in (fake.DEMO_DATA / "compare").glob("*.json"))
    for name in [*fake.fixture_names(), *fake.gguf_fixture_names(), *compare]:
        page = fake.fixture(name)
        pages[page["id"]] = page
        for path, body in fake.fixture_contents(name).items():
            contents[(page["id"], path)] = body
    return pages, contents


def _measured(facts: Mapping[str, Any]) -> float | None:
    """What the demo's model server reports a model uses: a little under the check's
    estimate from the real size of its weights. None for a model it does not know.

    For one build of a repository that holds several (GGUF), the weights are
    that build's own files — its model and its picture reader — as its
    folder's manifest lists them, never every build's together.
    """
    from framefound.local_models.preflight import estimate_memory_gb

    page = _pages()[0].get(str(facts.get("repo_id") or ""))
    if page is None:
        return None
    listed = facts.get("files")
    own = set(listed) if facts.get("variant") and isinstance(listed, list) else None
    weights = sum(
        int(entry.get("size") or 0)
        for entry in page.get("siblings", [])
        if str(entry.get("rfilename", "")).lower().endswith(WEIGHTS)
        and (own is None or entry.get("rfilename") in own)
    )
    if not weights:
        return None
    return round(estimate_memory_gb(weights) * MEASURED_SHARE, 1)


def _no_index(url: str, **_more: Any) -> Any:
    # The demo's software says its own newest version and asks nobody; a
    # question that reaches here anyway is answered as a network that is down.
    raise HubError("unreachable", UNREACHABLE)


def deps(settings: Settings, *, slow: bool = True) -> Deps:
    """The demo's outside world (`deps.deps_for` in demo mode).

    `slow=False` is the seed's: the same stand-ins without the pauses that
    make a download watchable.
    """
    if availability(settings).mode != "demo":
        raise RuntimeError(NOT_DEMO)
    from framefound.local_models import fake
    from framefound.local_models.ask import server_for
    from framefound.local_models.kinds import kinds

    pages, contents = _pages()
    pause = SECONDS_PER_CHUNK if slow else 0.0

    def hub_for(token: str | None) -> fake.FakeHub:
        hub = fake.FakeHub(pages, contents, token=token, chunk_bytes=CHUNK_BYTES)
        hub.small_files(SMALL_FILE_BYTES)
        if pause:
            hub.slow(pause)
        return hub

    server = server_for(settings)
    if isinstance(server, fake.FakeModelServer):
        # The one process-wide stand-in: the API and the lane share it, so a
        # model "loaded" by a Test is the one the page says is loaded.
        server.memory_for = _measured
        server.slow(ANSWER_SECONDS if slow else 0.0)
    return Deps(
        settings=settings,
        hub_for=hub_for,
        fetch_json=_no_index,
        server=server,
        kinds=kinds(settings),
        run=fake.FakeRunner(make_venvs=True),
        system=platform.system(),
        machine=platform.machine(),
    )


# --- the lane, inside the API --------------------------------------------------------

_LOCK = threading.Lock()
_POKE = threading.Event()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
# When a read of the page last started the lane (time.monotonic).
_READ_STARTED = float("-inf")


def wake_inline(settings: Settings) -> bool:
    """Run the work table now, inside this process: `wake()` in demo mode.

    True once the lane is running (it was, or it has been started); False
    outside demo mode, where nothing is started. The lane runs on its own
    thread, so this answers at once wherever it is called from — an API
    handler on the event loop, or one that wakes the lane from a thread.
    """
    if availability(settings).mode != "demo":
        return False
    with _LOCK:
        _POKE.set()
        if not _alive():
            _start(settings)
    return True


def keep_going(settings: Settings) -> None:
    """Start the lane if it is not running, at most once every TICK_S, without
    asking a running lane to look now.

    The page calls this when it is read in demo mode: after the API restarts
    nothing else would ever look at what the seed left waiting. A page polled
    every few seconds with nothing to do must not make the lane look every
    few seconds — each start is a thread and a connection to the database the
    page is reading — so between reads it looks no more often than beat would.
    New work never waits for this: whatever asks for it wakes the lane itself.
    """
    global _READ_STARTED
    if availability(settings).mode != "demo":
        return
    with _LOCK:
        moment = time.monotonic()
        if _alive() or moment - _READ_STARTED < TICK_S:
            return
        _READ_STARTED = moment
        _start(settings)


def _alive() -> bool:
    return _THREAD is not None and _THREAD.is_alive()


def _start(settings: Settings) -> None:
    """A new lane. Called holding _LOCK, with none running."""
    global _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(
        target=_lane, args=(settings,), name="local-ai-demo-lane", daemon=True
    )
    _THREAD.start()


def running() -> bool:
    """Whether the demo's lane is running in this process."""
    with _LOCK:
        return _alive()


def stop(timeout: float = 10.0) -> bool:
    """Stop the lane once the job under way has ended (tests). True when it has.

    The next read of the page may start it again at once: a stop is a fresh start.
    """
    global _READ_STARTED
    with _LOCK:
        thread = _THREAD
        _STOP.set()
        _POKE.set()
        _READ_STARTED = float("-inf")
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def _lane(settings: Settings) -> None:
    global _THREAD
    while True:
        _POKE.clear()
        try:
            waiting = asyncio.run(_drain(settings))
        except Exception:
            log.warning("local_ai.demo_lane_failed", exc_info=True)
            waiting = True
        with _LOCK:
            if _STOP.is_set() or (not waiting and not _POKE.is_set()):
                _THREAD = None
                return
        if waiting:
            # Woken early by wake_inline (new work), else after the beat's minute.
            _POKE.wait(TICK_S)
        with _LOCK:
            if _STOP.is_set():
                _THREAD = None
                return


async def _drain(settings: Settings) -> bool:
    """Run jobs until none is queued; whether any is still waiting or running."""
    from framefound.local_models import jobs

    # Its own engine: this thread's event loop is not the API's, and a pool
    # belongs to the loop that made it.
    engine = create_async_engine(settings.db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        while not _STOP.is_set() and await jobs.run_next(factory, deps(settings)):
            pass
        return await _active(factory)
    finally:
        await engine.dispose()


async def _active(factory: async_sessionmaker[AsyncSession]) -> bool:
    from framefound.local_models import jobs

    async with factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(LocalAiJob).where(LocalAiJob.state.in_(jobs.ACTIVE))
        )
    return bool(count)


# --- the seed -------------------------------------------------------------------------

# The models the seed adds (demo_data): the plan's first model, a text-only
# one that fits only overnight, and a large one whose download dropped.
LOOKER_MODEL = "mlx-community/gemma-4-e4b-it-4bit"
EDITOR_MODEL = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
DROPPED_MODEL = "mlx-community/demo-large-4bit"
# A GGUF build for the second program (L6 S7): a maker that publishes GGUF
# itself, so no converter has to be approved first. The seed takes the build
# the check suggests (Q4_K_M) and leaves its software uninstalled.
GGUF_MODEL = "google/demo-vision-gguf"
# The software: two versions installed, the newer in use, and a newer still
# "published", so Update and Roll back both have something to do.
SEED_VERSIONS = ("1.8", "1.9")
SEED_LATEST = "2.0"
ALREADY_SEEDED = "The demo's models are already in this catalogue; nothing was added."
WAITING_AGAIN = (
    "The demo's models are already in this catalogue; a test of the Editor is waiting for the "
    "overnight window again."
)
NO_WAIT = (
    "The demo's models are already in this catalogue. With the budgets and the window as they "
    "are now, a test of the Editor would not wait for the overnight window, so none was left "
    "waiting."
)
# A catalogue seeded before the GGUF build existed gets it on the next run.
# Its download is left queued: that run must not drive the lane (`_wait_again`).
GGUF_AGAIN = (
    f"The demo's other models are already in this catalogue; added the GGUF build {GGUF_MODEL}, "
    "whose program is not installed yet. Its download is waiting its turn: the review server "
    "does it when the AI models page is next opened."
)
WAITING_TOO = "A test of the Editor is waiting for the overnight window again."
# How much longer than the library's report interval the one piece of the
# dropping download's weights takes to arrive (see `seed`).
_DROP_MARGIN_S = 0.25


# What the seeds have added to this catalogue once: one app_settings row,
# {"<part>": true}. A part named here is never added again, so a model a
# reviewer removed, a look a reviewer cleared, or a cut a reviewer deleted
# stays gone when the review catalogue is seeded again. The script's own
# parts (infrastructure/scripts/seed_dev.py: the colour pages, the cuts, the
# motion files a folder review needs of the seeded clips) are kept here too.
SEEDED_KEY = "demo_seed"
GGUF_PART = "gguf"


async def seeded_parts(db: AsyncSession) -> frozenset[str]:
    """The parts the seeds have added to this catalogue before (`note_seeded`)."""
    row = await db.get(AppSetting, SEEDED_KEY, populate_existing=True)
    return frozenset(name for name, done in ((row.value if row else None) or {}).items() if done)


async def note_seeded(db: AsyncSession, *parts: str) -> None:
    """Remember that these parts are in the catalogue, so no later seed adds
    them again. Flushed; the caller commits it with the rows it stands for."""
    row = await db.get(AppSetting, SEEDED_KEY, populate_existing=True)
    if row is None:
        db.add(AppSetting(key=SEEDED_KEY, value=dict.fromkeys(parts, True)))
    else:
        # A new dict: a JSON column does not see a change made in place.
        row.value = {**(row.value or {}), **dict.fromkeys(parts, True)}
    await db.flush()


def know_every_kind() -> None:
    """Register every kind of job the lane runs, as the worker and the API have.

    For a process of its own that is about to run the lane (the seed, the
    script's Compare part). `jobs.run_next` ends a queued job whose kind it
    does not know as failed, "This version of FrameFound does not know how to
    do this.", and on a review catalogue that job is a reviewer's: a draft
    waiting for the night, a look, a review of a folder. Loading the handlers
    of this package alone left all of those unknown.
    """
    from framefound.processing import local_ai_tasks  # noqa: F401 - loads every handler module


async def seed(
    factory: async_sessionmaker[AsyncSession], settings: Settings, *, now: datetime | None = None
) -> str:
    """Fill the AI models page of a review catalogue, as if it had been used. A sentence.

    Run once, on a catalogue the seed has already filled with photographs
    (the finished Test looks at the newest one). The rows are made by the
    real handlers with the demo's stand-ins, on the real clock, so each
    reads exactly as the page will later make it read and every time on
    it has passed. The one exception is the Test waiting for the overnight
    window (`_wait_for_the_window`).

    Run again on a catalogue it has filled, it runs nothing: jobs a reviewer
    asked for are waiting there, and the review server's own lane does them.
    It adds no model but the GGUF build, once, to a catalogue filled before
    that build existed, and leaves its download queued for that lane; it puts
    a waiting Test of the Editor back if none is waiting any more (the window
    opened and the demo's lane ran it).

    `now` is this machine's wall clock (naive), read for the overnight window
    only: the waiting Test is made as if asked for in the daytime.
    """
    if availability(settings).mode != "demo":
        raise RuntimeError(NOT_DEMO)
    from framefound.local_models import fake as fakes
    from framefound.local_models import jobs, library, preflight, registry
    from framefound.local_models.settings_store import load_config, save_latest

    # Before a job is asked for (only a known kind can be), and before the
    # lane is run from here on an empty catalogue.
    know_every_kind()
    wall = now or datetime.now()
    async with factory() as db:
        if await db.scalar(select(func.count()).select_from(LocalModel)):
            return await _wait_again(factory, settings, wall)

    made = deps(settings, slow=False)

    async def run(job_kind: str, **fields: Any) -> LocalAiJob:
        async with factory() as db:
            job = await jobs.create(db, job_kind, **fields)
            await db.commit()
        await jobs.run_next(factory, made)
        async with factory() as db:
            done = await db.get(LocalAiJob, job.id)
            assert done is not None
            return done

    # The software that runs models.
    for version in SEED_VERSIONS:
        await run(
            "runtime_install",
            subject=f"{fakes.DemoKind.label} {version}",
            payload={"name": fakes.DemoKind.name, "version": version},
        )
    async with factory() as db:
        await save_latest(db, fakes.DemoKind.name, SEED_LATEST, None, now=datetime.now(UTC))

    async def add(repo_id: str) -> LocalModel:
        async with factory() as db:
            config = await load_config(db)
            found = preflight.preflight(
                made.hub_for(None),
                repo_id,
                config=config,
                kinds=made.kinds,
                installed={fakes.DemoKind.name: SEED_VERSIONS[-1]},
                free_bytes=None,
                min_free_bytes=0,
            )
            model = await registry.add_model(db, found, None)
            await db.commit()
            return model

    async def download(model: LocalModel, deps_used: Deps = made) -> LocalAiJob:
        async with factory() as db:
            job = await jobs.create(db, "download", subject=_subject(model), model_id=model.id)
            await db.commit()
        await jobs.run_next(factory, deps_used)
        async with factory() as db:
            done = await db.get(LocalAiJob, job.id)
            assert done is not None
            return done

    # The Looker: installed, given the role, tested on the newest photograph.
    # It fits the daytime budget, so the real clock runs it at any hour.
    looker = await add(LOOKER_MODEL)
    await download(looker)
    # The Editor: text only, and too big for the daytime budget.
    editor = await add(EDITOR_MODEL)
    await download(editor)
    async with factory() as db:
        for role, model in (("looker", looker), ("editor", editor)):
            await registry.set_role(db, role, model, pending=False, user_id=None)
        await db.commit()
    await run("test", subject=_subject(looker), model_id=looker.id, priority=0)

    # A GGUF build for the second program, which is left uninstalled.
    gguf = await _add_gguf(factory, made)
    if gguf is not None:
        await jobs.run_next(factory, made)

    # A download whose connection dropped half-way through the weights. The
    # sentence says how far it got by the library's last report, and the
    # library reports at most every REPORT_EVERY_S. So the half arrives in one
    # piece, after a pause longer than that: it is always reported, whatever
    # else this machine was doing, and the sentence always says "at 7.8 of
    # 15.6 GB" — never an earlier figure that happened to be reported last.
    pause = library.REPORT_EVERY_S + _DROP_MARGIN_S

    class Drops(fakes.FakeHub):
        def fetch(self, repo_id: str, revision: str, path: str, target: Path, **more: Any) -> None:
            weights = path.lower().endswith(WEIGHTS)
            self.slow(pause if weights else 0.0)
            if weights:
                self.fail_after(SMALL_FILE_BYTES // 2)
            super().fetch(repo_id, revision, path, target, **more)

    dropped = await add(DROPPED_MODEL)
    pages, contents = _pages()
    hub = Drops(pages, contents, chunk_bytes=SMALL_FILE_BYTES)
    hub.small_files(SMALL_FILE_BYTES)
    await download(dropped, replace(made, hub_for=hub.with_token))

    # A Test of the Editor, waiting for the overnight window to open.
    waits = await _wait_for_the_window(factory, editor, wall)
    editor_is = "waiting for the overnight window" if waits else "not tested"

    # Said as it is: a catalogue whose own lists refuse the build has three.
    build = "a GGUF build whose program is not installed yet, " if gguf is not None else ""
    return (
        f"Added {4 if gguf is not None else 3} demo models (the Looker tested, the Editor "
        f"{editor_is}, {build}one download that dropped) and the demo's software, "
        f"versions {' and '.join(SEED_VERSIONS)}."
    )


async def _add_gguf(factory: async_sessionmaker[AsyncSession], made: Deps) -> LocalModel | None:
    """Add the demo's GGUF build (`GGUF_MODEL`, the build the check suggests) and
    queue its download. The lane is not run here: the caller knows whether it may.

    For the second program (L6 S7): its software is left uninstalled, so the
    row says it is needed and a reviewer installs it from the page. It is
    given no role: a model on llama.cpp that nobody compared is asked about
    first, and the seed would have to answer for the reviewer
    (`skip_compare`). None when the catalogue's own rules refuse it (a
    reviewer may have blocked its publisher); nothing is overruled here, and
    it is tried again at the next seed.
    """
    from framefound.local_models import jobs, preflight, registry
    from framefound.local_models.settings_store import load_config

    async with factory() as db:
        config = await load_config(db)
        found = preflight.preflight(
            made.hub_for(None),
            GGUF_MODEL,
            config=config,
            kinds=made.kinds,
            installed={},
            free_bytes=None,
            min_free_bytes=0,
        )
        try:
            model = await registry.add_model(db, found, None)
        except registry.Refusal:
            return None
        await jobs.create(db, "download", subject=_subject(model), model_id=model.id)
        # With the row: a reviewer who removes the build later keeps it removed.
        await note_seeded(db, GGUF_PART)
        await db.commit()
    return model


async def _wait_again(
    factory: async_sessionmaker[AsyncSession], settings: Settings, wall: datetime
) -> str:
    """A catalogue the seed has filled: the GGUF build if no seed has added it
    here before, and a waiting Test of the Editor if none is left.

    Nothing is run. The catalogue is a reviewer's by now, and the lane takes
    the most urgent job first whoever asked for it: run from here, a
    reviewer's look or draft would be done in this process, beside the review
    server's own lane, or ended as failed where this process does not know
    its kind. So the build's download is left queued for that lane, which
    starts when the AI models page is read (`keep_going`).

    The build is added once (`GGUF_PART`). Without the mark, "seeded before
    the build existed" and "a reviewer removed it" are the same catalogue,
    and Remove would be undone by the next seed.
    """
    from framefound.local_models import jobs

    async with factory() as db:
        editor = await db.scalar(select(LocalModel).where(LocalModel.repo_id == EDITOR_MODEL))
        if editor is None:
            return ALREADY_SEEDED  # somebody's own models: nothing of the demo's to refresh
        has_gguf = await db.scalar(
            select(func.count())
            .select_from(LocalModel)
            .where(LocalModel.repo_key == GGUF_MODEL.casefold())
        )
        active = await db.scalar(
            select(func.count())
            .select_from(LocalAiJob)
            .where(
                LocalAiJob.kind == "test",
                LocalAiJob.subject == _subject(editor),
                LocalAiJob.state.in_(jobs.ACTIVE),
            )
        )
        seen = GGUF_PART in await seeded_parts(db)
        if has_gguf and not seen:
            # Filled by a seed from before the mark was kept: kept from now on.
            await note_seeded(db, GGUF_PART)
            await db.commit()
    gguf = None
    if not has_gguf and not seen:
        gguf = await _add_gguf(factory, deps(settings, slow=False))
    waits = not active and await _wait_for_the_window(factory, editor, wall)
    if gguf is None:
        if active:
            return ALREADY_SEEDED
        return WAITING_AGAIN if waits else NO_WAIT
    return f"{GGUF_AGAIN} {WAITING_TOO}" if waits else GGUF_AGAIN


async def _wait_for_the_window(
    factory: async_sessionmaker[AsyncSession], editor: LocalModel, wall: datetime
) -> bool:
    """Ask for a Test of the Editor that waits for the overnight window. Whether one waits.

    Written as the runner writes a Test the window holds back (`jobs._run`
    on `NotNow("window")`: waiting, the scheduler's sentence, its time), with
    the decision the Test's own `ask` makes, taken at a made-up daytime hour.
    It is not run: run, it would be stamped as started at that made-up hour,
    which after 20:00 is tomorrow's. Not "asked for later" either, so a
    change to the budgets or the window releases it as it would the real one.
    """
    from framefound.local_models import jobs, registry, schedule
    from framefound.local_models.settings_store import load_config

    async with factory() as db:
        config = await load_config(db)
        decision = schedule.decide(registry.memory_gb(editor), _daytime(wall), config.budgets())
        if decision.why != "window" or decision.not_before is None:
            return False
        job = await jobs.create(
            db, "test", subject=_subject(editor), model_id=editor.id, priority=0
        )
        job.state = "waiting"
        job.message = decision.message
        job.not_before = decision.not_before.astimezone(UTC)
        await db.commit()
    return True


def _subject(model: LocalModel) -> str:
    # The API's subject_of: `publisher/name@<8>[/variant]`, which job titles read.
    variant = f"/{model.variant}" if model.variant else ""
    return f"{model.repo_id}@{model.revision[:8]}{variant}"


def _daytime(wall: datetime) -> datetime:
    """Noon of the day the seed runs, or of the next day once the evening has come,
    so a job that waits for the overnight window has a window still to wait for."""
    noon = wall.replace(hour=12, minute=0, second=0, microsecond=0)
    return noon if wall.hour < 20 else noon + timedelta(days=1)
