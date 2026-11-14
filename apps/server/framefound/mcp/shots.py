"""MCP tools for choosing shots: find_shots, contact_sheet, recent_cuts.

What these replace, from the Brookside Quick 30 (2026-11-09): optical-flow
scripts written on the Mac to score drone clips for smoothness, a dozen
`view_media` calls and hand-rendered ffmpeg contact sheets to judge them,
and ordering by time of day by eye. The analysis now happens once at
ingest (processing/shots.py); these tools look it up.

**The measured fields describe; they never prescribe.** find_shots filters
on what a shot is and what it can do — smooth at 50%, holds 6 s at 200%, an
orbit at golden hour — and returns the matches in a stable order. Nothing
measured here says a shot is "good", and there is no house style or last
shot. Fast cuts or slow floaty ones, sped-up footage or ramps: the editor
(the person, or Claude on their behalf) decides, and recent_cuts shows what
was decided last time so the next video can be different on purpose.

**One field is an opinion, and is labelled as one.** Where a local model
has looked at a shot (`looks`, docs/local-models.md L3), its line ends with
`score NN`: that model's 0-100 guess at how usable the shot is, with a
caption and a reason in `verbose`. It is never the default order and never
a filter unless asked for (`order="score"`, `min_score`), and a shot nobody
has looked at prints exactly as it did before scores existed.
"""

import asyncio
import io
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import structlog
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.config import get_settings
from framefound.db.models import Asset, Cut, CutPiece, Look
from framefound.mcp import compact
from framefound.mcp.auth import open_session, principal
from framefound.media import motion
from framefound.nle import shotlist

# The shot search, the lines and cut history live in nle/shotlist.py, where
# the pages and jobs that draft a cut can reach them without the optional
# mcp package. Every name this module had is still here, bound to that copy.
from framefound.nle.shotlist import LIGHT_ALIASES as LIGHT_ALIASES
from framefound.nle.shotlist import MOVE_ALIASES as MOVE_ALIASES
from framefound.nle.shotlist import SKY_WORDS as SKY_WORDS
from framefound.nle.shotlist import SPEED_CHOICES as SPEED_CHOICES
from framefound.nle.shotlist import best_window as best_window
from framefound.nle.shotlist import record_cut as record_cut
from framefound.nle.shotlist import score_word as score_word
from framefound.nle.shotlist import shot_looks as shot_looks
from framefound.nle.shotlist import shot_snapshot as shot_snapshot
from framefound.nle.shotlist import window_line as window_line
from framefound.nle.shotlist import window_score as window_score
from framefound.nle.shotlist import windows_for as windows_for

log = structlog.get_logger()

MAX_SHOTS = 100
MAX_TILES = 24
MAX_STRIPS = 8
# This module's own, and passed to every search find_shots makes, so that
# lowering it here (as a test does) still reaches the query.
SCAN_LIMIT = shotlist.SCAN_LIMIT
# A piece counts as having used a window when they overlap by a second, or
# by half the piece if it is shorter than two.
USED_OVERLAP_S = shotlist.USED_OVERLAP_S


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
    score: int | None = Field(
        default=None,
        description="A local model's 0-100 opinion of how usable the shot is; "
        "None when no model has looked at it. An opinion, not a measurement",
    )
    caption: str | None = Field(default=None, description="That model's caption of the shot")
    score_reason: str | None = Field(default=None, description="That model's reason for its score")
    looked_by: str | None = Field(
        default=None,
        description="Which build and question said it: "
        "<repo>@<revision>[/<variant>]#<prompt version>. The variant names one build "
        "of several in a repository (a quantisation such as Q4_K_M); a key too long "
        "to store ends in ~<digest>",
    )
    made_up: bool = Field(
        default=False, description="True when the demo's made-up model said it, not a real one"
    )


class FrameRef(BaseModel):
    id: str = Field(description="Asset id (short or full)")
    ts_ms: int = Field(ge=0, description="Moment in the video, milliseconds")


# --- helpers -------------------------------------------------------------------


@contextmanager
def _as_tool_error() -> Iterator[None]:
    """A filter shotlist cannot read reaches Claude as the connector's own
    error, in the same words it always had."""
    try:
        yield
    except shotlist.ShotError as err:
        raise ToolError(str(err)) from None


def _normalise_move(value: str) -> str:
    with _as_tool_error():
        return shotlist.normalise_move(value)


def _normalise_light(value: str) -> tuple[str, str]:
    """('light', class) or ('sky', overcast|direct)."""
    with _as_tool_error():
        return shotlist.normalise_light(value)


def _speed(at_speed: int) -> float:
    with _as_tool_error():
        return shotlist.speed_of(at_speed)


_smooth_at = shotlist.smooth_at

# What "used in a cut" means, and the pieces it is read from, live in
# nle/shotlist.py: the API and the job code apply the same rule and cannot
# import this module (it needs the optional mcp package). The names here
# are bound to that one copy.
_overlap = shotlist._overlap
_used = shotlist.used_by
_pieces_by_asset = shotlist.pieces_by_asset


def _look_fields(look: Look | None) -> dict[str, Any]:
    """ShotOut's opinion fields; their defaults when no model looked."""
    if look is None:
        return {}
    return {
        "score": look.score,
        "caption": look.caption or None,
        "score_reason": look.reason or None,
        "looked_by": look.key or None,
        "made_up": bool(look.demo),
    }


# --- stills for contact sheets -------------------------------------------------


