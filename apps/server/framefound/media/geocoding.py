"""Reverse geocoding via Google, with a cache that outlives the process.

Only ever called for places the folder structure could not name. The
operator's own directories say "Feb 4 - 513 Jacobs Rd"; a gazetteer says
"Lancaster, Pennsylvania". The folder wins wherever it exists, so this fills
gaps rather than replacing anything.

Results are cached in the database keyed on a rounded coordinate. Clusters
move slightly as assets are added, and an unrounded key would miss on every
recomputation and re-bill the same lookup forever. Five decimal places is
about a metre — far tighter than a shoot, so two clusters that are really the
same place still share a cache entry.

Failures are cached too, briefly. A quota error or an unroutable coordinate
would otherwise be retried on every page load.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import GeocodeCache

log = structlog.get_logger()

ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"
KEY_PRECISION = 5
TIMEOUT_S = 8.0
FAILURE_RETRY_AFTER = timedelta(hours=6)
# Google's own guidance is 50 requests/second; nothing here approaches that,
# but a library with many unnamed clusters should not arrive all at once.
MAX_CONCURRENT = 4


def cache_key(lat: float, lon: float) -> str:
    return f"{round(lat, KEY_PRECISION)},{round(lon, KEY_PRECISION)}"


def _short_address(payload: dict[str, Any]) -> str:
    """Street address if Google offers one, else its best formatted result.

    `formatted_address` on the first result is usually the most precise, but
    for a coordinate in the middle of a field it can be a plus-code. A
    street_address or premise result reads far better on a card.
    """
    results: list[dict[str, Any]] = payload.get("results") or []
    for wanted in ("street_address", "premise", "subpremise"):
        for result in results:
            if wanted in (result.get("types") or []):
                return str(result.get("formatted_address") or "")
    for result in results:
        formatted = str(result.get("formatted_address") or "")
        # Plus-codes are worse than saying nothing useful.
        if formatted and "+" not in formatted.split(",")[0]:
            return formatted
    return ""


async def _lookup_one(client: httpx.AsyncClient, lat: float, lon: float, api_key: str) -> str:
    response = await client.get(
        ENDPOINT,
        params={"latlng": f"{lat},{lon}", "key": api_key},
        timeout=TIMEOUT_S,
    )
    response.raise_for_status()
    payload = response.json()
    status = payload.get("status")
    if status == "ZERO_RESULTS":
        return ""
    if status != "OK":
        raise RuntimeError(f"Google geocoding returned {status}")
    return _short_address(payload)


async def reverse_geocode_many(
    db: AsyncSession, coordinates: list[tuple[float, float]], api_key: str
) -> dict[str, str]:
    """Addresses for the given coordinates, keyed by `cache_key`.

    Everything already known comes from the database; only genuine misses go
    to Google. Missing entries simply do not appear in the result, so callers
    fall back to whatever name they already had.
    """
    wanted = {cache_key(lat, lon): (lat, lon) for lat, lon in coordinates}
    if not wanted:
        return {}

    rows = (
        (await db.execute(select(GeocodeCache).where(GeocodeCache.cache_key.in_(wanted))))
        .scalars()
        .all()
    )
    known = {row.cache_key: row for row in rows}
    now = datetime.now(UTC)

    resolved: dict[str, str] = {}
    to_fetch: list[tuple[str, float, float]] = []
    for key, (lat, lon) in wanted.items():
        row = known.get(key)
        if row is None:
            to_fetch.append((key, lat, lon))
            continue
        if row.address:
            resolved[key] = row.address
            continue
        fetched = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=UTC)
        if now - fetched > FAILURE_RETRY_AFTER:
            to_fetch.append((key, lat, lon))

    if not to_fetch:
        return resolved

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async with httpx.AsyncClient() as client:

        async def fetch(key: str, lat: float, lon: float) -> tuple[str, str | None]:
            async with semaphore:
                try:
                    return key, await _lookup_one(client, lat, lon, api_key)
                except Exception as exc:
                    log.warning("geocode.failed", key=key, error=str(exc)[:200])
                    return key, None

        results = await asyncio.gather(*(fetch(k, la, lo) for k, la, lo in to_fetch))

    for key, address in results:
        if address is None:
            continue  # transport failure: do not poison the cache
        row = known.get(key)
        if row is None:
            db.add(GeocodeCache(cache_key=key, address=address, fetched_at=now))
        else:
            row.address = address
            row.fetched_at = now
        if address:
            resolved[key] = address
    await db.commit()
    return resolved
