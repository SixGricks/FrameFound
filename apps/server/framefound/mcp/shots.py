"""MCP tools for choosing shots: find_shots, contact_sheet, recent_cuts.

What these replace, from the Brookside Quick 30 (2026-11-09): optical-flow
scripts written on the Mac to score drone clips for smoothness, a dozen
`view_media` calls and hand-rendered ffmpeg contact sheets to judge them,
and ordering by time of day by eye. The analysis now happens once at
ingest (processing/shots.py); these tools look it up.

**They describe; they never prescribe.** find_shots filters on what a shot
is and what it can do — smooth at 50%, holds 6 s at 200%, an orbit at
golden hour — and returns the matches in a stable order. It has no notion
of a "good" shot, a house style or a last shot. Fast cuts or slow floaty
ones, sped-up footage or ramps: the editor (the person, or Claude on their
behalf) decides, and recent_cuts shows what was decided last time so the
next video can be different on purpose.
"""

import asyncio
import io
import re
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.config import get_settings
from framefound.db.models import Asset, Cut, CutPiece, Frame, Library, ShotWindow
from framefound.mcp import compact
from framefound.mcp.auth import open_session, principal
from framefound.media import motion

log = structlog.get_logger()

MAX_SHOTS = 100
MAX_TILES = 24
MAX_STRIPS = 8
SCAN_LIMIT = 20000
# A piece counts as having used a window when they overlap by a second, or
# by half the piece if it is shorter than two.
USED_OVERLAP_S = 1.0

MOVE_ALIASES = {
    "glide": "lateral glide",
    "lateral": "lateral glide",
    "truck": "lateral glide",
    "drift": "top-down drift",
    "top down": "top-down drift",
    "topdown": "top-down drift",
    "push": "push-in",
    "push in": "push-in",
    "pushin": "push-in",
    "dolly in": "push-in",
    "forward": "push-in",
    "pull": "pull-out",
    "pull out": "pull-out",
    "pullout": "pull-out",
    "dolly out": "pull-out",
    "reveal": "pull-out",
    "still": "static",
    "locked off": "static",
    "ascend": "rise",
    "climb": "rise",
    "boom up": "rise",
    "sink": "descend",
    "boom down": "descend",
}
LIGHT_ALIASES = {
    "golden": "golden hour",
    "sunset": "golden hour",
    "sunrise": "golden hour",
    "blue": "blue hour",
    "dusk": "blue hour",
    "low": "low sun",
    "mid": "mid sun",
    "midday": "high sun",
    "high": "high sun",
    "noon": "high sun",
}
SKY_WORDS = {"overcast": "overcast", "cloudy": "overcast", "direct": "direct", "sunny": "direct"}
SPEED_CHOICES = tuple(int(s * 100) for s in motion.SPEEDS)


# --- shapes (verbose answers) --------------------------------------------------


class ShotOut(BaseModel):
    window_id: str
    asset_id: str
    id: str = Field(description="Short asset id, accepted by every tool")
    filename: str
    library: str
    path: str = Field(description="Server-side path of the original")
    local_path: str | None = None
    captured_at: str | None
    start_s: float
    end_s: float
    move: str
    direction: str | None
    speed: float = Field(description="Camera speed, frame widths per second")
    smooth: float = Field(description="Smoothness at the speed asked for")
    smooth_by_speed: dict[str, float]
    energy: str
    energy_score: float
    light: str | None
    sun_elevation: float | None
    look: dict[str, Any]
    framing: dict[str, Any]
    subjects: dict[str, float]
    source_fps: float | None
    speed_fit: dict[str, dict[str, Any]]
    used_in: list[str] = Field(description="Titles of earlier cuts that used this window")


class FrameRef(BaseModel):
    id: str = Field(description="Asset id (short or full)")
    ts_ms: int = Field(ge=0, description="Moment in the video, milliseconds")


# --- helpers -------------------------------------------------------------------


