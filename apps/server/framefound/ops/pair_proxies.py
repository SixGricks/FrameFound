"""Pair the editing proxies already catalogued as assets with their originals.

    python -m framefound.ops.pair_proxies                    # report: what would change
    python -m framefound.ops.pair_proxies --library GELCO    # one library
    python -m framefound.ops.pair_proxies --apply            # do it

Before migration 0028 the scanner indexed Premiere's
`Proxies/<stem>_Proxy.<ext>` files as assets of their own. New scans pair
them as they go (scanner/proxies.py); this is for the rows already there.

Nothing is deleted. Each proxy row stays, linked to its original, which takes
it out of search, the tag learner, the review queues and the transcription
and embedding sweeps. Tag decisions made on a proxy move to the original
(a decision replaces a suggestion there; the original's own decision stands).
Its transcript, frames and vectors stay attached to the proxy row, unread, so
releasing the pair later loses nothing. A proxy whose original is not
catalogued is reported and left alone: it is the only copy.

One transaction for the lot, and the share is never touched. Running it with
the scanner up is safe — a scan finishing meanwhile makes the same decisions.
"""

import argparse
import asyncio
import sys
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.models import AuditLog, Library, Tag
from framefound.scanner.proxies import PairingPlan, apply_pairing, plan_pairing

SAMPLE = 10


async def plan_all(db: AsyncSession, library_name: str | None = None) -> list[PairingPlan]:
    query = select(Library).order_by(Library.name)
    if library_name:
        query = query.where(Library.name == library_name)
    libraries = (await db.execute(query)).scalars().all()
    if library_name and not libraries:
        raise SystemExit(f"No library called {library_name!r}")
    return [await plan_pairing(db, library) for library in libraries]


async def describe(db: AsyncSession, plans: list[PairingPlan]) -> str:
    names = {tag.id: tag.name for tag in (await db.execute(select(Tag))).scalars()}
    lines: list[str] = []
    for plan in plans:
        lines.append(
            f"{plan.library.name} (pattern {plan.library.proxy_pattern or 'off'!r}): "
            f"{len(plan.link)} to pair, {plan.already} already paired, "
            f"{len(plan.unlink)} to release, {len(plan.unpaired)} with no original (left alone)"
        )
        for pair in plan.link[:SAMPLE]:
            lines.append(f"  {pair.proxy.relative_path}")
            lines.append(f"    -> {pair.original.relative_path}")
            if pair.proxy.captured_at != pair.original.captured_at:
                lines.append(
                    f"       dated {_day(pair.proxy.captured_at)} as a proxy, "
                    f"{_day(pair.original.captured_at)} as the original"
                )
            for move in pair.tag_moves:
                lines.append(
                    f"       tag {names.get(move.tag_id, move.tag_id)!s} ({move.source}): "
                    + {
                        "move": "moves to the original",
                        "upgrade": f"replaces the original's {move.original_source}",
                        "drop": f"dropped; the original already has it ({move.original_source})",
                    }[move.action]
                )
        if len(plan.link) > SAMPLE:
            lines.append(f"  … and {len(plan.link) - SAMPLE} more")
        for proxy in plan.unpaired[:SAMPLE]:
            lines.append(f"  no original: {proxy.relative_path}")
        for proxy in plan.unlink[:SAMPLE]:
            lines.append(f"  original gone, released: {proxy.relative_path}")
        moves = Counter(m.action for pair in plan.link for m in pair.tag_moves)
        if moves:
            lines.append(
                "  tags: " + ", ".join(f"{n} {action}" for action, n in sorted(moves.items()))
            )
    return "\n".join(lines)


def _day(when: object) -> str:
    return when.date().isoformat() if hasattr(when, "date") else "undated"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m framefound.ops.pair_proxies")
    parser.add_argument("--library", help="Only this library (by name)")
    parser.add_argument("--apply", action="store_true", help="Make the changes (default: report)")
    args = parser.parse_args(argv)

    engine = create_async_engine(get_settings().db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            plans = await plan_all(db, args.library)
            print(await describe(db, plans))
            changes = sum(plan.changes for plan in plans)
            if not args.apply:
                print(f"\nDry run: {changes} change(s). Re-run with --apply to make them.")
                return 0
            if not changes:
                print("\nNothing to change.")
                return 0
            requeue = []
            for plan in plans:
                requeue += await apply_pairing(db, plan)
            db.add(
                AuditLog(
                    event="proxies.paired",
                    detail={
                        "paired": sum(len(p.link) for p in plans),
                        "released": sum(len(p.unlink) for p in plans),
                        "libraries": [p.library.name for p in plans if p.changes],
                    },
                )
            )
            await db.commit()
        print(f"\nApplied {changes} change(s).")
        if requeue:
            # Released proxies need processing as assets in their own right;
            # the scanner's stuck-asset sweep picks up anything left `pending`
            # within ten minutes, so this does not need a broker of its own.
            print(f"{len(requeue)} released proxy(ies) will be processed by the next sweep.")
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
