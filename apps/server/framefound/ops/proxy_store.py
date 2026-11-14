"""Find proxies the catalogue lists but the proxy store does not hold, and
videos that never got the proxy they should have, and queue them.

    python -m framefound.ops.proxy_store            # report
    python -m framefound.ops.proxy_store --requeue  # and queue the missing ones

The Storage page's "Make the missing proxies again" asks for the same thing
(`find` then `queue_all`, through `POST /api/v1/storage/proxies/rebuild`),
so nobody needs a terminal for it.

For after a migration that left the old proxies behind, or a proxy drive
that was replaced rather than copied. The catalogue's record of a proxy is a
row saying "ready"; nothing rechecks the file, so without this the player
simply fails for those clips.

And for after the proxy store has been away. A clip that reached the front
of the queue while it was is skipped, not failed, and nothing queues a proxy
a second time: it has no record of a proxy at all, and no proxy until
somebody asks. This is the asking.

Refuses to run while a separate proxy store is not connected: every proxy
would look missing, and the queue would fill with thousands of transcodes
for files that are sitting safely on an unplugged drive.
"""

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.models import Asset, Derivative, Library, catalogued
from framefound.storage import stores


class StoreAway(RuntimeError):
    pass


class QueueDown(RuntimeError):
    """The queue stopped taking work part-way. `queued` were taken first;
    asking again is safe (a proxy that exists is not made twice)."""

    def __init__(self, queued: int) -> None:
        super().__init__(f"The processing queue stopped answering after {queued:,} were queued.")
        self.queued = queued


@dataclass(frozen=True)
class Found:
    """What a look at the proxies found."""

    on_record: int  # proxies the catalogue lists as made
    missing: list[uuid.UUID]  # of those, the assets whose file is not there
    never_made: list[uuid.UUID]  # videos that should have a proxy and have no record of one


async def missing(db: AsyncSession, data_dir: Path) -> tuple[int, list[uuid.UUID]]:
    """(proxies on record, assets whose proxy file is not there). Every look
    at the store runs in a thread: the Storage page calls this from a
    request, and a share that has hung must stall that request only, never
    the event loop that serves every other one."""
    problem = await asyncio.to_thread(stores.proxy_store_problem)
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


async def never_made(db: AsyncSession) -> list[uuid.UUID]:
    """Videos that should have a proxy and have no finished record of one.

    In a library that makes proxies, catalogued, reachable and past its
    metadata — and with no proxy row, or one left "pending" by a worker that
    was stopped mid-transcode. One marked failed was tried and is not tried
    again here (a BRAW clip, a file ffmpeg cannot read)."""
    settled = exists().where(
        Derivative.asset_id == Asset.id,
        Derivative.kind == "proxy",
        Derivative.status.in_(("ready", "failed")),
    )
    rows = await db.execute(
        select(Asset.id)
        .join(Library, Library.id == Asset.library_id)
        .where(
            Library.generate_proxies.is_(True),
            Asset.media_type == "video",
            Asset.availability == "online",
            Asset.processing_status == "ready",
            Asset.extension != "braw",
            catalogued(),
            ~settled,
        )
        .order_by(Asset.first_indexed_at)
    )
    return list(rows.scalars().all())


async def find(db: AsyncSession, data_dir: Path) -> Found:
    """Both lists at once. `StoreAway` while a separate proxy store is not
    connected: every proxy would look missing."""
    total, absent = await missing(db, data_dir)
    return Found(on_record=total, missing=absent, never_made=await never_made(db))


def queue_all(found: Found) -> int:
    """Queue every proxy `find` found wanting; how many were queued.

    The missing ones are `requested` (a proxy that existed is wanted,
    whatever the library's switch says today); the ones never made are not,
    so their library's proxy switch is read again when the job runs and
    still decides. Blocking (each is a message to the queue): call it in a
    thread from a request. `QueueDown` when the queue stops answering."""
    from framefound.processing.tasks import generate_proxy

    queued = 0
    try:
        for asset_id in found.missing:
            generate_proxy.delay(str(asset_id), requested=True)
            queued += 1
        for asset_id in found.never_made:
            generate_proxy.delay(str(asset_id), requested=False)
            queued += 1
    except Exception as err:
        raise QueueDown(queued) from err
    return queued


async def _run(do_requeue: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            try:
                found = await find(db, settings.data_dir)
            except StoreAway as err:
                print(f"Not checked: {err}")
                return 1
    finally:
        await engine.dispose()
    where = stores.proxy_root(settings.data_dir)
    absent, skipped = found.missing, found.never_made
    print(f"{found.on_record:,} proxies on record; {len(absent):,} missing from {where}.")
    print(f"{len(skipped):,} videos never got the proxy their library asks for.")
    if do_requeue and (absent or skipped):
        try:
            queued = queue_all(found)
        except QueueDown as err:
            print(f"{err} Run the same command again to queue the rest.")
            return 1
        print(f"Queued {queued:,} to be made (media queue).")
    elif absent or skipped:
        print("Run again with --requeue to make them.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--requeue", action="store_true", help="queue the missing proxies")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.requeue))


if __name__ == "__main__":
    sys.exit(main())