def _normalise_move(value: str) -> str:
    key = value.strip().lower().replace("_", " ")
    key = MOVE_ALIASES.get(key, key)
    if key not in motion.MOVES and key.endswith("s") and key[:-1] in motion.MOVES:
        key = key[:-1]
    if key not in motion.MOVES:
        raise ToolError(f"No move called {value!r}. Moves: {', '.join(motion.MOVES)}")
    return key


def _normalise_light(value: str) -> tuple[str, str]:
    """('light', class) or ('sky', overcast|direct)."""
    key = value.strip().lower().replace("_", " ")
    if key in SKY_WORDS:
        return "sky", SKY_WORDS[key]
    key = LIGHT_ALIASES.get(key, key)
    from framefound.media.sun import LIGHT_CLASSES

    if key not in LIGHT_CLASSES:
        raise ToolError(
            f"No light called {value!r}. Light: {', '.join(LIGHT_CLASSES)}, overcast, direct"
        )
    return "light", key


def _speed(at_speed: int) -> float:
    if at_speed not in SPEED_CHOICES:
        raise ToolError(f"at_speed is a percentage, one of {', '.join(map(str, SPEED_CHOICES))}")
    return at_speed / 100


def _smooth_at(window: ShotWindow, at_speed: int) -> float:
    by = window.smooth_by_speed or {}
    value = by.get(str(at_speed))
    return float(value) if value is not None else float(window.smooth or 0.0)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _used(window: ShotWindow, pieces: list[tuple[int, int, str]]) -> list[str]:
    titles: list[str] = []
    w0, w1 = window.start_ms / 1000, window.end_ms / 1000
    for in_ms, out_ms, title in pieces:
        p0, p1 = in_ms / 1000, out_ms / 1000
        need = min(USED_OVERLAP_S, 0.5 * (p1 - p0))
        if _overlap(w0, w1, p0, p1) >= max(need, 0.05) and title not in titles:
            titles.append(title)
    return titles


async def _pieces_by_asset(
    db: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[int, int, str]]]:
    out: dict[uuid.UUID, list[tuple[int, int, str]]] = {}
    if not asset_ids:
        return out
    for chunk_start in range(0, len(asset_ids), 500):
        chunk = asset_ids[chunk_start : chunk_start + 500]
        rows = (
            await db.execute(
                select(CutPiece.asset_id, CutPiece.in_ms, CutPiece.out_ms, Cut.title)
                .join(Cut, Cut.id == CutPiece.cut_id)
                .where(CutPiece.asset_id.in_(chunk))
            )
        ).all()
        for asset_id, in_ms, out_ms, title in rows:
            out.setdefault(uuid.UUID(str(asset_id)), []).append(
                (int(in_ms), int(out_ms), str(title))
            )
    return out


def window_line(
    window: ShotWindow,
    at_speed: int = 100,
    *,
    sequence_fps: float = 29.97,
    used: bool = False,
    with_subjects: bool = True,
) -> str:
    """The compact description of one window, without the clip: times,
    move, smoothness, energy, light, subjects, and flags."""
    start, end = window.start_ms / 1000, window.end_ms / 1000
    # ASCII dashes and two subjects: twenty lines must stay near 2 KB.
    parts = [f"{start:.1f}-{end:.1f}s", window.move.replace(" ", "-")]
    if window.direction and window.move != "static":
        parts.append(window.direction)
    if at_speed == 100:
        parts.append(f"smooth {compact.score(window.smooth)}")
    else:
        parts.append(f"smooth@{at_speed}% {compact.score(_smooth_at(window, at_speed))}")
    parts.append(f"energy {window.energy}")
    if window.light:
        parts.append(compact.LIGHT_SHORT.get(window.light, window.light))
    sky = (window.look or {}).get("sky")
    if sky == "overcast":
        parts.append("overcast")
    if with_subjects and window.subjects:
        parts.append(",".join(list(window.subjects)[:2]))
    if at_speed != 100:
        length = (end - start) / (at_speed / 100)
        parts.append(f"->{length:.1f}s")
        if (
            at_speed < 100
            and window.source_fps
            and window.source_fps * at_speed / 100 < sequence_fps * 0.97
        ):
            parts.append("dup")
    if used:
        parts.append("used")
    return " ".join(parts)


