"""A clip's colour look: the API (design L4 section 6, the video half).

A clip is offered three looks on the same still, side by side and always in
the same order: what the local model suggested, the learned look, and no
change. The operator picks one by eye (or sets the sliders by hand) and
takes it to Premiere as a `.cube` LUT, one clip at a time or a folder's
worth in one zip.

What a look is, and the looks that need no model, live in
`media/clip_look.py`; the table and the file in `media/lut.py`. This module
only gathers what a clip has and answers with it:

- Every picture is the still read back through the table that would be
  exported (`lut.apply`), never through the engine directly: the operator
  judges what the file will do. "No change" is the still itself.
- Nothing is ever chosen for anybody. A suggestion (`kind="suggested"`) is
  written by the local-AI lane's job only; the `chosen` row by a person's
  press here only. Two writers, two rows, neither overwriting the other.
- No answer holds a colour difference, a score or a rank. A look is judged
  by eye; a figure beside a picture gets read as a grade.
- Choosing and downloading need no model, so they work on the container
  server too. Only asking the model is refused there (the suggest routes),
  and the page is told why up front.
- Asking is a job on the local-AI lane (`processing/colour_jobs.py`): Suggest
  queues it and answers at once, and the page reads how it is getting on
  from `suggest.job`. A model that only fits overnight is not refused; the
  job waits and says until when.

Reading and writing are any signed-in user's, as the editor's are (a
read-only account is refused every non-GET by `auth/deps.py`). Nothing here
is an administrator's setting, so nothing is audited; a choice is logged.
No answer carries a server path: a folder is a path inside its library, as
Browse shows it. Every refusal is the sentence the page shows, in the one
error envelope: 400 the request cannot be served as asked, 404 no such
clip or look, 409 not possible as things stand. Stills, tables, pictures and
zips are worked in a thread.
"""

import asyncio
import hashlib
import io
import json
import time
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import structlog
from fastapi import APIRouter, HTTPException, Query, Response
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.api.v1.local_models import QUEUE_DOWN, DepsDep
from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import (
    Asset,
    ClipLook,
    Frame,
    Library,
    LocalAiJob,
    ShotWindow,
    User,
    catalogued,
)
from framefound.local_models import availability, jobs, registry
from framefound.media import clip_look, develop, folders, looks, lut

# Registers the `clip_look` and `listing_look` kinds in the API's process, so
# it can queue them and name them from its first request. The worker that
# RUNS them registers them only through `processing/local_ai_tasks.py`'s own
# import of colour_jobs (another team's file: its line is added by the
# orchestrator, and test_colour_jobs.py pins it).
from framefound.processing import colour_jobs, local_ai_tasks, shots

log = structlog.get_logger()
router = APIRouter(prefix="/clip-looks", tags=["clip-looks"])

# The two kinds of row a clip may have (ClipLook.kind).
SUGGESTED, CHOSEN = "suggested", "chosen"
# Learned looks kept, by clip, still and what they were learned from. Working
# one out from the photograph look fits sliders on the still (a second or
# more); the page asks for the clip once on opening and after every press.
LEARNED_KEPT = 64
# The most clips a folder line names, and the most looks one zip holds.
FOLDER_LISTED = 200
ZIP_MOST = 200
# The most looks one zip holds at the finer size (65 points a side is about
# 7 MB of text a clip before compression).
ZIP_MOST_FINE = 40
JPEG_QUALITY = 90
# How long a moment that showed something other than its own picture (a
# grab from the proxy failed) is answered from memory, and how many are kept.
MOMENT_REMEMBERED_S = 600.0
MOMENTS_KEPT = 1024

NO_SUCH_ASSET = "No such asset"
NOT_VIDEO = "Looks are for video. A photograph is edited in the editor."
NO_STILL = (
    "There is no still of this clip to judge a look on yet. It appears once the clip has "
    "been processed."
)
CANNOT_SUGGEST_HERE = (
    "Suggestions come from a model running on the Mac install of FrameFound. This server "
    "cannot run one. The learned look and the LUT download work here as they are."
)
NO_LOOKER = "No model has the Looker role yet. Give one the role on the AI models page."
NOT_SUGGESTED_YET = "The local model has not suggested a look for this clip yet."
NO_SUGGESTION = "There is no model suggestion for this clip."
NOTHING_CHOSEN = "No look has been chosen for this clip."
NO_LEARNED = "There is no learned look yet."
NO_SUGGESTION_TO_USE = "There is no model suggestion to use."
NO_LEARNED_TO_USE = "There is no learned look to use."
SEND_SLIDERS = "Send the sliders with a look of your own."
NO_LUT_NEEDED = "The clip as it was shot needs no LUT."
BAD_SIZE = "Choose the standard size (33) or the finer one (65)."
BAD_PREVIEW_SOURCE = "Say which look to show: model, learned, none or chosen."
BAD_CUBE_SOURCE = "Say which look to download: chosen, model or learned."
FOLDER_TOO_FINE = "Choose the standard size for a whole folder."
NONE_CHOSEN_IN_FOLDER = "No clip in this folder has a chosen look yet."
ON_ITS_WAY = "A suggestion for this clip is already on its way."
NOTHING_WAITING = "Nothing is waiting for this clip."
NOT_A_MOMENT = "Choose one of the moments listed for this clip."
# How urgent a press of Suggest is, and a whole folder's worth: one clip the
# operator is looking at goes before a folder being worked through.
SUGGEST_PRIORITY = 0
FOLDER_PRIORITY = 5

# What the file is for, in the card's words ("Using the LUT in Premiere").
READ_ME = (
    "Looks from FrameFound\n"
    "\n"
    "Each .cube file is the look chosen in FrameFound for the clip of the same name.\n"
    "The file changes colour only: it does what the picture on the clip's page shows\n"
    "to every frame of the clip.\n"
    "\n"
    "In Premiere, select the clip, open Lumetri Color, and load the file under\n"
    "Creative > Look > Browse (or Basic Correction > Input LUT > Browse).\n"
    "\n"
    f"{lut.ASSUMES}\n"
    "If the clip looks different in Premiere than it does in FrameFound before any\n"
    "LUT (log or HDR footage that Premiere converts), the look will land differently.\n"
    "A LUT cannot straighten, lift shadows region by region or replace a sky; those\n"
    "stay in the photograph editor.\n"
)
# The words each look is called by in a file's header.
_LOOK_WORDS = {
    "model": "the local model's suggestion",
    "learned": "the learned look",
    "none": "no change",
    "custom": "set by hand",
}


