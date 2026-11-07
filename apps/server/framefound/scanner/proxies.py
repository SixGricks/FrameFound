"""Editing proxies paired with their originals (migration 0028, ADR-0021).

Premiere's ingest writes a proxy beside each clip it imports:
`Pete Dye/2025 10 28/Proxies/MVI_4513_Proxy.mp4` for
`Pete Dye/2025 10 28/MVI_4513.MP4`. Catalogued as a file in its own right it
came back from every search next to its original, carried the day the proxy
was rendered as its capture date (2026-02-12 for a 2025-10-28 shoot), and
cost a second transcription, embedding and tag pass.

A proxy whose original is catalogued keeps its row, with `proxy_of_id`
pointing at the original. That row is how the original gets decoded from the
smaller file (processing/tasks.py), and it is otherwise out of the catalogue.
A proxy whose original is *not* there is the only copy of that footage, so it
stays an ordinary asset — and becomes one again if its original later goes.

The rule lives here, once, for the scanner, the watcher and the backfill
(`ops/pair_proxies.py`). Nothing here touches a file: pairing is a fact about
two rows, and the share stays read-only.
"""

import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import PAIRED_PROXY_STATUS, Asset, AssetTag, Library
from framefound.media.detect import media_type_for

log = structlog.get_logger()

DEFAULT_PATTERN = "Proxies/{stem}_Proxy"
STEM = "{stem}"
# A human decision outranks a machine's guess when the original already has
# a row for the same tag: the operator said it about this footage.
DECISIONS = ("manual", "confirmed", "rejected")


@dataclass(frozen=True)
class ProxyPattern:
    """`Proxies/{stem}_Proxy`, parsed: the folder beside the original, and
    what Premiere puts around the original's stem. Matched case-insensitively,
    with any video extension — Premiere writes `.mp4` for an `.MP4` and
    `.mov` for ProRes presets."""

    folder: str  # "" for proxies in the original's own folder
    prefix: str
    suffix: str

    @classmethod
    def parse(cls, raw: str | None) -> "ProxyPattern | None":
        """None when pairing is off (an empty setting). Raises ValueError on a
        pattern that could never match, so a library cannot save one."""
        text = (raw or "").strip().strip("/")
        if not text:
            return None
        folder, _, name = text.rpartition("/")
        if "/" in folder or "\\" in text:
            raise ValueError("A proxy pattern is one folder at most: Proxies/{stem}_Proxy")
        if name.count(STEM) != 1:
            raise ValueError("A proxy pattern names the original's stem once, as {stem}")
        prefix, _, suffix = name.partition(STEM)
        if not (folder or prefix or suffix):
            raise ValueError("A proxy pattern of just {stem} would match every original")
        return cls(folder=folder, prefix=prefix, suffix=suffix)


@dataclass(frozen=True)
class ProxyMatch:
    original_dir: str  # relative folder of the original; "" at the library root
    stem: str  # the original's stem as the proxy spells it


def match_proxy(relative_path: str, pattern: ProxyPattern | None) -> ProxyMatch | None:
    """Whether a catalogued path is an editing proxy, and of what.

    Only video counts: Premiere proxies are video, and a still or a WAV that
    happens to sit in a `Proxies` folder is somebody's actual file.
    """
    if pattern is None or media_type_for(relative_path) != "video":
        return None
    path = PurePosixPath(relative_path)
    parent = path.parent
    if pattern.folder:
        if parent.name.casefold() != pattern.folder.casefold():
            return None
        original_dir = parent.parent
    else:
        original_dir = parent
    stem = path.stem
    folded = stem.casefold()
    prefix, suffix = pattern.prefix.casefold(), pattern.suffix.casefold()
    if not (folded.startswith(prefix) and folded.endswith(suffix)):
        return None
    inner = stem[len(pattern.prefix) : len(stem) - len(pattern.suffix)]
    if not inner:
        return None
    return ProxyMatch(
        original_dir="" if str(original_dir) == "." else str(original_dir), stem=inner
    )


def is_proxy_path(relative_path: str, library: Library) -> bool:
    try:
        return match_proxy(relative_path, ProxyPattern.parse(library.proxy_pattern)) is not None
    except ValueError:
        return False


@dataclass
class TagMove:
    tag_id: uuid.UUID
    source: str
    action: str  # "move" | "upgrade" (proxy's decision replaces a suggestion) | "drop"
    original_source: str | None = None


@dataclass
class Pair:
    proxy: Asset
    original: Asset
    tag_moves: list[TagMove] = field(default_factory=list)


@dataclass
class PairingPlan:
    library: Library
    link: list[Pair] = field(default_factory=list)  # newly paired
    unlink: list[Asset] = field(default_factory=list)  # original gone: back in the catalogue
    unpaired: list[Asset] = field(default_factory=list)  # proxy-shaped, no original
    already: int = 0  # correctly paired before this run

    @property
    def changes(self) -> int:
        return len(self.link) + len(self.unlink)