def shot_snapshot(window: ShotWindow | None, speed: float = 1.0) -> dict[str, Any]:
    """What a piece's window was, for cut history."""
    if window is None:
        return {}
    key = motion.speed_key(speed)
    return {
        "idx": window.idx,
        "start_ms": window.start_ms,
        "end_ms": window.end_ms,
        "move": window.move,
        "direction": window.direction,
        "smooth": (window.smooth_by_speed or {}).get(key, window.smooth),
        "energy": window.energy,
        "light": window.light,
        "sky": (window.look or {}).get("sky"),
        "subjects": list((window.subjects or {}).keys())[:4],
    }


def best_window(windows: list[ShotWindow], in_s: float, out_s: float) -> ShotWindow | None:
    best, overlap = None, 0.0
    for window in windows:
        found = _overlap(window.start_ms / 1000, window.end_ms / 1000, in_s, out_s)
        if found > overlap:
            best, overlap = window, found
    return best


async def windows_for(
    db: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ShotWindow]]:
    out: dict[uuid.UUID, list[ShotWindow]] = {}
    if not asset_ids:
        return out
    rows = (
        (
            await db.execute(
                select(ShotWindow)
                .where(ShotWindow.asset_id.in_(asset_ids))
                .order_by(ShotWindow.asset_id, ShotWindow.idx)
            )
        )
        .scalars()
        .all()
    )
    for window in rows:
        out.setdefault(uuid.UUID(str(window.asset_id)), []).append(window)
    return out


async def record_cut(
    db: AsyncSession,
    title: str,
    pieces: list[tuple[Asset, float, float, float]],
    *,
    client: str | None,
    profile: str | None,
    created_by: uuid.UUID | None,
    source: str = "mcp",
    created_at: datetime | None = None,
) -> Cut:
    """Write a cut and its pieces, each with a snapshot of its window."""
    libraries = {lib.id: lib for lib in (await db.execute(select(Library))).scalars()}
    counts: dict[uuid.UUID, int] = {}
    for asset, *_ in pieces:
        counts[asset.library_id] = counts.get(asset.library_id, 0) + 1
    library_id = max(counts, key=lambda k: counts[k]) if counts else None
    library = libraries.get(library_id) if library_id else None
    windows = await windows_for(db, list({asset.id for asset, *_ in pieces}))
    cut = Cut(
        title=title[:300],
        client=(client or (library.name if library else ""))[:200],
        library_id=library_id,
        source=source,
        profile=profile,
        duration_s=round(sum((out - in_) / speed for _, in_, out, speed in pieces), 3),
        piece_count=len(pieces),
        created_by=created_by,
    )
    if created_at is not None:
        cut.created_at = created_at
    db.add(cut)
    await db.flush()
    for position, (asset, in_s, out_s, speed) in enumerate(pieces):
        window = best_window(windows.get(asset.id, []), in_s, out_s)
        db.add(
            CutPiece(
                cut_id=cut.id,
                position=position,
                asset_id=asset.id,
                filename=asset.filename,
                in_ms=int(round(in_s * 1000)),
                out_ms=int(round(out_s * 1000)),
                speed=speed,
                shot=shot_snapshot(window, speed),
            )
        )
    await db.commit()
    return cut


# --- stills for contact sheets -------------------------------------------------

# A thread-side cap, not an asyncio one: grabs run in worker threads, and an
# asyncio primitive created at import would bind to whichever loop used it
# first.
_extract_slots = threading.BoundedSemaphore(3)


def _grab(src: Any, dst: Any, at_s: float, width: int) -> None:
    from framefound.processing import scenes

    with _extract_slots:
        scenes.extract_frame(src, dst, at_s, max_width=width)


