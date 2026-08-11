"""Turning a folder of camera filenames into a gallery that sorts itself.

The naming scheme is the one proven in the listing exporter and the Drive
demo: `NN - Room label - original_stem.ext`. The original stem stays in the
name on purpose — it is the join key back to the RAW file, the catalogue,
and any earlier delivery, and keeping it is what makes the whole operation
reversible by eye as well as by manifest.

Classification is catalogue-first: when the shoot already lives on the NAS,
every photograph has a CLIP embedding in pgvector and labelling it is a dot
product — no pixels move anywhere. Only files the catalogue has never seen
fall back to embedding a small Drive thumbnail locally.
"""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import Asset, Frame

# An earlier organize pass leaves "07 - Kitchen - IMG_1731.jpg"; the true
# stem is IMG_1731. Stripping the prefix makes a re-run renumber cleanly
# instead of stacking prefixes, and keeps catalogue matching working on a
# folder that was already sorted once.
_SORT_PREFIX = re.compile(r"^\d{1,3}\s*-\s*.{1,40}?\s*-\s*")

_LIKE_SPECIALS = re.compile(r"([\\%_])")


def original_stem(name: str) -> tuple[str, str]:
    """('IMG_1731', '.jpg') from either the camera name or a sorted name."""
    base, dot, ext = name.rpartition(".")
    if not dot:
        base, ext = name, ""
    stripped = _SORT_PREFIX.sub("", base).strip()
    return stripped or base, f".{ext}" if dot else ""


def proposed_name(index: int, room_label: str, stem: str, ext: str) -> str:
    return f"{index:02d} - {room_label} - {stem}{ext.lower()}"


def folder_tokens(folder_name: str) -> set[str]:
    """Words that tie a Drive folder to a NAS path: '1505', 'kings', 'hwy'."""
    return {t.lower() for t in re.findall(r"[A-Za-z0-9]{3,}", folder_name)}


async def catalogue_embedding(db: AsyncSession, stem: str, tokens: set[str]) -> list[float] | None:
    """The stored embedding for this filename stem, if the shoot is on the NAS.

    Stems like IMG_1234 repeat across shoots, so candidates are scored by how
    many words of the Drive folder's name appear in their catalogue path —
    the shoot that shares an address wins. Ambiguity without any token overlap
    falls through to the thumbnail path rather than guessing.
    """
    escaped = _LIKE_SPECIALS.sub(r"\\\1", stem)
    rows = (
        await db.execute(
            select(Asset.relative_path, Frame.embedding)
            .join(Frame, Frame.asset_id == Asset.id)
            .where(
                Asset.media_type == "image",
                Asset.filename.ilike(f"{escaped}.%", escape="\\"),
                Frame.embedding.is_not(None),
            )
            .limit(25)
        )
    ).all()
    if not rows:
        return None
    if len(rows) == 1:
        return list(rows[0][1])

    def overlap(path: str) -> int:
        lowered = path.lower()
        return sum(1 for t in tokens if t in lowered)

    best = max(rows, key=lambda r: overlap(r[0]))
    if tokens and overlap(best[0]) == 0:
        return None
    return list(best[1])