# --- what the page is sent --------------------------------------------------------------


class _Out(BaseModel):
    # Several fields are called model_..., which is what they are.
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)


class StillOut(_Out):
    ts_ms: int
    # "shot": a shot's middle still; "frame": a sampled frame (a clip with no shots).
    from_: Literal["shot", "frame"] = Field(alias="from")


class ProvenanceOut(_Out):
    """Which build of which model answered, as text."""

    model: str | None
    model_revision: str | None
    variant: str
    runtime: str | None
    runtime_version: str | None
    prompt_version: str | None


class CandidateOut(_Out):
    source: Literal["model", "learned", "none"]
    available: bool
    # Why it is not available; or, for the learned look, where it comes from.
    note: str | None
    # The sliders, as the editor names them (exposure in stops, the rest -1..1).
    recipe: dict[str, float]
    in_words: str
    # The model's one line; "" for the others.
    reason: str
    # What the look wanted and a LUT cannot carry, in words ready to print.
    left_out: list[str]
    # The still its white balance and levels were measured on.
    measured_at_ms: int | None
    basis: Literal["clips", "photos"] | None
    # How many chosen clips or shipped photographs it was learned from.
    examples: int
    provenance: ProvenanceOut | None
    made_at: str | None
    demo: bool
    # Changes whenever the look does: put it in a preview's address.
    version: str


class ChosenOut(_Out):
    source: Literal["model", "learned", "none", "custom"]
    recipe: dict[str, float]
    in_words: str
    left_out: list[str]
    measured_at_ms: int | None
    chosen_at: str | None
    chosen_by: str | None
    provenance: ProvenanceOut | None
    demo: bool
    version: str


class JobOut(_Out):
    """A request for a suggestion, as the page shows it (`colour_jobs.job_out`)."""

    id: uuid.UUID
    state: Literal["queued", "waiting", "running", "done", "failed", "cancelled"]
    # L1's sentence: "Waiting for the overnight window (starts 21:30)…", "Local
    # AI is paused.", why it failed.
    message: str
    # An administrator's only (a path on the server, what the model wrote).
    detail: str | None
    not_before: str | None
    done: int
    total: int
    cancelling: bool
    can_cancel: bool


class SuggestOut(_Out):
    mode: Literal["ready", "demo", "unavailable"]
    # False when Suggest cannot be pressed; `note` says why.
    ok: bool
    note: str | None
    # The newest clip_look job while it is active, or failed and newer than
    # the suggestion; else null.
    job: JobOut | None


class QueuedOut(_Out):
    job: JobOut


class FolderQueuedOut(_Out):
    # Clips a suggestion was asked for now.
    queued: int
    # Clips left alone: they have a suggestion or a choice, or one is on its way.
    already: int
    # Clips with no shots and no sampled frames: nothing to show a model yet.
    no_still: int


class FolderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: uuid.UUID


class FolderClipOut(_Out):
    asset_id: uuid.UUID
    filename: str
    state: Literal["chosen", "suggested", "none"]


class FolderOut(_Out):
    # Inside its library, as Browse shows it; "" for the top of the library.
    path: str
    clips: int
    chosen: int
    suggested: int
    others: list[FolderClipOut]


class LutOut(_Out):
    sizes: list[int]
    default: int
    assumes: str


class ClipLooks(_Out):
    asset_id: uuid.UUID
    filename: str
    problem: str | None
    stills: list[StillOut]
    reference_ts_ms: int | None
    candidates: list[CandidateOut]
    chosen: ChosenOut | None
    suggest: SuggestOut
    folder: FolderOut
    lut: LutOut


class LookIn(BaseModel):
    """Sliders a person set: the eight a LUT carries, each optional. The
    ranges are the engine's (the editor's `RecipeIn`); shadows only darken."""

    model_config = ConfigDict(extra="forbid")

    exposure: float = Field(default=0.0, ge=-2.0, le=2.0)
    contrast: float = Field(default=0.0, ge=-1.0, le=1.0)
    temperature: float = Field(default=0.0, ge=-1.0, le=1.0)
    tint: float = Field(default=0.0, ge=-1.0, le=1.0)
    saturation: float = Field(default=0.0, ge=-1.0, le=1.0)
    vibrance: float = Field(default=0.0, ge=-1.0, le=1.0)
    shadows: float = Field(default=0.0, ge=-1.0, le=0.0)
    auto_wb: float = Field(default=0.0, ge=0.0, le=1.0)


class ChoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["model", "learned", "none", "custom"]
    # Read only for `custom`.
    recipe: LookIn | None = None


# --- what a clip has -----------------------------------------------------------------


@dataclass
class _Clip:
    asset: Asset
    data_dir: Path
    stills: list[tuple[int, str]]
    # The moment the reference still really shows, or None with no picture.
    reference_ms: int | None
    still: Image.Image | None
    problem: str | None


@dataclass(frozen=True)
class _Look:
    """One look as the API handles it: what is shown, built and stored."""

    source: str
    available: bool
    recipe: dict[str, Any]
    measured: dict[str, Any]
    left_out: tuple[str, ...] = ()
    measured_at_ms: int | None = None
    note: str | None = None
    basis: Literal["clips", "photos"] | None = None
    examples: int = 0
    reason: str = ""
    provenance: dict[str, Any] | None = None
    made_at: datetime | None = None
    demo: bool = False
    features: list[float] = field(default_factory=list)

    @property
    def version(self) -> str:
        return clip_look.version_of(self.recipe, self.measured)


# What a moment that did not show its own picture showed instead (a sampled
# frame near it, or nothing: -1), with when that was found.
_moved: OrderedDict[tuple[uuid.UUID, int], tuple[int, float]] = OrderedDict()


