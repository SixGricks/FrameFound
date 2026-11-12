"""Find proxies the catalogue lists but the proxy store does not hold, and
queue them to be made again.

    python -m framefound.ops.proxy_store            # report
    python -m framefound.ops.proxy_store --requeue  # and queue the missing ones

For after a migration that left the old proxies behind, or a proxy drive
that was replaced rather than copied. The catalogue's record of a proxy is a
row saying "ready"; nothing rechecks the file, so without this the player
simply fails for those clips.

Refuses to run while a separate proxy store is not connected: every proxy
would look missing, and the queue would fill with thousands of transcodes
for files that are sitting safely on an unplugged drive.
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.models import Derivative
from framefound.storage import stores


class StoreAway(RuntimeError):
    pass


async def missing(db: AsyncSession, data_dir: Path) -> tuple[int, list[uuid.UUID]]:
    """(proxies on record, assets whose proxy file is not there)."""
    problem = stores.proxy_store_problem()
    if problem is not None:
        raise StoreAway(problem)
    rows = (
        await db.execute(
            select(Derivative.asset_id, Derivative.relative_path).where(
                Derivative.kind == "proxy", Derivative.status == "ready"
            )
        )
    ).all()

    def absent() -> list[uuid.UUID]:
        return [
            asset_id
            for asset_id, relative_path in rows
            if not stores.derivative_path(data_dir, relative_path).is_file()
        ]

    return len(rows), await asyncio.to_thread(absent)


def requeue(asset_ids: list[uuid.UUID]) -> int:
    """Queue each proxy again. `requested`: a proxy that existed is wanted,
    whatever the library's proxy switch says today."""
    from framefound.processing.tasks import generate_proxy

    for asset_id in asset_ids:
        generate_proxy.delay(str(asset_id), requested=True)
    return len(asset_ids)


async def _run(do_requeue: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            try:
                total, absent = await missing(db, settings.data_dir)
            except StoreAway as err:
                print(f"Not checked: {err}")
                return 1
    finally:
        await engine.dispose()
    where = stores.proxy_root(settings.data_dir)
    print(f"{total:,} proxies on record; {len(absent):,} missing from {where}.")
    if absent and do_requeue:
        print(f"Queued {requeue(absent):,} to be made again (media queue).")
    elif absent:
        print("Run again with --requeue to make them again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--requeue", action="store_true", help="queue the missing proxies")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.requeue))


if __name__ == "__main__":
    sys.exit(main())