async def _still(db: AsyncSession, asset: Asset, ts_ms: int) -> tuple[Any, int]:
    """A picture of the clip at `ts_ms`, and the moment it actually shows.

    In order: the still the analysis saved there, a sampled frame within
    0.6 s, one grabbed from the proxy now (and kept, so the next sheet is
    instant), and failing all of those the nearest sampled frame."""
    from PIL import Image as PilImage

    from framefound.processing import shots

    data_dir = get_settings().data_dir
    saved = data_dir / shots.still_relpath(asset.id, ts_ms)
    if saved.is_file():
        return PilImage.open(saved).convert("RGB"), ts_ms
    nearest = (
        await db.execute(
            select(Frame.ts_ms, Frame.relative_path)
            .where(Frame.asset_id == asset.id)
            .order_by(func.abs(Frame.ts_ms - ts_ms), Frame.ts_ms)
            .limit(1)
        )
    ).first()
    if nearest is not None and abs(int(nearest[0]) - ts_ms) <= 600:
        path = data_dir / str(nearest[1])
        if path.is_file():
            return PilImage.open(path).convert("RGB"), int(nearest[0])
    proxy = shots.proxy_path(data_dir, asset.id)
    if proxy.is_file():
        saved.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(_grab, proxy, saved, ts_ms / 1000, shots.STILL_WIDTH)
            return PilImage.open(saved).convert("RGB"), ts_ms
        except Exception as exc:
            log.warning("mcp.sheet_grab_failed", asset_id=str(asset.id), reason=str(exc)[:120])
    if nearest is not None:
        path = data_dir / str(nearest[1])
        if path.is_file():
            return PilImage.open(path).convert("RGB"), int(nearest[0])
    blank = PilImage.new("RGB", (480, 270), (60, 60, 60))
    return blank, -1


_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[–\-]\s*(\d+(?:\.\d+)?)\s*s?")
_ID = re.compile(r"\b([0-9a-fA-F]{8}(?:[0-9a-fA-F-]{0,28}))\b")


def parse_shot(line: str) -> tuple[str, float, float | None]:
    """An asset id and a time range from a find_shots line ("ab12cd34
    DJI_0639 12.0–19.5s orbit …"), from "ab12cd34@12.0-19.5", or from
    "ab12cd34@12.0" (a single moment)."""
    text = line.strip()
    found = _ID.search(text)
    if not found:
        raise ToolError(f"No asset id in {line!r}")
    rest = text[found.end() :]
    span = _RANGE.search(rest)
    if span:
        return found.group(1), float(span.group(1)), float(span.group(2))
    moment = re.search(r"(\d+(?:\.\d+)?)\s*s?", rest)
    if moment:
        return found.group(1), float(moment.group(1)), None
    raise ToolError(f"No time range in {line!r}; give it as id@start-end")


# --- the tools -----------------------------------------------------------------