async def _still_at(
    db: AsyncSession, asset: Asset, ts_ms: int, data_dir: Path
) -> tuple[Image.Image, int]:
    """`shots.still_at`, without asking the proxy about the same moment again
    and again.

    A moment with no saved still is grabbed from the proxy: an ffmpeg run on
    the proxy store, which may be the NAS. A grab that fails keeps nothing,
    and the page asks for its clip every few seconds while a suggestion is
    on its way, so every poll would run it again. What such a moment showed
    instead is remembered for MOMENT_REMEMBERED_S, and that is asked for."""
    key = (asset.id, int(ts_ms))
    known = _moved.get(key)
    if known is not None and time.monotonic() - known[1] < MOMENT_REMEMBERED_S:
        _moved.move_to_end(key)
        if known[0] < 0:
            return Image.new("RGB", shots.BLANK_SIZE, shots.BLANK_GREY), -1
        return await shots.still_at(db, asset, known[0], data_dir=data_dir)
    picture, shown = await shots.still_at(db, asset, ts_ms, data_dir=data_dir)
    if shown == ts_ms:
        _moved.pop(key, None)
    else:
        _moved[key] = (shown, time.monotonic())
        _moved.move_to_end(key)
        while len(_moved) > MOMENTS_KEPT:
            _moved.popitem(last=False)
    return picture, shown


async def _video(db: AsyncSession, asset_id: uuid.UUID) -> Asset:
    asset = await db.get(Asset, asset_id)
    # A paired editing proxy is out of the catalogue: it has no page of its own.
    if asset is None or asset.proxy_of_id is not None:
        raise HTTPException(404, NO_SUCH_ASSET)
    if asset.media_type != "video":
        raise HTTPException(400, NOT_VIDEO)
    return asset


async def _clip(db: AsyncSession, asset_id: uuid.UUID, data_dir: Path) -> _Clip:
    """The clip, the moments it can be looked at on, and its reference still.

    The reference is the middle of the clip's longest shot (the sampled frame
    nearest its middle without shots); its white balance is the one every
    look of this clip is measured on. A clip with neither shots nor frames,
    or whose reference has no readable picture, has `problem` set."""
    asset = await _video(db, asset_id)
    windows = [
        (int(start), int(end))
        for start, end in (
            await db.execute(
                select(ShotWindow.start_ms, ShotWindow.end_ms)
                .where(ShotWindow.asset_id == asset.id)
                .order_by(ShotWindow.start_ms)
            )
        ).all()
    ]
    frames = [
        int(ts) for ts in await db.scalars(select(Frame.ts_ms).where(Frame.asset_id == asset.id))
    ]
    duration_ms = int(asset.duration_s * 1000) if asset.duration_s else None
    reference = clip_look.reference_moment(windows, frames, duration_ms)
    if reference is None:
        return _Clip(asset, data_dir, [], None, None, NO_STILL)
    still, shown = await _still_at(db, asset, reference, data_dir)
    if shown < 0:
        return _Clip(asset, data_dir, [], None, None, NO_STILL)
    stills = clip_look.moments(windows, frames, keep=reference)
    if shown != reference:
        # The picture found is a sampled frame near the moment asked for; the
        # page labels and asks for the moment the picture really shows.
        stills = sorted({*stills, (shown, "frame")} - {(reference, "shot"), (reference, "frame")})
    return _Clip(asset, data_dir, stills, shown, still, None)


async def _picture_at(db: AsyncSession, clip: _Clip, ts_ms: int | None) -> tuple[Image.Image, int]:
    """The still at `ts_ms` (the reference when None). 409 when there is none;
    400 for a moment the clip does not list: every other moment would be a
    grab from the proxy and a new file in the data folder, for anybody who
    asks."""
    if clip.problem is not None or clip.still is None or clip.reference_ms is None:
        raise HTTPException(409, NO_STILL)
    if ts_ms is None or ts_ms == clip.reference_ms:
        return clip.still, clip.reference_ms
    if ts_ms not in {ts for ts, _source in clip.stills}:
        raise HTTPException(400, NOT_A_MOMENT)
    picture, shown = await _still_at(db, clip.asset, ts_ms, clip.data_dir)
    if shown < 0:
        raise HTTPException(409, NO_STILL)
    return picture, shown


async def _rows(db: AsyncSession, asset_id: uuid.UUID) -> dict[str, ClipLook]:
    rows = await db.scalars(
        select(ClipLook)
        .where(ClipLook.asset_id == asset_id)
        .execution_options(populate_existing=True)
    )
    return {row.kind: row for row in rows}


# --- frozen readings -------------------------------------------------------------------


def _holds(recipe: Mapping[str, Any], measured: Mapping[str, Any]) -> bool:
    """Whether `measured` is a reading this recipe can be built from: every
    white balance and levels the recipe asks for, frozen and well formed."""
    import numpy as np

    try:
        develop.grade(
            np.zeros((1, 3), dtype=np.float32),
            dict(recipe),
            measured=dict(measured),
            pointwise=True,
        )
    except ValueError:
        return False
    return True


async def _readings(
    db: AsyncSession, clip: _Clip, recipe: dict[str, Any], measured: Any, at_ms: int | None
) -> tuple[dict[str, Any], int | None]:
    """A stored reading, or the same one taken again.

    A row read back with a reading that will not build (a column written by
    an older version, or by hand) is not an error to show anybody: it is
    measured again on the still it was taken on, else on the reference."""
    reading = dict(measured) if isinstance(measured, Mapping) else {}
    if _holds(recipe, reading):
        return reading, at_ms
    picture: Image.Image | None = None
    shown = at_ms
    if at_ms is not None and at_ms >= 0 and at_ms != clip.reference_ms:
        found, moment = await _still_at(db, clip.asset, at_ms, clip.data_dir)
        if moment >= 0:
            picture, shown = found, moment
    if picture is None and clip.still is not None:
        picture, shown = clip.still, clip.reference_ms
    if picture is None:
        return {}, at_ms
    return await asyncio.to_thread(develop.measure, picture, recipe), shown


def _provenance(row: ClipLook) -> dict[str, Any] | None:
    if not row.model:
        return None
    return {
        "model": row.model,
        "model_revision": row.model_revision,
        "variant": row.variant or "",
        "runtime": row.runtime,
        "runtime_version": row.runtime_version,
        "prompt_version": row.prompt_version,
    }