async def _find_original(
    db: AsyncSession, library: Library, match: ProxyMatch, pattern: ProxyPattern
) -> Asset | None:
    """The online original a proxy stands for: same library, the folder above
    `Proxies`, the same stem, any video extension, any case."""
    rows = (
        (
            await db.execute(
                select(Asset).where(
                    Asset.library_id == library.id,
                    Asset.media_type == "video",
                    Asset.availability == "online",
                    Asset.proxy_of_id.is_(None),
                    Asset.filename.ilike(f"{_escape(match.stem)}.%", escape="\\"),
                )
            )
        )
        .scalars()
        .all()
    )
    wanted = match.stem.casefold()
    for row in sorted(rows, key=lambda a: a.relative_path):
        path = PurePosixPath(row.relative_path)
        folder = "" if str(path.parent) == "." else str(path.parent)
        if folder != match.original_dir or path.stem.casefold() != wanted:
            continue
        if match_proxy(row.relative_path, pattern) is not None:
            continue  # a proxy of a proxy is not an original
        return row
    return None


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def plan_tag_moves(
    db: AsyncSession, proxy_id: uuid.UUID, original_id: uuid.UUID
) -> list[TagMove]:
    """What happens to each tag row on the proxy when it pairs.

    The operator's decisions (manual, confirmed, rejected) follow the footage
    to the original. Where the original already has the tag, a decision on the
    proxy replaces a mere suggestion on the original; the original's own
    decision stands otherwise, and the proxy's row is dropped as redundant.
    A suggestion on the proxy moves only where the original has nothing.
    """
    on_proxy = (await db.execute(select(AssetTag).where(AssetTag.asset_id == proxy_id))).scalars()
    on_original = {
        row.tag_id: row.source
        for row in (
            await db.execute(select(AssetTag).where(AssetTag.asset_id == original_id))
        ).scalars()
    }
    moves = []
    for row in on_proxy:
        theirs = on_original.get(row.tag_id)
        if theirs is None:
            action = "move"
        elif row.source in DECISIONS and theirs not in DECISIONS:
            action = "upgrade"
        else:
            action = "drop"
        moves.append(TagMove(row.tag_id, row.source, action, theirs))
    return moves


async def plan_pairing(
    db: AsyncSession, library: Library, candidates: list[Asset] | None = None
) -> PairingPlan:
    """Work out what pairing would change in one library. Writes nothing.

    `candidates` narrows it to some assets (the watcher's one new file);
    otherwise every proxy-shaped video in the library, plus every row already
    linked, is checked again — that is what keeps a rescan stable and lets a
    deleted original release its proxy.
    """
    plan = PairingPlan(library=library)
    try:
        pattern = ProxyPattern.parse(library.proxy_pattern)
    except ValueError:
        log.warning("proxies.bad_pattern", library=library.name, pattern=library.proxy_pattern)
        pattern = None

    if candidates is None:
        query = select(Asset).where(Asset.library_id == library.id, Asset.media_type == "video")
        if pattern is not None:
            shape = f"%{_escape(pattern.suffix)}.%"
            query = query.where(
                Asset.relative_path.ilike(shape, escape="\\") | Asset.proxy_of_id.is_not(None)
            )
        else:
            query = query.where(Asset.proxy_of_id.is_not(None))
        candidates = list((await db.execute(query)).scalars().all())

    for asset in candidates:
        match = match_proxy(asset.relative_path, pattern)
        original = (
            await _find_original(db, library, match, pattern)
            if match is not None and pattern is not None and asset.availability == "online"
            else None
        )
        if original is None:
            if asset.proxy_of_id is not None:
                plan.unlink.append(asset)
            elif match is not None:
                plan.unpaired.append(asset)
        elif asset.proxy_of_id == original.id:
            plan.already += 1
        else:
            plan.link.append(Pair(asset, original, await plan_tag_moves(db, asset.id, original.id)))
    return plan


async def apply_pairing(db: AsyncSession, plan: PairingPlan) -> list[uuid.UUID]:
    """Make the plan so. Returns the assets that now need processing: proxies
    released back into the catalogue, and proxy-shaped files with no original
    that the scan held back from the queue. The caller commits."""
    for pair in plan.link:
        # Recomputed against the rows as they now are: two proxies of one
        # original (an .mp4 and a .mov preset) would otherwise both "move"
        # the same tag onto it. The plan's own list is the report.
        await db.flush()
        pair.tag_moves = await plan_tag_moves(db, pair.proxy.id, pair.original.id)
        rows = {
            row.tag_id: row
            for row in (
                await db.execute(select(AssetTag).where(AssetTag.asset_id == pair.proxy.id))
            ).scalars()
        }
        theirs = {
            row.tag_id: row
            for row in (
                await db.execute(select(AssetTag).where(AssetTag.asset_id == pair.original.id))
            ).scalars()
        }
        for move in pair.tag_moves:
            row = rows[move.tag_id]
            if move.action == "move":
                row.asset_id = pair.original.id
            elif move.action == "upgrade":
                target = theirs[move.tag_id]
                target.source = row.source
                target.confidence = row.confidence
                target.origin = row.origin
                target.created_by = row.created_by
                await db.delete(row)
            else:
                await db.delete(row)
        pair.proxy.proxy_of_id = pair.original.id
        pair.proxy.processing_status = PAIRED_PROXY_STATUS
        log.info(
            "proxies.paired",
            proxy=pair.proxy.relative_path,
            original=pair.original.relative_path,
            tags_moved=sum(m.action != "drop" for m in pair.tag_moves),
        )

    requeue: list[uuid.UUID] = []
    for proxy in plan.unlink:
        proxy.proxy_of_id = None
        if proxy.availability == "online":
            proxy.processing_status = "pending"
            requeue.append(proxy.id)
        log.info("proxies.released", proxy=proxy.relative_path, reason="original not catalogued")
    for proxy in plan.unpaired:
        if proxy.processing_status == "pending" and proxy.availability == "online":
            requeue.append(proxy.id)
    await db.flush()
    return requeue


async def reconcile(
    db: AsyncSession, library: Library, candidates: list[Asset] | None = None
) -> tuple[PairingPlan, list[uuid.UUID]]:
    plan = await plan_pairing(db, library, candidates)
    return plan, await apply_pairing(db, plan)
