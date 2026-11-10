"""Shot intelligence, by hand: backfill, re-describe, import a past cut.

    python -m framefound.ops.shots backfill --library GELCO --folder "Brookside" --make-proxies
    python -m framefound.ops.shots backfill --library GELCO --folder "Brookside" --inline
    python -m framefound.ops.shots describe --library GELCO
    python -m framefound.ops.shots import-cut "Rev 2.xml" --library GELCO --title "..."
    python -m framefound.ops.shots inspect /media/gelco/.../DJI_0639_D.MP4

`backfill` finds the videos in scope that have no shot windows (or windows
from an older analysis) and queues the motion pass for each. The motion
pass reads only proxies, so `--make-proxies` first queues a proxy for every
clip in scope without one — even where the library has proxies switched
off, which is the point: a course about to be edited, not the whole
archive. `--inline` runs the analysis here and now, one clip at a time,
and prints how fast it went (the figure for docs/benchmarks.md).

`import-cut` reads an FCP7 XML timeline (what build_rough_cut writes, or a
Premiere export of one) into cut history, so a video cut before history
existed still counts for recent_cuts and find_shots' `used` flag.

`inspect` analyses files directly, with no database: the windows and their
smoothness per speed, for checking the analysis against footage by eye.
"""

import argparse
import asyncio
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET  # noqa: S405 - the operator's own timeline file
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.models import Asset, Derivative, Library, ShotWindow, catalogued
from framefound.media import motion


async def _library(db: AsyncSession, name: str) -> Library:
    found = (
        await db.execute(select(Library).where(func.lower(Library.name) == name.strip().lower()))
    ).scalar_one_or_none()
    if found is None:
        names = ", ".join(sorted((await db.execute(select(Library.name))).scalars().all()))
        raise SystemExit(f"No library called {name!r}. Libraries: {names}")
    return found


def _scope(library: Library, folder: str | None) -> list[Any]:
    conditions: list[Any] = [
        Asset.library_id == library.id,
        Asset.media_type == "video",
        Asset.availability == "online",
        Asset.extension != "braw",
        # A paired editing proxy is the original's footage again (ADR-0021).
        catalogued(),
    ]
    if folder:
        conditions.append(func.lower(Asset.relative_path).contains(folder.lower(), autoescape=True))
    return conditions