def _keys(raw: Any) -> tuple[str, ...]:
    return tuple(str(key) for key in raw) if isinstance(raw, list) else ()


async def _from_row(db: AsyncSession, clip: _Clip, row: ClipLook, source: str) -> _Look:
    recipe = clip_look.clean_look(row.recipe if isinstance(row.recipe, Mapping) else {})
    measured, at_ms = await _readings(db, clip, recipe, row.measured, row.still_ts_ms)
    return _Look(
        source=source,
        available=True,
        recipe=recipe,
        measured=measured,
        left_out=_keys(row.left_out),
        measured_at_ms=at_ms,
        reason=row.reason or "",
        provenance=_provenance(row),
        made_at=row.updated_at,
        demo=bool(row.demo),
        features=[float(v) for v in row.features] if isinstance(row.features, list) else [],
    )


# --- the learned look ------------------------------------------------------------------

_learned: OrderedDict[tuple[Any, ...], clip_look.Candidate] = OrderedDict()


def clear_learned() -> None:
    """Forget every learned look worked out so far, and every moment
    remembered as having no picture of its own (tests)."""
    _learned.clear()
    _moved.clear()


async def _examples(
    db: AsyncSession, asset_id: uuid.UUID
) -> list[tuple[Sequence[float], Mapping[str, float]]]:
    """The looks chosen for OTHER clips, with the still each was chosen on.
    A clip is never its own example; "no change" is a choice and counts."""
    rows = (
        await db.execute(
            select(ClipLook.asset_id, ClipLook.features, ClipLook.recipe)
            .where(ClipLook.kind == CHOSEN, ClipLook.asset_id != asset_id)
            .order_by(ClipLook.asset_id)
        )
    ).all()
    return [
        (features, recipe if isinstance(recipe, Mapping) else {})
        for _id, features, recipe in rows
        if isinstance(features, list)
    ]


def _look_stamp(data_dir: Path) -> int | None:
    try:
        return (data_dir / looks.LOOK_PATH).stat().st_mtime_ns
    except OSError:
        return None