async def _still(db: AsyncSession, asset: Asset, ts_ms: int) -> tuple[Any, int]:
    """A picture of the clip at `ts_ms`, and the moment it actually shows
    (-1 with a grey tile when there is none). The looking, and the grab from
    the proxy when no still was saved, are processing/shots.py `still_at`:
    the one place a missing still is made, for a sheet here and for a page
    alike."""
    from framefound.processing import shots

    return await shots.still_at(db, asset, ts_ms, data_dir=get_settings().data_dir)


_NUMBER = r"(\d+(?:\.\d+)?)"
# "12.0-19.5" after an "@", and "12.0-19.5s" as its own word in a find_shots
# line; a whole word with the trailing "s", so a clip named
# "2026-11-03_course" is never read as a time range.
_EXPLICIT = re.compile(rf"@\s*{_NUMBER}(?:\s*[–\-]\s*{_NUMBER})?\s*s?")
_RANGE_WORD = re.compile(rf"{_NUMBER}[–\-]{_NUMBER}s")
_MOMENT_WORD = re.compile(rf"{_NUMBER}s")
_ID = re.compile(r"\b([0-9a-fA-F]{8}(?:[0-9a-fA-F-]{0,28}))\b")


def parse_shot(line: str) -> tuple[str, float, float | None]:
    """An asset id and a time range from a find_shots line ("ab12cd34
    DJI_0639 12.0-19.5s orbit …"), from "ab12cd34@12.0-19.5", or from
    "ab12cd34@12.0" (a single moment)."""
    text = line.strip()
    found = _ID.search(text)
    if not found:
        raise ToolError(f"No asset id in {line!r}")
    rest = text[found.end() :]
    explicit = _EXPLICIT.match(rest)
    if explicit:
        end = explicit.group(2)
        return found.group(1), float(explicit.group(1)), float(end) if end else None
    for word in rest.split():
        span = _RANGE_WORD.fullmatch(word)
        if span:
            return found.group(1), float(span.group(1)), float(span.group(2))
    for word in rest.split():
        moment = _MOMENT_WORD.fullmatch(word)
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
        min_score: int = 0,
        order: Literal["smooth", "length", "time", "score"] = "smooth",
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
        `verbose` returns every field.

        A line ending `score NN` is a local model's 0-100 opinion of how
        usable that shot is — an opinion, not a measurement; `verbose` adds
        its caption, reason and which model said it. `min_score` keeps only
        shots scored at least that (so leaves out every shot no model has
        looked at); `order="score"` puts the highest-scored first and
        unscored shots last."""
        from framefound.mcp.server import _libraries, _library_id, _profile_map

        who = principal()
        limit = max(1, min(limit, MAX_SHOTS))
        _speed(at_speed)
        if not 0 <= min_score <= 100:
            raise ToolError("min_score is a local model's 0-100 opinion; give 0 to 100")
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
            with _as_tool_error():
                after = shotlist.parse_when(captured_after) if captured_after else None
                before = shotlist.parse_when(captured_before) if captured_before else None
                scan = await shotlist.scan_shots(
                    db,
                    shotlist.ShotFilter(
                        library_id=library_id,
                        folder=folder or "",
                        captured_after=after,
                        captured_before=before,
                        moves=wanted_moves,
                        energy=list(energy or []),
                        light=light_classes,
                        skies=skies,
                        subjects=wanted_subjects,
                        min_smooth=min_smooth,
                        at_speed=at_speed,
                        min_length_s=min_length_s,
                        exclude_used=exclude_used,
                        real_frames_only=real_frames_only,
                        sequence_fps=sequence_fps,
                        min_score=min_score,
                        order=order,
                    ),
                    scan_limit=SCAN_LIMIT,
                )
            analysed = await shotlist.analysed_clips(db, library_id)
            libraries = await _libraries(db)
            profiles = await _profile_map(db, profile)

        look_of = scan.look_of
        total = len(scan.shots)
        shown = scan.shots[:limit]

        if verbose:
            from framefound.api.v1.panel import _translate_for

            out = []
            for window, asset, used in shown:
                lib = libraries.get(asset.library_id)
                root = lib.root_path.rstrip("/") if lib else ""
                seen = look_of(window, asset)
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
                        **_look_fields(seen),
                    )
                )
            return compact.structured(out, f"{len(out)} of {total} shots")

        from framefound.api.v1.panel import _translate_for

        lines = []
        scored = False
        for window, asset, used in shown:
            line = shotlist.shot_line(
                asset, window, at_speed, sequence_fps=sequence_fps, used=bool(used)
            )
            # After every measured word and before the path: the line's own
            # words are pinned, and parse_shot reads them back.
            score = score_word(look_of(window, asset))
            scored = scored or bool(score)
            # Local paths only when asked for, by naming the computer.
            local = _translate_for(asset, profiles) if profiles else None
            lines.append(line + score + (f" | {local}" if local else ""))
        speed_note = "" if at_speed == 100 else f" at {at_speed}% (->timeline length)"
        header = (
            f"{len(shown)} of {total} shots{speed_note} | "
            "id clip in-out move smooth energy light subjects"
        )
        if scored:
            header += " [score: a local model's 0-100 opinion]"
        footer = ""
        if min_score and not scan.scanned and scan.any_scored:
            footer = (
                f"No shot here was scored {min_score} or more by a local model. "
                "Lower min_score, or ask without it for the measured shots."
            )
        elif min_score and not scan.scanned and scan.any_window:
            footer = (
                "No shot here has a local model's score yet, so min_score left out every one. "
                "Ask without min_score for the measured shots."
            )
        elif not scan.scanned:
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
            # A local model's draft that went nowhere is not a cut the next
            # video should be different from.
            query = (
                select(Cut)
                .where(shotlist.counts_as_used())
                .order_by(Cut.created_at.desc())
                .limit(limit)
            )
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