def register(server: MCPServer) -> None:
    @server.tool()
    async def find_shots(
        library: str | None = None,
        folder: str | None = None,
        captured_after: str | None = None,
        captured_before: str | None = None,
        moves: list[str] | None = None,
        energy: list[Literal["low", "medium", "high"]] | None = None,
        light: list[str] | None = None,
        subjects: list[str] | None = None,
        min_smooth: float = 0.0,
        at_speed: int = 100,
        min_length_s: float = 0.0,
        exclude_used: bool = False,
        real_frames_only: bool = False,
        sequence_fps: float = 29.97,
        order: Literal["smooth", "length", "time"] = "smooth",
        limit: int = 20,
        verbose: bool = False,
        profile: str | None = None,
    ) -> CallToolResult:
        """Shots across a library: stretches of video where the camera does
        one thing, described at ingest. One compact line each:
        `id clip in-out move [direction] smooth energy light subjects`.

        Filters: `folder` (a course or folder name, matched in the path),
        capture dates (ISO), `moves` (static, pan, tilt, push-in, pull-out,
        orbit, rise, descend, lateral glide, top-down drift), `energy` of
        the subject (low/medium/high), `light` (night, blue hour, golden
        hour, low/mid/high sun, or overcast/direct), `subjects` (any of),
        `min_smooth` judged AT `at_speed` (a percentage: 25, 50, 100, 200,
        400, 800 — jitter that ruins 50% can vanish at 400%), and
        `min_length_s`, the timeline length the window fills at that speed.
        `real_frames_only` keeps slow motion that shows real frames in a
        `sequence_fps` timeline (a 29.97 source at 50% duplicates every
        frame; such lines are flagged `dup`). `exclude_used` drops windows
        earlier cuts used (otherwise flagged `used`).

        These describe; they do not choose. Pick by the edit's own pace and
        music. Cut a line with build_rough_cut (the id, in_s/out_s inside
        its range); look at lines with contact_sheet(shots=[...]).
        `verbose` returns every field."""
        from framefound.mcp.server import _libraries, _library_id, _profile_map

        who = principal()
        limit = max(1, min(limit, MAX_SHOTS))
        speed = _speed(at_speed)
        wanted_moves = sorted({_normalise_move(m) for m in moves or [] if m.strip()})
        light_classes: list[str] = []
        skies: list[str] = []
        for value in light or []:
            if not value.strip():
                continue
            kind, found = _normalise_light(value)
            (light_classes if kind == "light" else skies).append(found)
        wanted_subjects = [s.strip().lower() for s in subjects or [] if s.strip()]
        async with open_session() as db:
            library_id = await _library_id(db, library)
            query = (
                select(ShotWindow, Asset)
                .join(Asset, Asset.id == ShotWindow.asset_id)
                .where(Asset.availability == "online", Asset.media_type == "video")
            )
            if library_id is not None:
                query = query.where(Asset.library_id == library_id)
            if folder and folder.strip():
                query = query.where(
                    func.lower(Asset.relative_path).contains(
                        folder.strip().lower(), autoescape=True
                    )
                )
            for bound, is_after in ((captured_after, True), (captured_before, False)):
                if bound:
                    try:
                        when = datetime.fromisoformat(bound)
                    except ValueError:
                        raise ToolError(f"{bound!r} is not an ISO date") from None
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=UTC)
                    query = query.where(
                        Asset.captured_at >= when if is_after else Asset.captured_at <= when
                    )
            if wanted_moves:
                query = query.where(ShotWindow.move.in_(wanted_moves))
            if energy:
                query = query.where(ShotWindow.energy.in_(list(energy)))
            if light_classes:
                query = query.where(ShotWindow.light.in_(light_classes))
            rows = (await db.execute(query.limit(SCAN_LIMIT))).all()
            analysed = int(
                (
                    await db.execute(
                        select(func.count(func.distinct(ShotWindow.asset_id)))
                        .join(Asset, Asset.id == ShotWindow.asset_id)
                        .where(*([Asset.library_id == library_id] if library_id else []))
                    )
                ).scalar_one()
            )
            pieces = await _pieces_by_asset(db, list({a.id for _, a in rows}))
            libraries = await _libraries(db)
            profiles = await _profile_map(db, profile)

        matched: list[tuple[ShotWindow, Asset, list[str]]] = []
        for window, asset in rows:
            if _smooth_at(window, at_speed) < min_smooth:
                continue
            if (window.end_ms - window.start_ms) / 1000 / speed < min_length_s:
                continue
            if skies and (window.look or {}).get("sky") not in skies:
                continue
            if wanted_subjects:
                have = [s.lower() for s in (window.subjects or {})]
                if not any(any(w == h or w in h for h in have) for w in wanted_subjects):
                    continue
            if (
                real_frames_only
                and speed < 1.0
                and (not window.source_fps or window.source_fps * speed < sequence_fps * 0.97)
            ):
                continue
            used = _used(window, pieces.get(asset.id, []))
            if exclude_used and used:
                continue
            matched.append((window, asset, used))

        def key(item: tuple[ShotWindow, Asset, list[str]]) -> tuple[Any, ...]:
            window, asset, _ = item
            length = window.end_ms - window.start_ms
            when = asset.captured_at.timestamp() if asset.captured_at else 0.0
            if order == "length":
                return (-length, asset.filename, window.idx)
            if order == "time":
                return (when, asset.filename, window.start_ms)
            return (-round(_smooth_at(window, at_speed), 2), -length, asset.filename, window.idx)

        matched.sort(key=key)
        total = len(matched)
        shown = matched[:limit]

        if verbose:
            from framefound.api.v1.panel import _translate_for

            out = []
            for window, asset, used in shown:
                lib = libraries.get(asset.library_id)
                root = lib.root_path.rstrip("/") if lib else ""
                out.append(
                    ShotOut(
                        window_id=str(window.id),
                        asset_id=str(asset.id),
                        id=compact.short_id(asset.id),
                        filename=asset.filename,
                        library=lib.name if lib else "",
                        path=f"{root}/{asset.relative_path}",
                        local_path=_translate_for(asset, profiles) if profiles else None,
                        captured_at=asset.captured_at.isoformat() if asset.captured_at else None,
                        start_s=window.start_ms / 1000,
                        end_s=window.end_ms / 1000,
                        move=window.move,
                        direction=window.direction,
                        speed=window.speed,
                        smooth=_smooth_at(window, at_speed),
                        smooth_by_speed=window.smooth_by_speed or {},
                        energy=window.energy,
                        energy_score=window.energy_score,
                        light=window.light,
                        sun_elevation=window.sun_elevation,
                        look=window.look or {},
                        framing=window.framing or {},
                        subjects=window.subjects or {},
                        source_fps=window.source_fps,
                        speed_fit=motion.speed_fit(
                            (window.end_ms - window.start_ms) / 1000,
                            window.source_fps,
                            sequence_fps,
                        ),
                        used_in=used,
                    )
                )
            return compact.structured(out, f"{len(out)} of {total} shots")

        from framefound.api.v1.panel import _translate_for

        lines = []
        for window, asset, used in shown:
            line = f"{compact.short_id(asset.id)} {compact.clip_name(asset.filename)} " + (
                window_line(window, at_speed, sequence_fps=sequence_fps, used=bool(used))
            )
            # Local paths only when asked for, by naming the computer.
            local = _translate_for(asset, profiles) if profiles else None
            lines.append(line + (f" | {local}" if local else ""))
        speed_note = "" if at_speed == 100 else f" at {at_speed}% (->timeline length)"
        header = (
            f"{len(shown)} of {total} shots{speed_note} | "
            "id clip in-out move smooth energy light subjects"
        )
        footer = ""
        if not rows:
            footer = (
                f"No analysed shots match. {analysed} clip(s) in scope have been analysed; "
                "shot analysis needs a proxy (python -m framefound.ops.shots backfill)."
            )
        elif total > len(shown):
            footer = f"{total - len(shown)} more: raise limit or narrow the filters."
        log.info("mcp.find_shots", matched=total, shown=len(shown), by=who.token_name)
        return compact.table(header, lines, footer)

    @server.tool(structured_output=False)
    async def contact_sheet(
        shots: list[str] | None = None,
        frames: list[FrameRef] | None = None,
        asset: str | None = None,
        mode: Literal["grid", "strip"] = "grid",
        title: str = "",
    ) -> list[Any]:
        """One picture of many moments, instead of a view_media call each.

        Give `shots` (lines from find_shots, pasted as they are, or
        "id@12.0-19.5"), `frames` ({id, ts_ms} pairs), or `asset` (one
        clip: all its windows). "grid" shows each shot's middle (up to 24
        tiles); "strip" shows start, middle and end of each shot side by
        side (up to 8 shots), so a move can be judged from one image. Each
        tile is labelled: number, clip, time, move, smoothness."""
        from PIL import Image as PilImage

        from framefound.mcp.server import _asset
        from framefound.media import contact_sheet as sheet

        principal()
        wanted: list[tuple[str, float, float | None]] = []
        for line in shots or []:
            if line.strip():
                wanted.append(parse_shot(line))
        for frame in frames or []:
            wanted.append((frame.id, frame.ts_ms / 1000, None))
        async with open_session() as db:
            if asset:
                clip = await _asset(db, asset)
                for found in (await windows_for(db, [clip.id])).get(clip.id, []):
                    wanted.append((str(clip.id), found.start_ms / 1000, found.end_ms / 1000))
            if not wanted:
                raise ToolError("Give shots, frames or an asset to show")
            limit = MAX_STRIPS if mode == "strip" else MAX_TILES
            dropped = max(0, len(wanted) - limit)
            wanted = wanted[:limit]
            assets: dict[str, Asset] = {}
            for ref, *_ in wanted:
                if ref not in assets:
                    assets[ref] = await _asset(db, ref)
                    if assets[ref].media_type != "video":
                        raise ToolError(f"{assets[ref].filename} is not a video")
            windows = await windows_for(db, list({a.id for a in assets.values()}))
            tiles: list[tuple[str, Any]] = []
            captions: list[str] = []
            for number, (ref, start, end) in enumerate(wanted, 1):
                clip = assets[ref]
                window = best_window(
                    windows.get(clip.id, []), start, end if end is not None else start + 0.01
                )
                label_tail = ""
                if window is not None:
                    label_tail = f" {window.move} {compact.score(window.smooth)}"
                if end is None:
                    moments = [int(start * 1000)]
                elif mode == "strip":
                    from framefound.processing.shots import window_moments

                    moments = list(window_moments(int(start * 1000), int(end * 1000)))
                else:
                    moments = [int((start + end) / 2 * 1000)]
                name = compact.clip_name(clip.filename, 14)
                for position, ts_ms in enumerate(moments):
                    image, shown_ms = await _still(db, clip, ts_ms)
                    when = f"{shown_ms / 1000:.1f}s" if shown_ms >= 0 else "no picture"
                    tag = f"{number}" + ("abc"[position] if len(moments) > 1 else "")
                    tiles.append((f"{tag} {name} {when}{label_tail}", image))
                span = f"{start:.1f}" + (f"–{end:.1f}s" if end is not None else "s")
                captions.append(
                    f"{number} {compact.short_id(clip.id)} "
                    f"{compact.clip_name(clip.filename)} {span}"
                    + (f" {window.move}" if window is not None else "")
                )

        def draw() -> bytes:
            prepared = []
            columns = 3 if mode == "strip" else 4
            cell_w = 516 if mode == "strip" else 384
            cell_h = cell_w * 9 // 16
            for label, image in tiles:
                copy = image.copy()
                copy.thumbnail((cell_w, cell_h), PilImage.Resampling.LANCZOS)
                prepared.append((label, copy))
            composed = sheet.compose(
                prepared,
                title,
                columns=columns,
                cell_width=cell_w,
                cell_image_height=cell_h,
                per_sheet=len(prepared),
                label_size=14,
            )
            out = io.BytesIO()
            composed.save(out, "JPEG", quality=80)
            return out.getvalue()

        jpeg = await asyncio.to_thread(draw)
        caption = "Contact sheet" + (f" — {title}" if title else "") + ":\n" + "\n".join(captions)
        if mode == "strip":
            caption += "\nEach row: a (start), b (middle), c (end)."
        if dropped:
            caption += f"\n{dropped} more left out (at most {limit} per sheet)."
        return [Image(data=jpeg, format="jpeg"), caption]

    @server.tool()
    async def recent_cuts(
        library: str | None = None,
        client: str | None = None,
        limit: int = 5,
        verbose: bool = False,
    ) -> CallToolResult:
        """The last cuts built for a library or client, summarised: shot
        count, average shot length, speed mix, move mix, light mix and
        subjects — so the next video can be different on purpose rather
        than a repeat of the last."""
        from framefound.mcp.server import _library_id

        principal()
        limit = max(1, min(limit, 50))
        async with open_session() as db:
            library_id = await _library_id(db, library)
            query = select(Cut).order_by(Cut.created_at.desc()).limit(limit)
            if library_id is not None:
                query = query.where(Cut.library_id == library_id)
            if client and client.strip():
                query = query.where(func.lower(Cut.client) == client.strip().lower())
            cuts = (await db.execute(query)).scalars().all()
            pieces: dict[uuid.UUID, list[CutPiece]] = {}
            if cuts:
                for piece in (
                    (
                        await db.execute(
                            select(CutPiece)
                            .where(CutPiece.cut_id.in_([c.id for c in cuts]))
                            .order_by(CutPiece.cut_id, CutPiece.position)
                        )
                    )
                    .scalars()
                    .all()
                ):
                    pieces.setdefault(uuid.UUID(str(piece.cut_id)), []).append(piece)
        if not cuts:
            return compact.table("No cuts recorded yet for that library or client.", [])
        summaries = [summarise(cut, pieces.get(cut.id, [])) for cut in cuts]
        if verbose:
            return compact.structured([CutSummary(**s) for s in summaries])
        lines = [_cut_line(s) for s in summaries]
        everything = [p for c in cuts for p in pieces.get(c.id, [])]
        overall = summarise_pieces(everything)
        footer = (
            f"Across these {len(cuts)}: avg shot {overall['avg_shot_s']:.1f}s · "
            f"speed {_mix(overall['speeds'])} · moves {_mix(overall['moves'])} · "
            f"light {_mix(overall['light'])}"
            + (f" · subjects {_mix(overall['subjects'])}" if overall["subjects"] else "")
        )
        return compact.table(f"{len(cuts)} recent cut(s), newest first", lines, footer)


