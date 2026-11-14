"""The one function that writes a rough cut: clips end to end, as an FCP7
XML sequence that Premiere Pro and DaVinci Resolve open as a timeline, and
the same cut written into cut history.

It was the body of the connector's `build_rough_cut` tool. It moved here so
a local model's draft (a job, a page) builds its timeline with exactly the
code Claude's cuts use, on an install without the optional `mcp` package:
one writer means one set of rules about paths, photographs held on screen,
out points clamped to the clip, and what history records.

The connector's tool is now a thin wrapper: it opens the session, says who
is asking, and turns a `CutError` into its own error with the same words.
Nothing here imports `framefound.mcp`.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import Asset, Cut, CutPiece, Library, PathMapping
from framefound.nle import fcp7, shotlist

log = structlog.get_logger()

MAX_CUTS = 200
DEFAULT_STILL_S = 4.0
DEFAULT_TITLE = "FrameFound rough cut"


# Where Claude finds the id of a cut to revise. recent_cuts leaves out a
# local draft nobody downloaded, which is the cut most often revised, so
# that draft's id comes from the review request instead.
WHERE_CUT_IDS_ARE = (
    "A local draft's id is in the review request about it; "
    "recent_cuts with verbose lists the ids of finished cuts."
)


class CutError(ValueError):
    """The request cannot become a cut; the message says why and names the fix."""


# --- shapes (the connector's, moved verbatim) ---------------------------------


class CutIn(BaseModel):
    id: str = Field(description="Asset id (a video, or a photograph to hold on screen)")
    in_s: float = Field(default=0.0, ge=0, description="Start, seconds into the source")
    out_s: float | None = Field(
        default=None, description="End, seconds into the source; a photo's hold length"
    )
    note: str = Field(default="", max_length=500, description="Becomes a marker on the cut")
    # Speed is recorded in cut history only; the XML plays every piece at
    # 100% until speed per cut lands in the timeline itself.


class RoughCut(BaseModel):
    title: str
    filename: str
    profile: str | None
    duration_s: float
    cuts: int
    xml: str = Field(description="FCP7 XML: save as .xml and import into Premiere or Resolve")
    unmapped: list[str] = Field(
        description="Files with no path for this profile; they will import offline"
    )
    note: str
    recorded: bool = Field(
        default=False, description="Whether the cut was written to cut history (recent_cuts)"
    )
    cut_id: str | None = Field(default=None, description="The cut history row, when recorded")
    version: int | None = Field(default=None, description="Its version among the cuts it revises")


# --- finding things -----------------------------------------------------------


async def _by_id(db: AsyncSession, model: Any, ref: str) -> list[Any] | None:
    """The rows (at most two) a full UUID or a short id names; None when
    `ref` is neither. A short id is the first eight or more hex characters,
    as the compact answers print them."""
    raw = ref.strip()
    try:
        key = uuid.UUID(raw)
    except ValueError:
        key = None
    if key is not None:
        row = await db.get(model, key)
        return [row] if row is not None else []
    prefix = raw.replace("-", "").lower()
    if len(prefix) < 8 or any(ch not in "0123456789abcdef" for ch in prefix):
        return None
    # A range rather than a string match: it works on PostgreSQL's uuid
    # and SQLite's hex text alike, and uses the primary key.
    low = uuid.UUID(prefix.ljust(32, "0"))
    high = uuid.UUID(prefix.ljust(32, "f"))
    return list(
        (await db.execute(select(model).where(model.id >= low, model.id <= high).limit(2)))
        .scalars()
        .all()
    )


async def find_asset(db: AsyncSession, ref: str) -> Asset:
    """An asset by its UUID, or by the short id the compact answers print
    (the first eight or more hex characters), when that is unambiguous."""
    found = await _by_id(db, Asset, ref)
    if found is None:
        raise CutError(f"{ref!r} is not an asset id")
    if not found:
        raise CutError(f"No asset {ref}")
    if len(found) > 1:
        raise CutError(f"{ref!r} matches more than one asset; give more of the id")
    asset: Asset = found[0]
    return asset


async def find_cut(db: AsyncSession, ref: str) -> Cut:
    """A cut in history by its UUID or short id, as `find_asset` reads them.

    Any cut in history is found, a local draft that was never downloaded
    included. Claude names a local draft from the review request about it
    (which gives the full id), and a finished cut from recent_cuts with
    verbose; recent_cuts never lists an unsent draft, so the refusal must
    not send Claude only there. A typo is refused with where to look
    rather than starting a family of its own.
    """
    found = await _by_id(db, Cut, ref)
    if not found:
        raise CutError(f"No cut {ref!r}. {WHERE_CUT_IDS_ARE}")
    if len(found) > 1:
        raise CutError(f"{ref!r} matches more than one cut; give more of the id")
    cut: Cut = found[0]
    return cut


async def profile_map(db: AsyncSession, profile: str | None) -> dict[uuid.UUID, tuple[Any, Any]]:
    """Every library a workstation profile covers, keyed by library; {} for
    no profile. A name that covers nothing is refused with the real names,
    since a wrong one would otherwise make every clip import offline."""
    from framefound.api.v1.panel import _profiles

    if not profile or not profile.strip():
        return {}
    found = await _profiles(db, profile)
    if not found:
        names = sorted(set((await db.execute(select(PathMapping.profile_name))).scalars().all()))
        raise CutError(
            f"No path profile called {profile!r}. Profiles: {', '.join(names) or 'none yet'} "
            "(add one per library on the Libraries page)."
        )
    return found


def local_path(asset: Asset, profiles: dict[uuid.UUID, tuple[Any, Any]]) -> str | None:
    """The asset's original as a profile's computer opens it, or None."""
    from framefound.api.v1.panel import _translate_for

    return _translate_for(asset, profiles)


def has_audio(asset: Asset) -> bool:
    return asset.media_type == "audio" or asset.audio_codec is not None


def majority_rate(spans: Sequence[tuple[float | None, float]]) -> float | None:
    """The frame rate that covers the most seconds of a cut, from (fps,
    seconds) per piece; None when no piece has a rate (all photographs).

    Left to itself the timeline takes its rate from the first video piece,
    so one 59.94 opener would set a cut shot mostly at 29.97 at the wrong
    rate. Rates that differ only past the second decimal (29.97 and
    29.97003) are one rate; a tie goes to the one that came first.
    """
    seconds: dict[float, float] = {}
    first: dict[float, float] = {}
    for fps, length in spans:
        if fps is None or fps <= 0:
            continue
        key = round(fps, 2)
        first.setdefault(key, fps)
        seconds[key] = seconds.get(key, 0.0) + max(length, 0.0)
    if not seconds:
        return None
    best = max(seconds, key=lambda k: seconds[k])  # max keeps the first of equals
    return first[best]


# --- the cut ------------------------------------------------------------------


async def build_rough_cut(
    db: AsyncSession,
    title: str,
    cuts: list[CutIn],
    *,
    profile: str | None = None,
    still_s: float = DEFAULT_STILL_S,
    client: str | None = None,
    record: bool = True,
    created_by: uuid.UUID | None = None,
    by: str = "",
    source: str = "mcp",
    parent: str | uuid.UUID | None = None,
    provenance: Mapping[str, str] | None = None,
    sequence_fps: float | None = None,
    must_record: bool = False,
) -> RoughCut:
    """Lay `cuts` end to end into an FCP7 XML sequence and, with `record`,
    write it into cut history in the session given (which this commits).

    Every refusal is a `CutError` whose message is the sentence the
    connector has always answered with. `sequence_fps` sets the timeline's
    rate; left out, the first video piece sets it, as it always has.
    History is a record, not the product, so a failure to write it is
    logged and the timeline still comes back with `recorded` false; unless
    `must_record`, for a caller whose product *is* the record (a local
    draft), where the failure is raised after the session is rolled back.

    `parent` (a cut's id or short id) makes the recorded cut the next
    version of that cut's family, kept beside it; neither replaces the
    other. A parent that is not in history is refused before anything is
    built, whether or not the cut is to be recorded: the caller meant a cut
    that is not there. `provenance` names the local build that drafted it,
    under the column names of `shotlist.PROVENANCE`.
    """
    if not cuts:
        raise CutError("Give at least one cut")
    if len(cuts) > MAX_CUTS:
        raise CutError(f"At most {MAX_CUTS} cuts in one sequence")
    # A caller's mistake, not the editor's: refused here, where it cannot be
    # mistaken for a failure to record and swallowed with the log line.
    unknown = sorted(set(provenance or {}) - set(shotlist.PROVENANCE))
    if unknown:
        raise ValueError(f"not provenance: {', '.join(unknown)}")
    # A blank parent (a client that sends "" for "none") revises nothing.
    asked = str(parent).strip() if parent is not None else ""
    revises = await find_cut(db, asked) if asked else None
    still_s = min(max(still_s, 0.5), 60.0)
    # What no XML file can hold (a NUL in a model's reason, say) is left out
    # once, here, so the timeline and the record carry the same words, and
    # PostgreSQL, which refuses a NUL in text, is never handed one.
    title = fcp7.xml_text(title)
    notes = [fcp7.xml_text(cut.note) for cut in cuts]
    profiles = await profile_map(db, profile)
    libraries = {lib.id: lib for lib in (await db.execute(select(Library))).scalars()}
    assets: dict[str, Asset] = {}
    for cut in cuts:
        if cut.id not in assets:
            assets[cut.id] = await find_asset(db, cut.id)

    built: list[fcp7.Cut] = []
    unmapped: list[str] = []
    for cut, note in zip(cuts, notes, strict=True):
        asset = assets[cut.id]
        if asset.media_type == "audio":
            raise CutError(f"{asset.filename} is audio; a rough cut takes video and photos")
        path = local_path(asset, profiles) if profiles else None
        if path is None:
            library = libraries.get(asset.library_id)
            root = library.root_path.rstrip("/") if library else ""
            path = f"{root}/{asset.relative_path}"
            if asset.filename not in unmapped:
                unmapped.append(asset.filename)
        clip = fcp7.Clip(
            name=asset.filename,
            path=path,
            duration_s=asset.duration_s,
            fps=asset.fps,
            width=asset.width,
            height=asset.height,
            has_audio=has_audio(asset),
        )
        if asset.media_type == "image":
            hold = (cut.out_s - cut.in_s) if cut.out_s is not None else still_s
            built.append(fcp7.Cut(clip, 0.0, max(0.5, hold), is_still=True, note=note))
            continue
        out_s = cut.out_s if cut.out_s is not None else (asset.duration_s or cut.in_s)
        if asset.duration_s is not None:
            out_s = min(out_s, asset.duration_s)
        if out_s <= cut.in_s:
            raise CutError(
                f"{asset.filename}: out_s ({out_s:g}) must be after in_s ({cut.in_s:g})"
                + (f"; the clip is {asset.duration_s:g}s long" if asset.duration_s else "")
            )
        built.append(fcp7.Cut(clip, cut.in_s, out_s, note=note))

    # None (the connector's case) leaves the rate to the first video piece,
    # so its timelines stay byte for byte what they were.
    xml = fcp7.build_sequence(title.strip() or DEFAULT_TITLE, built, fps=sequence_fps)
    total = sum(c.out_s - c.in_s for c in built)
    recorded = False
    cut_id: str | None = None
    version: int | None = None
    if record:
        pieces = [
            (assets[cut.id], piece.in_s, piece.out_s, 1.0)
            for cut, piece in zip(cuts, built, strict=True)
        ]
        try:
            row = await shotlist.record_cut(
                db,
                title.strip() or DEFAULT_TITLE,
                pieces,
                client=client,
                profile=profile,
                created_by=created_by,
                source=source,
                # Kept so the cut can be downloaded again with its markers.
                notes=notes,
                parent=revises,
                provenance=provenance,
            )
            # The identity survives the commit without a reload, which an
            # async session cannot do lazily; the version is read back by
            # it rather than off a row a session may have expired.
            identity = inspect(row).identity
            if identity:
                cut_id = str(identity[0])
                version = await db.scalar(select(Cut.version).where(Cut.id == identity[0]))
            recorded = True
        except Exception:
            # A half-written cut must not poison the caller's session: its
            # next statement would fail on the broken transaction instead.
            await db.rollback()
            if must_record:
                raise
            # History is a record, not the product: the timeline still
            # goes back to the editor.
            log.warning("mcp.cut_not_recorded", exc_info=True)
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "-" for ch in title).strip()
    note = f"{len(built)} cuts, {total:.1f}s."
    if unmapped:
        note += (
            f" {len(unmapped)} file(s) have no path for "
            + (f"profile {profile!r}" if profile else "any profile (none given)")
            + "; they use the server's path and will import offline until relinked."
        )
    if recorded:
        note += " Recorded in cut history."
        if revises is not None:
            note += f" Kept as version {version} beside the cut it revises."
    # The connector's event name, so existing log searches still find it.
    log.info("mcp.rough_cut", cuts=len(built), seconds=round(total, 1), by=by)
    return RoughCut(
        title=title,
        filename=f"{safe or 'rough-cut'}.xml",
        profile=profile,
        duration_s=round(total, 3),
        cuts=len(built),
        xml=xml,
        unmapped=unmapped,
        note=note,
        recorded=recorded,
        cut_id=cut_id,
        version=version,
    )


# --- a cut again, from history -------------------------------------------------

NOTHING_TO_SEND = (
    "Every clip in this cut has been deleted from the catalogue, so there is nothing to send."
)


class NothingToSend(CutError):
    """No piece of the cut has a clip left in the catalogue. A `CutError`
    so every caller that reads one reads this; its own class so the API can
    answer it as a conflict with the cut rather than a bad request."""


@dataclass(frozen=True)
class Rebuilt:
    cut: RoughCut
    # Filenames, each once, in the order the cut used them.
    missing: list[str]  # deleted from the catalogue: left out
    offline: list[str]  # not on the share now: still written, imports offline until back


async def rebuild(db: AsyncSession, cut: Cut, *, profile: str | None) -> Rebuilt:
    """The Premiere file for a cut already in history, for `profile`'s
    computer: its stored pieces back through `build_rough_cut`, recording
    nothing.

    A cut is kept as pieces, not as a file, so the same cut downloads for
    one computer today and another tomorrow with only the paths changed.
    A piece whose clip has since been deleted is left out and named; one
    whose clip is merely off the share is written (it relinks once the
    share is back) and named. The timeline runs at the rate most of the
    footage was shot at, so a short 59.94 opener does not set it.
    """
    pieces = (
        (
            await db.execute(
                select(CutPiece).where(CutPiece.cut_id == cut.id).order_by(CutPiece.position)
            )
        )
        .scalars()
        .all()
    )
    ids = {piece.asset_id for piece in pieces if piece.asset_id is not None}
    assets: dict[uuid.UUID, Asset] = {}
    if ids:
        found = (await db.execute(select(Asset).where(Asset.id.in_(ids)))).scalars()
        assets = {asset.id: asset for asset in found}

    cuts: list[CutIn] = []
    spans: list[tuple[float | None, float]] = []
    missing: list[str] = []
    offline: list[str] = []
    for piece in pieces:
        # asset_id is SET NULL when the clip is deleted; a database that
        # does not enforce the key leaves it pointing at nothing instead.
        asset = assets.get(piece.asset_id) if piece.asset_id is not None else None
        if asset is None:
            if piece.filename not in missing:
                missing.append(piece.filename)
            continue
        if asset.availability != "online" and asset.filename not in offline:
            offline.append(asset.filename)
        in_s, out_s = piece.in_ms / 1000, piece.out_ms / 1000
        # A photograph was recorded as held from 0 for its length, which
        # is how build_rough_cut reads a hold back.
        cuts.append(CutIn(id=str(asset.id), in_s=in_s, out_s=out_s, note=piece.note))
        spans.append((asset.fps if asset.media_type == "video" else None, out_s - in_s))
    if not cuts:
        raise NothingToSend(NOTHING_TO_SEND)
    built = await build_rough_cut(
        db,
        cut.title,
        cuts,
        profile=profile,
        record=False,
        sequence_fps=majority_rate(spans),
    )
    return Rebuilt(cut=built, missing=missing, offline=offline)