async def _learned_look(db: AsyncSession, clip: _Clip) -> _Look:
    if clip.still is None or clip.reference_ms is None:
        return _Look("learned", False, {}, {}, note=clip.problem)
    examples = await _examples(db, clip.asset.id)
    learned_from = hashlib.sha256(
        json.dumps(examples, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    stamp = await asyncio.to_thread(_look_stamp, clip.data_dir)
    key = (clip.asset.id, clip.reference_ms, learned_from, stamp)
    candidate = _learned.get(key)
    if candidate is None:
        photos = await asyncio.to_thread(looks.load, clip.data_dir)
        candidate = await asyncio.to_thread(
            clip_look.learned, clip.still, clips=examples, photos=photos
        )
        _learned[key] = candidate
        while len(_learned) > LEARNED_KEPT:
            _learned.popitem(last=False)
    else:
        _learned.move_to_end(key)
    if not candidate.available:
        return _Look("learned", False, {}, {}, note=candidate.note)
    return _Look(
        source="learned",
        available=True,
        recipe=dict(candidate.recipe),
        measured=dict(candidate.measured),
        left_out=tuple(candidate.left_out),
        measured_at_ms=clip.reference_ms,
        note=candidate.note,
        basis=candidate.basis,
        examples=candidate.examples,
    )


# --- the three looks, and the choice ------------------------------------------------------


@dataclass(frozen=True)
class _Suggest:
    mode: Literal["ready", "demo", "unavailable"]
    ok: bool
    note: str | None


async def _suggest(db: AsyncSession, deps: Any) -> _Suggest:
    found = availability(deps.settings)
    if not found.ok:
        return _Suggest(found.mode, False, CANNOT_SUGGEST_HERE)
    if found.mode == "demo":
        # The demo has no beat: whatever is left waiting is looked at once
        # somebody opens a page (as the AI models page does).
        from framefound.local_models import demo

        demo.keep_going(deps.settings)
    if (await registry.roles(db))["looker"].model_id is None:
        return _Suggest(found.mode, False, NO_LOOKER)
    return _Suggest(found.mode, True, None)


async def _looks(
    db: AsyncSession, clip: _Clip, rows: Mapping[str, ClipLook], suggest: _Suggest
) -> dict[str, _Look]:
    """The three looks a clip is offered, by source. With no still, none of
    them is: there is nothing to judge one on."""
    if clip.problem is not None:
        return {
            source: _Look(source, False, {}, {}, note=clip.problem)
            for source in ("model", "learned", "none")
        }
    suggested = rows.get(SUGGESTED)
    if suggested is not None:
        model = await _from_row(db, clip, suggested, "model")
    else:
        model = _Look("model", False, {}, {}, note=suggest.note or NOT_SUGGESTED_YET)
    return {
        "model": model,
        "learned": await _learned_look(db, clip),
        "none": _Look("none", True, {}, {}),
    }


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        # SQLite hands back what it stored, without the zone; it was UTC.
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recipe_out(recipe: Mapping[str, Any]) -> dict[str, float]:
    return {key: float(value) for key, value in recipe.items()}


def _candidate_out(look: _Look) -> CandidateOut:
    return CandidateOut(
        source=look.source,
        available=look.available,
        note=look.note,
        recipe=_recipe_out(look.recipe),
        in_words=clip_look.in_words(look.recipe),
        reason=look.reason,
        left_out=lut.left_out_words(look.left_out),
        measured_at_ms=look.measured_at_ms if look.available else None,
        basis=look.basis,
        examples=look.examples,
        provenance=ProvenanceOut(**look.provenance) if look.provenance else None,
        made_at=_iso(look.made_at),
        demo=look.demo,
        version=look.version,
    )


async def _chosen_out(db: AsyncSession, clip: _Clip, row: ClipLook | None) -> ChosenOut | None:
    if row is None:
        return None
    look = await _from_row(db, clip, row, row.source)
    chooser = await db.get(User, row.decided_by) if row.decided_by else None
    source = row.source if row.source in ("model", "learned", "none", "custom") else "custom"
    return ChosenOut(
        source=source,
        recipe=_recipe_out(look.recipe),
        in_words=clip_look.in_words(look.recipe),
        left_out=lut.left_out_words(look.left_out),
        measured_at_ms=look.measured_at_ms if look.recipe else None,
        chosen_at=_iso(row.updated_at),
        chosen_by=chooser.email if chooser is not None else None,
        provenance=ProvenanceOut(**look.provenance) if look.provenance else None,
        demo=look.demo,
        version=look.version,
    )


def _folder_of(asset: Asset) -> str:
    return asset.relative_path.rpartition("/")[0]


def _in_folder(asset: Asset) -> Any:
    """Which clips are this clip's folder: the same folder of the same
    library, not folders inside it; video, catalogued and online — and the
    clip on the page always, offline or not. The one rule for the folder
    line, the folder's Suggest and its zip, so the looks the page counts as
    chosen are the ones the zip holds."""
    try:
        beside = and_(
            Asset.library_id == asset.library_id,
            Asset.media_type == "video",
            Asset.availability == "online",
            catalogued(),
            *folders.directly_in(_folder_of(asset)),
        )
    except folders.FolderError:
        # A name the catalogue could not have written; the clip stands alone.
        return Asset.id == asset.id
    return or_(beside, Asset.id == asset.id)


async def _folder(db: AsyncSession, asset: Asset) -> FolderOut:
    """The clips of this clip's folder (`_in_folder`), and their looks."""
    path = _folder_of(asset)
    where = _in_folder(asset)
    clips = (
        await db.execute(select(Asset.id, Asset.filename).where(where).order_by(Asset.filename))
    ).all()
    kinds: dict[uuid.UUID, set[str]] = {}
    for clip_id, kind in (
        await db.execute(
            select(ClipLook.asset_id, ClipLook.kind)
            .join(Asset, Asset.id == ClipLook.asset_id)
            .where(where)
        )
    ).all():
        kinds.setdefault(clip_id, set()).add(kind)

    def state(clip_id: uuid.UUID) -> Literal["chosen", "suggested", "none"]:
        held = kinds.get(clip_id, set())
        return "chosen" if CHOSEN in held else "suggested" if SUGGESTED in held else "none"

    states = {clip_id: state(clip_id) for clip_id, _name in clips}
    others = [
        FolderClipOut(asset_id=clip_id, filename=name, state=states[clip_id])
        for clip_id, name in clips
        if clip_id != asset.id
    ][:FOLDER_LISTED]
    held = list(states.values())
    return FolderOut(
        path=path,
        clips=len(held),
        chosen=held.count("chosen"),
        suggested=held.count("suggested"),
        others=others,
    )


async def _job(
    db: AsyncSession, asset_id: uuid.UUID, suggested: ClipLook | None, *, admin: bool
) -> JobOut | None:
    """The clip's newest request for a suggestion, while it is waiting or under
    way, or when it failed after the suggestion now shown was made (so a
    failure is said until a later suggestion replaces it). Else None."""
    job = await colour_jobs.job_for(db, colour_jobs.CLIP_KIND, str(asset_id))
    shown = colour_jobs.job_out(job, admin=admin)
    if job is None or shown is None:
        return None
    if shown["state"] not in jobs.ACTIVE:
        made = colour_jobs.utc(suggested.updated_at) if suggested is not None else None
        asked = colour_jobs.utc(job.created_at)
        if shown["state"] != "failed" or (made is not None and asked is not None and asked < made):
            return None
    return JobOut(**shown)


async def _answer(db: AsyncSession, clip: _Clip, deps: Any, *, admin: bool = False) -> ClipLooks:
    rows = await _rows(db, clip.asset.id)
    suggest = await _suggest(db, deps)
    offered = await _looks(db, clip, rows, suggest)
    job = await _job(db, clip.asset.id, rows.get(SUGGESTED), admin=admin)
    return ClipLooks(
        asset_id=clip.asset.id,
        filename=clip.asset.filename,
        problem=clip.problem,
        stills=[StillOut(ts_ms=ts, from_=source) for ts, source in clip.stills],
        reference_ts_ms=clip.reference_ms,
        candidates=[_candidate_out(offered[source]) for source in ("model", "learned", "none")],
        chosen=await _chosen_out(db, clip, rows.get(CHOSEN)),
        suggest=SuggestOut(mode=suggest.mode, ok=suggest.ok, note=suggest.note, job=job),
        folder=await _folder(db, clip.asset),
        lut=LutOut(sizes=list(lut.SIZES), default=lut.DEFAULT_SIZE, assumes=lut.ASSUMES),
    )


async def _look_for(db: AsyncSession, clip: _Clip, source: str) -> _Look:
    """The look a picture or a file is made from, by the name the page uses
    for it. 404 when there is no such look for this clip."""
    rows = await _rows(db, clip.asset.id)
    if source == CHOSEN:
        row = rows.get(CHOSEN)
        if row is None:
            raise HTTPException(404, NOTHING_CHOSEN)
        return await _from_row(db, clip, row, row.source)
    if source == "model":
        row = rows.get(SUGGESTED)
        if row is None:
            raise HTTPException(404, NO_SUGGESTION)
        return await _from_row(db, clip, row, "model")
    if source == "learned":
        look = await _learned_look(db, clip)
        if not look.available:
            raise HTTPException(404, NO_LEARNED)
        return look
    return _Look("none", True, {}, {})


def _size(size: int) -> int:
    if size not in lut.SIZES:
        raise HTTPException(400, BAD_SIZE)
    return size


def _table(look: _Look, size: int) -> Any:
    try:
        return lut.build(look.recipe, size=size, measured=look.measured)
    except ValueError:
        # Only a reading that could not be taken again gets here: no still.
        raise HTTPException(409, NO_STILL) from None


def _jpeg(picture: Image.Image) -> bytes:
    out = io.BytesIO()
    picture.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


def _through(still: Image.Image, look: _Look, size: int) -> bytes:
    """The still read back through the table the file would hold."""
    if not look.recipe:
        return _jpeg(still)
    return _jpeg(lut.apply(still, _table(look, size)))


def _picture_response(body: bytes) -> Response:
    # The address changes with the look (`v`), but a look asked again under
    # the same address must never be the browser's old picture.
    return Response(body, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _cube(look: _Look, *, filename: str, source: str, size: int) -> bytes:
    """The `.cube` file of one look, as the bytes to send: built in memory and
    never written through a text-mode file, which on Windows would put back
    the carriage returns that make it invalid."""
    words = _LOOK_WORDS.get(look.source, "a look")
    if source == CHOSEN:
        words = f"the chosen look ({words})"
    text = lut.to_cube(
        _table(look, size),
        title=lut.cube_filename(filename, source)[: -len(".cube")],
        comments=lut.comment_block(
            clip=filename,
            look=words,
            size=size,
            recipe=look.recipe,
            measured=look.measured,
            measured_at_ms=look.measured_at_ms,
            left_out=look.left_out,
            provenance=look.provenance,
            demo=look.demo,
        ),
    )
    return text.encode("ascii")


def _attachment(ascii_name: str, real_name: str) -> str:
    """An ASCII name plus the RFC 5987 one, as the transcript download does."""
    plain = "".join(ch if ch.isascii() and ch not in '"\\' else "_" for ch in ascii_name)
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(real_name)}"


# --- the folder ---------------------------------------------------------------------------


def _zip_name(folder: str, library_name: str) -> str:
    name = folder.rpartition("/")[2] or library_name
    return f"{name} - FrameFound looks.zip"


def _zip(entries: Sequence[tuple[str, bytes]], read_me: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries:
            archive.writestr(name, body)
        archive.writestr("READ ME.txt", read_me.encode("ascii", "replace"))
    return out.getvalue()


def _entry_names(filenames: Sequence[str]) -> list[str]:
    """`<clip stem>.cube` for each clip, numbered where two would collide:
    "A.mov" and "A.mp4" have one stem, and so do two names in a script with
    no ASCII form (both "clip")."""
    taken: set[str] = set()
    names = []
    for filename in filenames:
        stem = lut.clip_stem(filename)
        name, number = f"{stem}.cube", 1
        while name.casefold() in taken:
            number += 1
            name = f"{stem} ({number}).cube"
        taken.add(name.casefold())
        names.append(name)
    return names


@router.get("/folder.zip")
async def folder_looks(
    _user: CurrentUser,
    db: DbDep,
    deps: DepsDep,
    asset_id: uuid.UUID,
    size: int = lut.DEFAULT_SIZE,
) -> Response:
    """The chosen looks of the clip's folder (the clips the folder line
    counts), one `.cube` each, in one zip."""
    size = _size(size)
    asset = await _video(db, asset_id)
    path = _folder_of(asset)
    rows = (
        await db.execute(
            select(Asset, ClipLook)
            .join(ClipLook, ClipLook.asset_id == Asset.id)
            .where(_in_folder(asset), ClipLook.kind == CHOSEN)
            .order_by(Asset.filename, Asset.id)
        )
    ).all()
    chosen = [
        (clip, row)
        for clip, row in rows
        if clip_look.clean_look(row.recipe if isinstance(row.recipe, Mapping) else {})
    ]
    if not chosen:
        raise HTTPException(404, NONE_CHOSEN_IN_FOLDER)
    if size != lut.DEFAULT_SIZE and len(chosen) > ZIP_MOST_FINE:
        raise HTTPException(400, FOLDER_TOO_FINE)
    held = chosen[:ZIP_MOST]
    looks_by_clip = []
    for clip_asset, row in held:
        clip = _Clip(clip_asset, deps.settings.data_dir, [], None, None, None)
        look = await _from_row(db, clip, row, row.source)
        if look.recipe and not _holds(look.recipe, look.measured):
            # Not even the still it was measured on could be read again.
            continue
        looks_by_clip.append((clip_asset.filename, look))
    if not looks_by_clip:
        raise HTTPException(409, NO_STILL)
    read_me = READ_ME
    if len(chosen) > len(held):
        read_me += (
            f"\nThis folder has {len(chosen)} chosen looks; the first {len(held)} by name "
            "are in this file.\n"
        )
    names = _entry_names([filename for filename, _look in looks_by_clip])

    def build() -> bytes:
        return _zip(
            [
                (name, _cube(look, filename=filename, source=CHOSEN, size=size))
                for name, (filename, look) in zip(names, looks_by_clip, strict=True)
            ],
            read_me,
        )

    body = await asyncio.to_thread(build)
    library = await db.get(Library, asset.library_id)
    real = _zip_name(path, library.name if library is not None else "Library")
    return Response(
        body,
        media_type="application/zip",
        headers={
            # clip_stem drops the ".zip" and keeps the rest of the name ASCII.
            "Content-Disposition": _attachment(f"{lut.clip_stem(real)}.zip", real)
        },
    )


async def _wake(deps: Any) -> bool:
    """Wake the lane; False when the queue is down."""
    return await asyncio.to_thread(local_ai_tasks.wake, deps.settings)


async def _can_suggest(db: AsyncSession, deps: Any) -> None:
    """409 with the page's own sentence where Suggest cannot be pressed."""
    suggest = await _suggest(db, deps)
    if not suggest.ok:
        raise HTTPException(409, suggest.note or CANNOT_SUGGEST_HERE)


@router.post("/folder/suggest", status_code=202, response_model=FolderQueuedOut)
async def suggest_folder(
    body: FolderIn, user: CurrentUser, db: DbDep, deps: DepsDep
) -> FolderQueuedOut:
    """Ask for a suggestion for every clip of the clip's folder that has none:
    no suggestion, no choice and none on its way. Each is its own job, behind
    a single clip somebody pressed Suggest on."""
    asset = await _video(db, body.asset_id)
    await _can_suggest(db, deps)
    clips = list(await db.scalars(select(Asset).where(_in_folder(asset)).order_by(Asset.filename)))
    ids = [clip.id for clip in clips]
    looked_at = set(await db.scalars(select(ClipLook.asset_id).where(ClipLook.asset_id.in_(ids))))
    active = list(
        await db.scalars(
            select(LocalAiJob)
            .where(
                LocalAiJob.kind == colour_jobs.CLIP_KIND,
                LocalAiJob.subject.in_([str(each) for each in ids]),
                LocalAiJob.state.in_(jobs.ACTIVE),
            )
            .execution_options(populate_existing=True)
        )
    )
    # A job whose worker died reads as failed on every clip's page; it is
    # ended now, as the lane's next tick would, and that clip asked for again.
    reaped = False
    for job in active:
        reaped = await jobs.reap(db, job) or reaped
    if reaped:
        await db.commit()
    on_its_way = {job.subject for job in active if jobs.shown(job)[0] in jobs.ACTIVE}
    # A clip with no shots and no sampled frames has no still to show; its
    # job would only fail. Its page says why, and Reprocess is there.
    with_pictures = set(
        await db.scalars(
            select(Asset.id).where(
                Asset.id.in_(ids),
                or_(
                    exists().where(ShotWindow.asset_id == Asset.id),
                    exists().where(Frame.asset_id == Asset.id),
                ),
            )
        )
    )
    queued: list[uuid.UUID] = []
    already = no_still = 0
    for clip in clips:
        if clip.id in looked_at or str(clip.id) in on_its_way:
            already += 1
        elif clip.id not in with_pictures:
            no_still += 1
        else:
            try:
                job = await colour_jobs.queue_clip(
                    db, clip, user_id=user.id, priority=FOLDER_PRIORITY
                )
            except jobs.JobRefused:
                # Asked for meanwhile (a second press, or Suggest on that very
                # clip): on its way, so left alone, and the rest still asked.
                already += 1
                continue
            queued.append(job.id)
    await db.commit()
    if queued and not await _wake(deps):
        # Nothing is left waiting for a worker that will never come.
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id.in_(queued)))
        await db.commit()
        raise HTTPException(503, QUEUE_DOWN)
    log.info(
        "clip_looks.folder_suggest_queued",
        asset_id=str(asset.id),
        queued=len(queued),
        already=already,
    )
    return FolderQueuedOut(queued=len(queued), already=already, no_still=no_still)


# --- one clip -----------------------------------------------------------------------------


@router.get("/{asset_id}", response_model=ClipLooks)
async def clip_looks(asset_id: uuid.UUID, user: CurrentUser, db: DbDep, deps: DepsDep) -> ClipLooks:
    """The three looks of a clip, the one chosen, and what the page needs
    round them. A clip with no picture yet is a 200 that says so."""
    clip = await _clip(db, asset_id, deps.settings.data_dir)
    return await _answer(db, clip, deps, admin=user.role == "admin")


@router.post("/{asset_id}/suggest", status_code=202, response_model=QueuedOut)
async def suggest(asset_id: uuid.UUID, user: CurrentUser, db: DbDep, deps: DepsDep) -> QueuedOut:
    """Ask the Looker for a look for this clip. Answers at once: the job runs on
    the local-AI lane, and the page follows it in `suggest.job`. A model that
    only fits overnight is not refused; the job waits and says until when."""
    clip = await _clip(db, asset_id, deps.settings.data_dir)
    await _can_suggest(db, deps)
    if clip.problem is not None:
        raise HTTPException(409, clip.problem)
    current = await colour_jobs.job_for(db, colour_jobs.CLIP_KIND, str(clip.asset.id))
    if current is not None and await jobs.reap(db, current):
        # Its worker died: the page already shows it failed. Ended now, as the
        # lane's next tick would, so it does not stand in the way of asking
        # again (with the worker down, that tick may never come).
        await db.commit()
    try:
        job = await colour_jobs.queue_clip(
            db, clip.asset, user_id=user.id, priority=SUGGEST_PRIORITY
        )
    except jobs.JobRefused:
        raise HTTPException(409, ON_ITS_WAY) from None
    job_id = job.id
    await db.commit()
    if not await _wake(deps):
        # Nothing is left waiting for a worker that will never come.
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id == job_id))
        await db.commit()
        raise HTTPException(503, QUEUE_DOWN)
    log.info("clip_looks.suggest_queued", asset_id=str(clip.asset.id), job_id=str(job_id))
    found = await db.get(LocalAiJob, job_id, populate_existing=True)
    return QueuedOut(job=JobOut(**colour_jobs.shown_job(found or job, admin=user.role == "admin")))


