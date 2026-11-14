"""The pictures a model is shown: files FrameFound already has, never new ones.

A photograph is shown as its preview (else its thumbnail); a clip judged as
a whole, as its poster (else its thumbnail); a shot, as the stills the shot
analysis saved at its start, middle and end — or the middle alone — each
else a frame the catalogue sampled within 0.6 s of that moment
(`nle.shotlist.still_found(..., nearest=False)`, the one rule for "which
file is this shot's still").

Nothing here grabs a frame from a playable copy, starts ffmpeg or waits on
the proxy store. Looking runs in the local-AI lane, where there is no
ffmpeg and the copies may be on the NAS; a shot with no picture on this
machine is recorded as not looked at, and re-analysing the clip remakes
its stills. `processing.shots.still_at` is the one function that grabs,
and it is not used here.

Every file is re-encoded as the small JPEG Claude's reviewer sends
(`tag_review.preview_from_file`: 768 px at most, quality 80, about
100 KB), so three of them sit far inside what one question may carry. A
file that is there but will not read as a picture (half a JPEG from a
worker that was stopped) is stepped past, never shown.

Reading files is blocking and runs in a thread; a thread never touches a
database session.
"""

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai import tag_review
from framefound.db.models import Derivative
from framefound.looking.scope import WHOLE_PICTURES, Item
from framefound.looking.settings_store import STILLS_CHOICES
from framefound.nle import shotlist
from framefound.storage import stores

# Unreadable files stepped past for one moment before it counts as having
# no picture: a clip whose frames are all broken is not searched to the end.
UNREADABLE_LIMIT = 4
# The derived files that show a whole item, best first. The scope's list,
# not a copy: what is counted as showable is what is shown.
WHOLE = WHOLE_PICTURES


def moments_for(start_ms: int, end_ms: int, stills: int) -> list[int]:
    """The moments of a shot a model is shown: its start, middle and end
    (`stills == 3`; fewer when the shot is too short for three to differ),
    or its middle (`stills == 1`). ValueError for any other number: the
    setting offers exactly those two."""
    if stills not in STILLS_CHOICES:
        raise ValueError(f"A shot is shown as {' or '.join(map(str, STILLS_CHOICES))} pictures.")
    # Imported here, as nle.shotlist does: processing.shots brings the decode
    # path with it, and choosing moments needs only its arithmetic.
    from framefound.processing.shots import window_moments

    start, middle, end = window_moments(start_ms, end_ms)
    return [middle] if stills == 1 else sorted({start, middle, end})


def read_picture(path: Path | None) -> bytes | None:
    """`path` as the JPEG a model is sent, or None when it is not there or
    does not read as a picture. Blocking."""
    if path is None:
        return None
    # Anything Pillow raises — missing, truncated, not an image, too large —
    # means "no picture", never a failed review.
    try:
        return tag_review.preview_from_file(path)
    except Exception:
        return None


def for_photo(data_dir: Path, preview: str | None, thumbnail: str | None) -> bytes | None:
    """A whole item's picture from its derived files' relative paths: the
    first of `preview` and `thumbnail` that is there and reads. (A clip's
    poster is passed as `preview`.) Blocking."""
    return _first_of(data_dir, (preview, thumbnail))


def for_shot(paths: Sequence[Path | None]) -> list[bytes]:
    """One JPEG per path that is there and reads, in order. Blocking."""
    pictures: list[bytes] = []
    for path in paths:
        picture = read_picture(path)
        if picture is not None:
            pictures.append(picture)
    return pictures


async def _whole(db: AsyncSession, item: Item, data_dir: Path) -> list[bytes]:
    kinds = WHOLE.get(item.media_type)
    if kinds is None:
        return []
    rows = await db.execute(
        select(Derivative.kind, Derivative.relative_path).where(
            Derivative.asset_id == item.asset_id,
            Derivative.kind.in_(kinds),
            Derivative.status == "ready",
        )
    )
    relative = {str(kind): str(path) for kind, path in rows.all()}
    picture = await asyncio.to_thread(_first_of, data_dir, [relative.get(kind) for kind in kinds])
    return [] if picture is None else [picture]


def _first_of(data_dir: Path, relative_paths: Sequence[str | None]) -> bytes | None:
    """The first of these derived files that is there and reads. Blocking."""
    for relative in relative_paths:
        if relative:
            picture = read_picture(stores.derivative_path(data_dir, relative))
            if picture is not None:
                return picture
    return None


async def _at(
    db: AsyncSession, asset_id: uuid.UUID, moment: int, data_dir: Path
) -> tuple[Path, bytes] | None:
    """The picture on disk for one moment of a clip and its file, stepping
    past files that do not read (a broken saved still must not hide a
    sampled frame beside it that reads)."""
    broken: set[Path] = set()
    # Counted in tries, not in files stepped past: should a lookup ever hand
    # the same broken file back, this still ends.
    for _ in range(UNREADABLE_LIMIT):
        found = await shotlist.still_found(
            db, asset_id, moment, data_dir, nearest=False, skip=broken
        )
        if found is None or found[0] in broken:
            return None
        path = found[0]
        picture = await asyncio.to_thread(read_picture, path)
        if picture is not None:
            return path, picture
        broken.add(path)
    return None


async def for_item(db: AsyncSession, item: Item, *, stills: int, data_dir: Path) -> list[bytes]:
    """The pictures to show a model for `item`, in order; [] when this
    machine has none (D22: no still, no look).

    A shot gives up to `stills` pictures (3 or 1); two moments that come to
    the same file are one picture. A whole item gives one."""
    if item.start_ms is None or item.end_ms is None:
        return await _whole(db, item, data_dir)
    pictures: list[bytes] = []
    shown: set[Path] = set()
    for moment in moments_for(item.start_ms, item.end_ms, stills):
        found = await _at(db, item.asset_id, moment, data_dir)
        if found is None or found[0] in shown:
            continue
        shown.add(found[0])
        pictures.append(found[1])
    return pictures
