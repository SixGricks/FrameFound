"""`framefound storage`: where each bulky kind of file lives, and moving one.

    framefound install --store "/Volumes/Photos/Framefound"
    framefound storage
    framefound storage move renders "/Volumes/Fast SSD/FrameFound renders"

The Storage page does all of this without a console; these are the same
functions (storage/moves.py) for an install script, or a day the page cannot
be reached. Unlike `framefound proxies move` nothing is stopped or restarted:
where a kind lives is recorded in the data folder, and every running service
follows it at its next lookup.
"""

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.config import Settings
from framefound.native import envfile
from framefound.native.layout import Layout
from framefound.storage import move_job, moves, stores

Say = Callable[[str], None]


@dataclass(frozen=True)
class Asked:
    """What only the catalogue can say about a folder and a moment."""

    # Files the catalogue lists as footage inside the folder.
    catalogued: int
    # The kind a move started from the Storage page is moving right now.
    moving: str | None


Ask = Callable[[Settings, Path], Asked]
# The models that have a role (or are waiting for one), read when a move of
# the model library is about to switch.
Roles = Callable[[Settings], Iterable[Any]]


def role_models(settings: Settings) -> list[Any]:
    """Every model with a role, from the database (`registry.role_models`)."""
    from framefound.local_models import registry

    async def ask() -> list[Any]:
        engine = create_async_engine(settings.db_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                return list(await registry.role_models(db))
        finally:
            await engine.dispose()

    try:
        return asyncio.run(ask())
    except Exception as err:
        raise moves.MoveError(
            f"Which models have a role could not be read ({err}). Nothing was changed. "
            "Start FrameFound (framefound start), then try again."
        ) from err


def ask_catalogue(settings: Settings, folder: Path) -> Asked:
    """One question to the database before anything is marked or moved.

    A store's marker keeps the scanner out of its folder, so marking a
    folder that holds catalogued footage would report all of it missing; and
    two moves at once, one from the page and one from here, would each lift
    the pause the other is relying on."""

    async def ask() -> Asked:
        engine = create_async_engine(settings.db_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                state = await move_job.load(db)
                return Asked(
                    catalogued=await moves.catalogued_under(db, folder, settings),
                    moving=state.kind if state is not None and move_job.is_active(state) else None,
                )
        finally:
            await engine.dispose()

    try:
        return asyncio.run(ask())
    except Exception as err:
        raise moves.MoveError(
            f"The catalogue could not be asked whether {folder} holds footage ({err}). "
            "Start FrameFound (framefound start), then try again."
        ) from err


def _refuse_while_moving(asked: Asked) -> None:
    """A move the Storage page has asked for, even one still waiting for a
    worker. (One that is running, from the page or from another terminal,
    is also refused by `moves.move` itself: it holds the turn.)"""
    if asked.moving is not None:
        raise moves.MoveError(
            f"{stores.words(asked.moving).things.capitalize()} are being moved from the "
            "Storage page right now. Wait for that to finish, or cancel it there."
        )


def use_bulk_folder(
    layout: Layout,
    settings: Settings,
    root: Path,
    say: Say,
    ask: Ask | None = None,
    sure: Callable[[str], bool] = lambda _question: False,
) -> None:
    """`framefound install --store`: make `root` the one folder for
    everything bulky. Marks it, makes a folder in it for each kind and
    records it; moves nothing.

    A folder that looks like a share that is not connected — on this Mac's
    own disk, or the bulk folder already but without its marker — is asked
    about first (`sure`), and nothing is made in it until the answer is yes.
    Marked without asking, every kind in it would read as connected and fill
    the Mac's disk, with the files on the real share reported missing."""
    root = moves.tidy(root)
    asked = (ask or ask_catalogue)(settings, root)
    _refuse_while_moving(asked)
    moves.validate_bulk_root(root, settings, catalogued=asked.catalogued, create=False)
    question = moves.bulk_question(root, settings)
    if question is not None and not sure(str(question)):
        raise moves.MoveError(
            f"{question.why} Nothing was changed. (To use that folder anyway, add --yes.)"
        )
    staying = moves.adopt_bulk_root(
        root, settings=settings, catalogued=asked.catalogued, sure=question is not None
    )
    # The record in the data folder is what the running services follow.
    # The setting says the same to anyone reading the file, and to a start
    # from a data folder that has lost its record.
    envfile.update(layout.env_file, {"FRAMEFOUND_BULK_DIR": str(root)})
    say(f"Bulk storage is in {root}")
    for kind in staying:
        things = stores.words(kind).things
        say(
            f"{things.capitalize()} stay in {stores.kind_folder(kind, settings)} for now "
            f"(there are files there). Bring them over with:\n"
            f'  framefound storage move {kind} "{root / kind}"'
        )


_size = moves.size_text


def describe(settings: Settings) -> list[str]:
    """One line per kind: where it lives, whether that is there, how much."""
    bulk = stores.bulk_root(settings)
    lines = [
        f"Bulk folder: {bulk}"
        if bulk is not None
        else "Bulk folder: none — everything is on this Mac "
        "(set one with: framefound install --store <folder>)"
    ]
    if settings.bulk_dir is not None and bulk is not None and settings.bulk_dir != bulk:
        # The settings file was edited by hand, and nothing followed.
        lines.append(
            f"  (framefound.env says FRAMEFOUND_BULK_DIR={settings.bulk_dir}. That line is "
            "not what is followed: the folder above was recorded when it was set. To "
            "change it: framefound install --store <folder>, or the Storage page.)"
        )
    previous = stores.read_state(settings.data_dir).previous
    for kind in stores.KINDS:
        root = stores.kind_root(kind, settings)
        if root is None:
            lines.append(f"{kind:<9} no copy is kept off this Mac")
            continue
        problem = stores.kind_problem(kind, settings)
        try:
            if problem is not None:
                note = f"NOT CONNECTED: {problem}"
            else:
                files, size = moves.usage(kind, root)
                note = f"{files:,} files, {_size(size)}"
                if kind == "models":
                    # Counted in its `library` folder only: the ONNX and
                    # Whisper caches beside it are not models anybody added.
                    note += (
                        " in the model library (the caches FrameFound uses itself stay in "
                        f"{settings.models_dir})"
                    )
        except OSError as err:
            note = f"cannot be read ({err.strerror or err})"
        lines.append(f"{kind:<9} {root}   {note}")
        old = previous.get(kind)
        if old is not None and old != root:
            lines.append(f"{'':<9} old copies are still in {old} — remove them on the Storage page")
    return lines


def move(
    settings: Settings,
    kind: str,
    target: Path,
    say: Say,
    *,
    empty: bool = False,
    sure: Callable[[str], bool] = lambda _question: False,
    ask: Ask | None = None,
    roles: Roles | None = None,
    **options: Any,
) -> None:
    """`framefound storage move <kind> <folder>`: copy, check, switch.
    `options` go to `moves.move` (tests shorten its waits). For the model
    library, every model with a role is given its copy on this machine just
    before the switch (`move_job.keep_roles_here`); `roles` says which."""
    if kind not in stores.KINDS:
        raise moves.MoveError(
            f"{kind!r} is not a kind of storage. The kinds are: {', '.join(moves.MOVABLE)}"
        )
    words = stores.words(kind)
    # `..` and the like worked out before anything is compared or recorded.
    target = moves.tidy(target)
    asked = (ask or ask_catalogue)(settings, target)
    _refuse_while_moving(asked)
    # Looked at before the question below, so a folder that cannot be used
    # at all is refused for its real reason, and before anything is made in
    # it, so answering no leaves nothing behind.
    found = moves.preflight(
        kind,
        target,
        settings=settings,
        mode="empty" if empty else "copy",
        catalogued=asked.catalogued,
    )
    if found.question is not None and not sure(str(found.question)):
        raise moves.MoveError("Nothing was changed. (To use that folder anyway, add --yes.)")

    said_phase, said_at = "", 0.0

    def report(progress: moves.Progress) -> None:
        # A line when the step changes, and every few seconds while copying:
        # a terminal left open overnight should not scroll for ever.
        nonlocal said_phase, said_at
        now = time.monotonic()
        changed = progress.phase != said_phase
        if not changed and (progress.phase != "copying" or now - said_at < 5):
            return
        said_phase, said_at = progress.phase, now
        if progress.phase in ("copying", "finishing") and progress.files_total:
            say(
                f"  {progress.phase}: {progress.files_done:,} of {progress.files_total:,} files, "
                f"{_size(progress.bytes_done)} of {_size(progress.bytes_total)}"
            )
        elif changed and progress.phase != "done":
            say(f"  {progress.phase}")

    if kind == "models":

        def keep_roles() -> None:
            move_job.keep_roles_here((roles or role_models)(settings), settings)

        options.setdefault("before_switch", keep_roles)

    result = moves.move(
        kind,
        target,
        settings=settings,
        mode="empty" if empty else "copy",
        catalogued=asked.catalogued,
        report=report,
        **options,
    )
    say(f"{words.things.capitalize()} now live in {target} ({result.files:,} files)")
    old = stores.read_state(settings.data_dir).previous.get(kind)
    if old is not None and old != target:
        say(
            f"The old copies are still in {old}. Once everything looks right, free the "
            "space on the Storage page (Remove old copies)."
        )