@router.post("/{asset_id}/suggest/cancel", response_model=QueuedOut)
async def cancel_suggestion(
    asset_id: uuid.UUID, user: CurrentUser, db: DbDep, deps: DepsDep
) -> QueuedOut:
    """Cancel the request for a suggestion that is waiting or under way. One
    waiting ends at once; one under way stops at the next step it can."""
    asset = await _video(db, asset_id)
    job = await colour_jobs.job_for(db, colour_jobs.CLIP_KIND, str(asset.id))
    if job is None or jobs.shown(job)[0] not in jobs.ACTIVE:
        raise HTTPException(409, NOTHING_WAITING)
    try:
        job = await jobs.request_cancel(db, job.id)
    except (jobs.JobRefused, jobs.NoSuchJob):
        raise HTTPException(409, NOTHING_WAITING) from None
    await db.commit()
    log.info("clip_looks.suggest_cancelled", asset_id=str(asset.id), job_id=str(job.id))
    return QueuedOut(job=JobOut(**colour_jobs.shown_job(job, admin=user.role == "admin")))


@router.get("/{asset_id}/preview.jpg")
async def preview(
    asset_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    deps: DepsDep,
    source: str = Query(...),
    ts: int | None = Query(default=None, ge=0),
    size: int = lut.DEFAULT_SIZE,
    # Ignored: the page puts the look's version here, so a changed look is a
    # new address to the browser.
    v: str | None = None,
) -> Response:
    """The still at `ts` read back through the table built from that look."""
    if source not in ("model", "learned", "none", CHOSEN):
        raise HTTPException(400, BAD_PREVIEW_SOURCE)
    size = _size(size)
    clip = await _clip(db, asset_id, deps.settings.data_dir)
    still, _shown = await _picture_at(db, clip, ts)
    look = await _look_for(db, clip, source)
    return _picture_response(await asyncio.to_thread(_through, still, look, size))