class CutSummary(BaseModel):
    id: str
    title: str
    client: str
    created_at: str
    source: str
    shots: int
    duration_s: float
    avg_shot_s: float
    speeds: dict[str, int]
    moves: dict[str, int]
    light: dict[str, int]
    subjects: dict[str, int]
    pieces: list[dict[str, Any]]


def _count(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def summarise_pieces(pieces: list[CutPiece]) -> dict[str, Any]:
    lengths = [(p.out_ms - p.in_ms) / 1000 / (p.speed or 1.0) for p in pieces]
    return {
        "shots": len(pieces),
        "avg_shot_s": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
        "speeds": _count([f"{int(round((p.speed or 1.0) * 100))}%" for p in pieces]),
        "moves": _count([str((p.shot or {}).get("move") or "unanalysed") for p in pieces]),
        "light": _count(
            [
                compact.LIGHT_SHORT.get(str(light), str(light))
                for p in pieces
                if (light := (p.shot or {}).get("light"))
            ]
        ),
        "subjects": _count([s for p in pieces for s in (p.shot or {}).get("subjects", [])[:2]]),
    }


def summarise(cut: Cut, pieces: list[CutPiece]) -> dict[str, Any]:
    stats = summarise_pieces(pieces)
    return {
        "id": compact.short_id(cut.id),
        "title": cut.title,
        "client": cut.client,
        "created_at": cut.created_at.isoformat() if cut.created_at else "",
        "source": cut.source,
        "duration_s": round(cut.duration_s, 2),
        **stats,
        "pieces": [
            {
                "clip": compact.clip_name(p.filename),
                "in_s": p.in_ms / 1000,
                "out_s": p.out_ms / 1000,
                "speed": p.speed,
                "move": (p.shot or {}).get("move"),
                "light": (p.shot or {}).get("light"),
            }
            for p in pieces
        ],
    }


def _mix(counts: dict[str, int], top: int = 4) -> str:
    if not counts:
        return "—"
    items = list(counts.items())
    shown = ", ".join(f"{k} {v}" for k, v in items[:top])
    return shown + (f", +{len(items) - top}" if len(items) > top else "")


def _cut_line(s: dict[str, Any]) -> str:
    day = s["created_at"][:10]
    parts = [
        f"{day} “{s['title']}” {s['client']}",
        f"{s['shots']} shots",
        f"{s['duration_s']:.1f}s",
        f"avg {s['avg_shot_s']:.1f}s",
        f"speed {_mix(s['speeds'])}",
        f"moves {_mix(s['moves'])}",
        f"light {_mix(s['light'])}",
    ]
    if s["subjects"]:
        parts.append(f"subjects {_mix(s['subjects'])}")
    if s["source"] == "import":
        parts.append("(imported)")
    return " · ".join(parts)
