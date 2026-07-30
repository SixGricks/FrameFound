"""Benchmarks that measure the running install, not a synthetic one.

`python -m framefound.ops.benchmark`

M8 asks for large-library benchmarks. The useful version of that is not a
figure from a laptop with warm caches — it is what this deployment actually
does, on this hardware, against this storage, with the real catalogue. Every
number here is measured against whatever the database holds when it runs.

Reported as p50/p95 rather than an average, because an average hides the case
the operator complains about. Search that is usually 40 ms and occasionally
4 seconds is a slow search.
"""

import asyncio
import json
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.models import (
    Asset,
    AssetTag,
    Derivative,
    Frame,
    Tag,
    Transcript,
    TranscriptSegment,
)

log = structlog.get_logger()

# Enough repeats to see a p95 without turning a benchmark into a load test.
REPEATS = 12


@dataclass
class Timing:
    name: str
    runs: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    note: str = ""


@dataclass
class Report:
    scale: dict[str, int] = field(default_factory=dict)
    timings: list[Timing] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


async def _time(
    name: str, work: Callable[[], Awaitable[Any]], note: str = "", repeats: int = REPEATS
) -> Timing:
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        await work()
        samples.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples)
    return Timing(
        name=name,
        runs=len(samples),
        p50_ms=round(statistics.median(ordered), 1),
        p95_ms=round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1),
        max_ms=round(max(ordered), 1),
        note=note,
    )


async def _scale(db: AsyncSession) -> dict[str, int]:
    async def count(model: Any, *where: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return int((await db.execute(stmt)).scalar_one())

    return {
        "assets": await count(Asset),
        "assets_online": await count(Asset, Asset.availability == "online"),
        "frames": await count(Frame),
        "frames_embedded": await count(Frame, Frame.embedding.is_not(None)),
        "derivatives_ready": await count(Derivative, Derivative.status == "ready"),
        "transcripts": await count(Transcript),
        "tags": await count(Tag),
        "tag_links": await count(AssetTag),
        "located": await count(Asset, Asset.gps_lat.is_not(None)),
    }


async def run(db: AsyncSession) -> Report:
    report = Report(scale=await _scale(db))

    # Browse: the first thing anyone does, and a full-table count under it.
    report.timings.append(
        await _time(
            "browse page 1 (50 assets, sorted)",
            lambda: db.execute(
                select(Asset)
                .where(Asset.availability == "online")
                .order_by(Asset.mtime.desc())
                .limit(50)
            ),
        )
    )
    report.timings.append(
        await _time(
            "asset count",
            lambda: db.execute(select(func.count()).select_from(Asset)),
            note="drives the pagination control",
        )
    )

    # A deep page: offset pagination degrades with depth, and this is where it
    # shows if it is going to.
    report.timings.append(
        await _time(
            "browse page 100 (offset 4950)",
            lambda: db.execute(
                select(Asset)
                .where(Asset.availability == "online")
                .order_by(Asset.mtime.desc())
                .offset(4950)
                .limit(50)
            ),
        )
    )

    report.timings.append(
        await _time(
            "filename search (ILIKE)",
            lambda: db.execute(select(Asset).where(Asset.filename.ilike("%dji%")).limit(40)),
            note="no trigram index — expected to scale linearly",
        )
    )

    # Transcript search, if there is anything to search.
    if report.scale["transcripts"]:
        report.timings.append(
            await _time(
                "transcript phrase search",
                lambda: db.execute(
                    select(TranscriptSegment.transcript_id, TranscriptSegment.start_ms)
                    .join(Transcript, Transcript.id == TranscriptSegment.transcript_id)
                    .where(TranscriptSegment.text.ilike("%the%"))
                    .limit(40)
                ),
            )
        )

    # Vector search: the one that matters for M5, and the one most likely to
    # regress silently when the index is missing or unused.
    if report.scale["frames_embedded"] > 10:
        sample = (
            await db.execute(select(Frame.embedding).where(Frame.embedding.is_not(None)).limit(1))
        ).scalar_one()

        report.timings.append(
            await _time(
                "visual similarity (top 40)",
                lambda: db.execute(
                    select(Frame.asset_id)
                    .where(Frame.embedding.is_not(None))
                    .order_by(Frame.embedding.cosine_distance(sample))
                    .limit(40)
                ),
                note="HNSW index on frames.embedding",
            )
        )

        # `:v::vector` because a bind parameter arrives as text and pgvector's
        # <=> has no operator for `vector <=> varchar`. The ORM path above
        # handles this through the type decorator; raw SQL has to say it.
        plan = (
            await db.execute(
                text(
                    "EXPLAIN SELECT asset_id FROM frames WHERE embedding IS NOT NULL "
                    "ORDER BY embedding <=> :v::vector LIMIT 40"
                ).bindparams(v=str(sample))
            )
        ).all()
        plan_text = " ".join(str(row[0]) for row in plan)
        if "Index Scan" not in plan_text:
            # A sequential scan here means the HNSW index is not being used,
            # which is invisible until the library is large enough to hurt.
            report.warnings.append(
                "Vector search is NOT using an index scan — check the HNSW index "
                f"on frames.embedding. Plan: {plan_text[:200]}"
            )

    # Places clusters on every request, so its cost grows with located assets.
    if report.scale["located"] > 10:
        report.timings.append(
            await _time(
                "load located assets (places input)",
                lambda: db.execute(
                    select(Asset.id, Asset.gps_lat, Asset.gps_lon, Asset.relative_path).where(
                        Asset.gps_lat.is_not(None), Asset.availability == "online"
                    )
                ),
                note="clustering runs on this in Python",
                repeats=5,
            )
        )

    # Duplicate detection: a self-join over the whole table.
    report.timings.append(
        await _time(
            "duplicate grouping (identical)",
            lambda: db.execute(
                select(Asset.partial_hash, Asset.size_bytes, func.count())
                .where(Asset.partial_hash.is_not(None), Asset.availability == "online")
                .group_by(Asset.partial_hash, Asset.size_bytes)
                .having(func.count() > 1)
            ),
            repeats=5,
        )
    )

    for timing in report.timings:
        # A page render budget of ~200 ms leaves room for the network and the
        # browser; anything past that is worth knowing about before a user
        # reports it.
        if timing.p95_ms > 200:
            report.warnings.append(
                f"{timing.name}: p95 {timing.p95_ms} ms exceeds the 200 ms budget"
            )
    return report


async def main() -> None:
    engine = create_async_engine(get_settings().db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            report = await run(db)
    finally:
        await engine.dispose()

    print(json.dumps({"scale": report.scale}, indent=2))
    print(f"\n{'query':<44} {'p50':>9} {'p95':>9} {'max':>9}")
    print("-" * 74)
    for timing in report.timings:
        print(
            f"{timing.name:<44} {timing.p50_ms:>8.1f}ms {timing.p95_ms:>8.1f}ms "
            f"{timing.max_ms:>8.1f}ms"
        )
        if timing.note:
            print(f"{'':<44} {timing.note}")
    if report.warnings:
        print("\nWARNINGS")
        for warning in report.warnings:
            print(f"  - {warning}")
    else:
        print("\nNo warnings: every query inside the 200 ms budget.")
    print(
        json.dumps({"timings": [asdict(t) for t in report.timings]}, indent=2),
        file=__import__("sys").stderr,
    )


if __name__ == "__main__":
    asyncio.run(main())