@router.post("/{asset_id}/preview")
async def preview_sliders(
    asset_id: uuid.UUID,
    body: LookIn,
    _user: CurrentUser,
    db: DbDep,
    deps: DepsDep,
    ts: int | None = Query(default=None, ge=0),
    size: int = lut.DEFAULT_SIZE,
) -> Response:
    """Sliders set by hand, shown the same way: white balance measured on the
    reference still, the still at `ts` read through the table."""
    size = _size(size)
    clip = await _clip(db, asset_id, deps.settings.data_dir)
    still, _shown = await _picture_at(db, clip, ts)
    look = await _custom(clip, body)
    return _picture_response(await asyncio.to_thread(_through, still, look, size))


async def _custom(clip: _Clip, sliders: LookIn) -> _Look:
    if clip.still is None or clip.reference_ms is None:
        raise HTTPException(409, NO_STILL)
    recipe = clip_look.clean_look(sliders.model_dump())
    measured = await asyncio.to_thread(develop.measure, clip.still, recipe)
    return _Look("custom", True, recipe, measured, measured_at_ms=clip.reference_ms)


@router.put("/{asset_id}/choice", response_model=ClipLooks)
async def choose(
    asset_id: uuid.UUID, body: ChoiceIn, user: CurrentUser, db: DbDep, deps: DepsDep
) -> ClipLooks:
    """A person's choice for this clip: one of the three looks as it is
    offered now, or sliders of their own. It replaces an earlier choice and
    touches nothing else; a suggestion stays a suggestion."""
    # Read now: a retried store rolls the session back, which expires the
    # signed-in user's row with everything else loaded in it.
    admin, user_id = user.role == "admin", user.id
    clip = await _clip(db, asset_id, deps.settings.data_dir)
    if body.source == "custom":
        if body.recipe is None:
            raise HTTPException(400, SEND_SLIDERS)
        look = await _custom(clip, body.recipe)
    elif body.source == "model":
        row = (await _rows(db, clip.asset.id)).get(SUGGESTED)
        if row is None:
            raise HTTPException(409, NO_SUGGESTION_TO_USE)
        look = await _from_row(db, clip, row, "model")
    else:
        if clip.problem is not None:
            raise HTTPException(409, clip.problem)
        if body.source == "learned":
            look = await _learned_look(db, clip)
            if not look.available:
                raise HTTPException(409, NO_LEARNED_TO_USE)
        else:
            look = _Look("none", True, {}, {}, measured_at_ms=clip.reference_ms)
    features = look.features
    if not features and clip.still is not None:
        features = await asyncio.to_thread(clip_look.features_of, clip.still)
    await _store_choice(db, clip.asset, look, features, user_id)
    log.info("clip_looks.chosen", asset_id=str(clip.asset.id), source=look.source)
    return await _answer(db, clip, deps, admin=admin)