async def backfill(args: argparse.Namespace) -> int:
    from framefound.processing import shots

    engine = create_async_engine(get_settings().db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            library = await _library(db, args.library)
            has_proxy = exists().where(
                Derivative.asset_id == Asset.id,
                Derivative.kind == "proxy",
                Derivative.status == "ready",
            )
            current = exists().where(
                ShotWindow.asset_id == Asset.id, ShotWindow.analysis_version >= motion.VERSION
            )
            has_motion = exists().where(
                Derivative.asset_id == Asset.id,
                Derivative.kind == "motion",
                Derivative.status == "ready",
            )
            scope = _scope(library, args.folder)
            todo = [] if args.redo else [~(has_motion & current)]
            ready = (
                (
                    await db.execute(
                        select(Asset)
                        .where(*scope, has_proxy, *todo)
                        .order_by(Asset.relative_path)
                        .limit(args.limit or None)
                    )
                )
                .scalars()
                .all()
            )
            missing = (
                (
                    await db.execute(
                        select(Asset.id).where(*scope, ~has_proxy).order_by(Asset.relative_path)
                    )
                )
                .scalars()
                .all()
            )
            total = (
                await db.execute(select(func.count()).select_from(Asset).where(*scope))
            ).scalar_one()
            print(
                f"{library.name}{' / ' + args.folder if args.folder else ''}: {total} videos, "
                f"{len(ready)} with a proxy to analyse, {len(missing)} without a proxy."
            )
            if args.make_proxies and missing:
                from framefound.processing.tasks import generate_proxy

                for asset_id in missing[: args.limit or None]:
                    generate_proxy.delay(str(asset_id), requested=True)
                print(
                    f"Queued {min(len(missing), args.limit or len(missing))} proxies "
                    "(media queue); each one queues its motion pass when it is done."
                )
            if not ready:
                return 0
            if not args.inline:
                from framefound.processing.tasks import analyze_motion

                for asset in ready:
                    analyze_motion.delay(str(asset.id))
                print(f"Queued the motion pass for {len(ready)} videos (frames queue).")
                return 0
            libraries = {library.id: library}
            footage = spent = 0.0
            print("clip, seconds of footage, seconds taken, x real time, windows")
            for asset in ready:
                lib = libraries[asset.library_id]
                started = time.monotonic()
                try:
                    count = await shots.analyse_motion(db, asset, lib, Path(lib.root_path))
                except Exception as err:  # noqa: BLE001 - report and carry on
                    await db.rollback()
                    print(f"{asset.filename}: failed ({err})")
                    continue
                taken = time.monotonic() - started
                footage += asset.duration_s or 0.0
                spent += taken
                speed = (asset.duration_s or 0.0) / taken if taken else 0.0
                length = asset.duration_s or 0.0
                print(f"{asset.filename}, {length:.1f}, {taken:.1f}, {speed:.2f}, {count}")
                if await shots.has_embeddings(db, asset.id):
                    await shots.describe_shots(db, asset)
            if spent:
                print(
                    f"\n{len(ready)} clips, {footage:.0f} s of footage in {spent:.0f} s: "
                    f"{footage / spent:.2f}x real time."
                )
            return 0
    finally:
        await engine.dispose()


async def describe(args: argparse.Namespace) -> int:
    from framefound.processing import shots

    engine = create_async_engine(get_settings().db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            library = await _library(db, args.library)
            has_windows = exists().where(ShotWindow.asset_id == Asset.id)
            ids = (
                (
                    await db.execute(
                        select(Asset.id).where(*_scope(library, args.folder), has_windows)
                    )
                )
                .scalars()
                .all()
            )
            if args.inline:
                for asset_id in ids:
                    asset = await db.get(Asset, asset_id)
                    if asset is not None:
                        await shots.describe_shots(db, asset)
                print(f"Described {len(ids)} videos.")
                return 0
            from framefound.processing.tasks import describe_shots

            for asset_id in ids:
                describe_shots.delay(str(asset_id))
            print(f"Queued descriptors for {len(ids)} videos (vision queue).")
            return 0
    finally:
        await engine.dispose()


# --- importing a timeline ------------------------------------------------------


@dataclass
class TimelinePiece:
    name: str
    path: str
    in_s: float
    out_s: float


def _rate(node: ET.Element | None) -> float:
    if node is None:
        return 30.0
    timebase = float(node.findtext("timebase") or 30)
    ntsc = (node.findtext("ntsc") or "").upper() == "TRUE"
    return timebase * 1000 / 1001 if ntsc else timebase


def parse_timeline(text: str) -> tuple[str, list[TimelinePiece]]:
    """The sequence's name and its video pieces, in timeline order, from
    FCP7 xmeml. In and out are counted in the clip item's own rate, which
    is how Premiere reads them."""
    root = ET.fromstring(text)  # noqa: S314 - the operator's own timeline file
    sequence = root.find("sequence") if root.tag == "xmeml" else root
    if sequence is None:
        raise ValueError("No <sequence> in that file")
    name = sequence.findtext("name") or ""
    files: dict[str, tuple[str, str]] = {}
    pieces: list[tuple[float, TimelinePiece]] = []
    for track in sequence.findall("./media/video/track"):
        for item in track.findall("clipitem"):
            file = item.find("file")
            if file is None:
                continue
            file_id = file.get("id", "")
            file_name = file.findtext("name")
            pathurl = file.findtext("pathurl")
            if file_name or pathurl:
                files[file_id] = (file_name or "", pathurl or "")
            fname, url = files.get(file_id, (item.findtext("name") or "", ""))
            rate = _rate(item.find("rate"))
            start = float(item.findtext("start") or 0)
            in_f = float(item.findtext("in") or 0)
            out_f = float(item.findtext("out") or 0)
            path = urllib.parse.unquote(
                url.removeprefix("file://localhost").removeprefix("file://")
            )
            pieces.append((start, TimelinePiece(fname, path, in_f / rate, out_f / rate)))
    pieces.sort(key=lambda p: p[0])
    return name, [p for _, p in pieces]


async def import_cut(args: argparse.Namespace) -> int:
    from framefound.mcp.shots import record_cut

    source = Path(args.xml)
    name, pieces = parse_timeline(source.read_text(encoding="utf-8"))
    title = args.title or name or source.stem
    when = (
        datetime.fromisoformat(args.date)
        if args.date
        else datetime.fromtimestamp(source.stat().st_mtime, UTC)
    )
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    engine = create_async_engine(get_settings().db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            library = await _library(db, args.library)
            found: list[tuple[Asset, float, float, float]] = []
            unmatched: list[str] = []
            for piece in pieces:
                candidates = (
                    (
                        await db.execute(
                            select(Asset).where(
                                Asset.library_id == library.id, Asset.filename == piece.name
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                # Same name in two folders: the one whose path ends the
                # timeline's path wins.
                chosen = next(
                    (a for a in candidates if piece.path.endswith(a.relative_path)),
                    candidates[0] if candidates else None,
                )
                if chosen is None:
                    unmatched.append(piece.name)
                    continue
                found.append((chosen, piece.in_s, piece.out_s, 1.0))
            if not found:
                print("No piece of that timeline matches a file in the library.")
                return 1
            cut = await record_cut(
                db,
                title,
                found,
                client=args.client,
                profile=None,
                created_by=None,
                source="import",
                created_at=when,
            )
            print(f"Recorded {title!r}: {len(found)} pieces, {cut.duration_s:.1f}s ({cut.id}).")
            if unmatched:
                print(f"Not in the library, left out: {', '.join(unmatched)}")
            return 0
    finally:
        await engine.dispose()


# --- inspecting files ----------------------------------------------------------


def inspect(args: argparse.Namespace) -> int:
    from framefound.processing import shots
    from framefound.processing.probe import probe_ffprobe

    for raw in args.files:
        path = Path(raw)
        probed = probe_ffprobe(path)
        fps = float(probed.get("fps") or 30.0)
        width = int(probed.get("width") or 1920)
        height = int(probed.get("height") or 1080)
        duration = float(probed.get("duration_s") or 0.0)
        analysis, windows, taken = shots.analyse_file(path, fps, width, height, duration)
        print(
            f"{path.name}: {duration:.1f}s at {fps:.2f} fps, analysed in {taken:.1f}s "
            f"({duration / taken if taken else 0:.2f}x real time), {len(windows)} windows"
        )
        for index, window in enumerate(windows):
            by = " ".join(f"{k}%:{v:.2f}" for k, v in window.smooth_by_speed.items())
            look = window.look
            print(
                f"  w{index} {window.start_s:6.1f}-{window.end_s:6.1f}s {window.move:<14} "
                f"{window.direction or '':<6} speed {window.speed:.3f} | {by} | "
                f"energy {window.energy} | warm {look.get('warmth')} sky {look.get('sky')}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    sub = parser.add_subparsers(dest="command", required=True)

    fill = sub.add_parser("backfill", help="analyse videos that have no shot windows yet")
    fill.add_argument("--library", required=True)
    fill.add_argument("--folder", help="only paths containing this (a course, a year)")
    fill.add_argument("--make-proxies", action="store_true", help="queue missing proxies first")
    fill.add_argument("--inline", action="store_true", help="analyse here, and time it")
    fill.add_argument("--redo", action="store_true", help="re-analyse clips already done")
    fill.add_argument("--limit", type=int, default=0)

    desc = sub.add_parser(
        "describe", help="re-run framing and subjects (after a vocabulary change)"
    )
    desc.add_argument("--library", required=True)
    desc.add_argument("--folder")
    desc.add_argument("--inline", action="store_true")

    imp = sub.add_parser("import-cut", help="record an FCP7 XML timeline in cut history")
    imp.add_argument("xml")
    imp.add_argument("--library", required=True)
    imp.add_argument("--title")
    imp.add_argument("--client")
    imp.add_argument("--date", help="when it was cut (ISO); default the file's time")

    ins = sub.add_parser("inspect", help="analyse files directly, no database")
    ins.add_argument("files", nargs="+")

    args = parser.parse_args(argv)
    if args.command == "inspect":
        return inspect(args)
    runner = {"backfill": backfill, "describe": describe, "import-cut": import_cut}[args.command]
    return asyncio.run(runner(args))


if __name__ == "__main__":
    sys.exit(main())