async def _store_choice(
    db: AsyncSession,
    asset: Asset,
    look: _Look,
    features: list[float],
    user_id: uuid.UUID,
) -> None:
    """Write the clip's one `chosen` row: an update of that row when there is
    one, else an insert. Two presses at once can both find none; the second
    insert then meets the unique key and becomes an update. The rollback
    that takes expires everything loaded in the session, so `asset` is read
    again before the caller answers with it (an async session cannot load
    an expired attribute on access: that would be a 500 after the choice
    was stored)."""
    asset_id = asset.id
    provenance = look.provenance or {}
    values: dict[str, Any] = {
        "source": look.source,
        "recipe": dict(look.recipe),
        "measured": dict(look.measured),
        "left_out": list(look.left_out),
        "still_ts_ms": look.measured_at_ms if look.measured_at_ms is not None else 0,
        "features": list(features),
        "reason": look.reason[:300],
        "model": provenance.get("model"),
        "model_revision": provenance.get("model_revision"),
        "variant": provenance.get("variant") or "",
        "runtime": provenance.get("runtime"),
        "runtime_version": provenance.get("runtime_version"),
        "prompt_version": provenance.get("prompt_version"),
        "demo": look.demo,
        "decided_by": user_id,
        "updated_at": datetime.now(UTC),
    }
    for attempt in range(2):
        row = (await _rows(db, asset_id)).get(CHOSEN)
        if row is None:
            row = ClipLook(asset_id=asset_id, kind=CHOSEN)
            db.add(row)
        for name, value in values.items():
            setattr(row, name, value)
        try:
            await db.commit()
            return
        except IntegrityError:
            await db.rollback()
            if attempt:
                raise
            await db.refresh(asset)


@router.delete("/{asset_id}/choice", response_model=ClipLooks)
async def clear_choice(
    asset_id: uuid.UUID, user: CurrentUser, db: DbDep, deps: DepsDep
) -> ClipLooks:
    """Forget the clip's choice. The suggestion, if any, is untouched."""
    clip = await _clip(db, asset_id, deps.settings.data_dir)
    row = (await _rows(db, clip.asset.id)).get(CHOSEN)
    if row is not None:
        await db.delete(row)
        await db.commit()
        log.info("clip_looks.cleared", asset_id=str(clip.asset.id))
    return await _answer(db, clip, deps, admin=user.role == "admin")


@router.get("/{asset_id}/look.cube")
async def look_file(
    asset_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    deps: DepsDep,
    source: str = CHOSEN,
    size: int = lut.DEFAULT_SIZE,
) -> Response:
    """One look as a `.cube` LUT for Premiere: plain ASCII, built when asked
    and never stored."""
    if source not in (CHOSEN, "model", "learned"):
        raise HTTPException(400, BAD_CUBE_SOURCE)
    size = _size(size)
    clip = await _clip(db, asset_id, deps.settings.data_dir)
    look = await _look_for(db, clip, source)
    if not look.recipe:
        raise HTTPException(400, NO_LUT_NEEDED)
    filename = clip.asset.filename
    body = await asyncio.to_thread(_cube, look, filename=filename, source=source, size=size)
    name = lut.cube_filename(filename, source)
    return Response(
        body,
        media_type="text/plain; charset=us-ascii",
        headers={"Content-Disposition": _attachment(name, name)},
    )
