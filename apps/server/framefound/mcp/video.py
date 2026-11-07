"""MCP tools for making video from the catalogue — the Cowork workflow.

What an agent needs to go from "find the footage of the Ledgerock barn and
cut a sixty-second piece" to a timeline an editor opens:

1. **Where things are, for this machine.** The server's `/media/...` path
   opens nowhere. Path profiles ("Mac Mini", "DJ's PC") already translate
   it for the Premiere panel and Lightroom; `get_media_files` and
   `build_rough_cut` take the same `profile` so a path handed back opens
   on the computer asking, Windows or Mac.
2. **What was said, and when.** `get_transcript` returns every timed cue,
   so a quote can be cut on its words rather than on a guess.
3. **Where the shots change.** `get_scenes` turns the sampled frames into
   ranges, one per detected shot, which is how b-roll gets chosen. Claude
   *looks* at a shot with `view_media(id, ts_ms=...)`, which returns the
   picture itself; a signed `frame_url` is only a link for a person, since
   Claude Desktop's own fetching runs off-machine and cannot reach the LAN.
4. **Selects to review.** `build_selects` turns a search (or chosen moments)
   into a bin with a marker on each moment and a "Selects" string-out with
   handles — the step between a pile of clips and an edit.
5. **Something to edit.** `build_rough_cut` writes an FCP7 XML *sequence*
   (Premiere Pro and DaVinci Resolve open it; Final Cut Pro 10/11 reads only
   FCPXML and needs a converter such as 7toX) referencing the originals
   through the profile, in the order and with the in/out points chosen.

Everything here reads. Originals are never served over the connector —
they open through the share on the workstation; what downloads is the
1080p proxy, which is plenty for an agent to cut, caption or preview.
"""

import uuid
from typing import Any

import structlog
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field
from sqlalchemy import select

from framefound.api.v1.selects import MomentIn, SelectsResult
from framefound.db.models import (
    Asset,
    Derivative,
    Frame,
    Library,
    PathMapping,
    Transcript,
    TranscriptSegment,
)
from framefound.mcp.auth import open_session, principal
from framefound.nle.selects import (
    DEFAULT_HANDLES_S,
    DEFAULT_MAX_PICTURE_S,
    DEFAULT_MAX_SPEECH_S,
    Order,
    scene_groups,
    scene_ranges,
)

log = structlog.get_logger()

MAX_CUTS = 200
MAX_SEGMENTS = 2000
DEFAULT_STILL_S = 4.0
# How a downloaded link is described to the agent, per derivative kind.
DOWNLOADS = {
    "proxy": "1080p H.264 MP4 of the video, with sound — download this to cut or caption",
    "poster": "a still from the video",
    "preview": "a large JPEG of the photograph",
    "thumbnail": "a small preview image",
    "subtitle": "WebVTT captions from the transcript",
    "waveform": "an image of the audio's waveform",
}


# --- shapes ------------------------------------------------------------------


class ProfileOut(BaseModel):
    name: str
    platform: str = Field(description="windows, macos or linux")
    libraries: dict[str, str] = Field(description="Library name -> where this machine sees it")


class LocalPath(BaseModel):
    profile: str
    platform: str
    path: str


class MediaFiles(BaseModel):
    id: str
    filename: str
    media_type: str
    duration_s: float | None
    fps: float | None
    width: int | None
    height: int | None
    has_audio: bool
    local_paths: list[LocalPath] = Field(
        description="The original, as each workstation profile opens it"
    )
    downloads: dict[str, str] = Field(description="Signed links (12 hours) per derivative")
    about_downloads: dict[str, str]
    note: str


class Cue(BaseModel):
    start_s: float
    end_s: float
    text: str


class TranscriptResult(BaseModel):
    id: str
    filename: str
    state: str = Field(description="speech, no_speech, or none (not transcribed)")
    language: str | None
    model: str | None
    cues: list[Cue]
    truncated: bool
    captions_url: str | None = Field(description="Signed WebVTT link, when captions exist")


class Scene(BaseModel):
    scene: int
    start_s: float
    end_s: float
    frame_ms: int = Field(description="The scene's picture: look at it with view_media(id, ts_ms)")
    sampled_ms: list[int] = Field(
        description="Every sampled moment inside the scene; any can be passed to view_media"
    )
    frame_url: str | None = Field(
        description="Signed link to the picture, for a person on the network to open"
    )


class ScenesResult(BaseModel):
    id: str
    filename: str
    duration_s: float | None
    scenes: list[Scene]
    note: str


class CutIn(BaseModel):
    id: str = Field(description="Asset id (a video, or a photograph to hold on screen)")
    in_s: float = Field(default=0.0, ge=0, description="Start, seconds into the source")
    out_s: float | None = Field(
        default=None, description="End, seconds into the source; a photo's hold length"
    )
    note: str = Field(default="", max_length=500, description="Becomes a marker on the cut")


class RoughCut(BaseModel):
    title: str
    filename: str
    profile: str | None
    duration_s: float
    cuts: int
    xml: str = Field(description="FCP7 XML: save as .xml and import into Premiere or Resolve")
    unmapped: list[str] = Field(
        description="Files with no path for this profile; they will import offline"
    )
    note: str


# --- helpers -----------------------------------------------------------------


def _signed(base: str, asset_id: uuid.UUID, kind: str, query: str = "") -> str | None:
    from framefound.mcp.server import _media_url

    url = _media_url(base, asset_id, kind)
    if url is None:
        return None
    if query:
        path, _, signature = url.partition("?")
        return f"{path}?{query}&{signature}"
    return url


async def _asset(db: Any, asset_id: str) -> Asset:
    from framefound.mcp.server import _asset as find

    return await find(db, asset_id)


async def _profile_map(db: Any, profile: str | None) -> dict[uuid.UUID, tuple[Any, Any]]:
    from framefound.api.v1.panel import _profiles

    if not profile or not profile.strip():
        return {}
    found = await _profiles(db, profile)
    if not found:
        names = sorted(set((await db.execute(select(PathMapping.profile_name))).scalars().all()))
        raise ToolError(
            f"No path profile called {profile!r}. Profiles: {', '.join(names) or 'none yet'} "
            "(add one per library on the Libraries page)."
        )
    return found


def _local_path(asset: Asset, profiles: dict[uuid.UUID, tuple[Any, Any]]) -> str | None:
    from framefound.api.v1.panel import _translate_for

    return _translate_for(asset, profiles)


def _has_audio(asset: Asset) -> bool:
    return asset.media_type == "audio" or asset.audio_codec is not None


# --- the tools ---------------------------------------------------------------


def register(server: MCPServer) -> None:
    @server.tool()
    async def path_profiles() -> list[ProfileOut]:
        """The workstation path profiles: how each computer (a Windows PC, a
        Mac) sees the libraries. Pass a profile's name as `profile` to
        get_media_files or build_rough_cut to get paths that open on that
        machine. Ask the person which computer they are on if unsure."""
        principal()
        async with open_session() as db:
            rows = (
                await db.execute(
                    select(PathMapping, Library)
                    .join(Library, Library.id == PathMapping.library_id)
                    .order_by(PathMapping.profile_name, Library.name)
                )
            ).all()
        grouped: dict[str, ProfileOut] = {}
        for mapping, library in rows:
            entry = grouped.setdefault(
                mapping.profile_name,
                ProfileOut(name=mapping.profile_name, platform=mapping.platform, libraries={}),
            )
            entry.libraries[library.name] = mapping.mapped_prefix
        return list(grouped.values())

    @server.tool()
    async def get_media_files(id: str, profile: str | None = None) -> MediaFiles:
        """How to get at one item's media: the original's path on each
        workstation (or just `profile`'s), and signed download links for the
        proxy, poster, captions and previews. Originals are not downloadable;
        they open from the share on a machine that has it mounted."""
        who = principal()
        async with open_session() as db:
            asset = await _asset(db, id)
            if profile and profile.strip():
                wanted = await _profile_map(db, profile)
                mappings = [
                    (mapping, library)
                    for library_id, (mapping, library) in wanted.items()
                    if library_id == asset.library_id
                ]
            else:
                mappings = [
                    (row[0], row[1])
                    for row in (
                        await db.execute(
                            select(PathMapping, Library)
                            .join(Library, Library.id == PathMapping.library_id)
                            .where(PathMapping.library_id == asset.library_id)
                            .order_by(PathMapping.profile_name)
                        )
                    ).all()
                ]
            kinds = (
                (
                    await db.execute(
                        select(Derivative.kind).where(
                            Derivative.asset_id == asset.id, Derivative.status == "ready"
                        )
                    )
                )
                .scalars()
                .all()
            )
        local: list[LocalPath] = []
        for mapping, library in mappings:
            path = _local_path(asset, {asset.library_id: (mapping, library)})
            if path:
                local.append(
                    LocalPath(profile=mapping.profile_name, platform=mapping.platform, path=path)
                )
        downloads = {
            str(kind): url
            for kind in kinds
            if str(kind) in DOWNLOADS and (url := _signed(who.base_url, asset.id, str(kind)))
        }
        note = (
            "Use the proxy link to download the video for cutting; use a local path to "
            "open the full-quality original on that computer."
            if asset.media_type == "video"
            else "Use a local path to open the original on that computer."
        )
        if not local:
            note += " No workstation profile covers this library yet."
        return MediaFiles(
            id=str(asset.id),
            filename=asset.filename,
            media_type=asset.media_type,
            duration_s=asset.duration_s,
            fps=asset.fps,
            width=asset.width,
            height=asset.height,
            has_audio=_has_audio(asset),
            local_paths=local,
            downloads=downloads,
            about_downloads={kind: DOWNLOADS[kind] for kind in downloads},
            note=note,
        )

    @server.tool()
    async def get_transcript(
        id: str, start_s: float | None = None, end_s: float | None = None
    ) -> TranscriptResult:
        """Everything said in a video or recording, as timed cues (seconds).
        Narrow with start_s/end_s for a long one. Use the cue times as in/out
        points for build_rough_cut to cut a quote on its words."""
        who = principal()
        async with open_session() as db:
            asset = await _asset(db, id)
            transcript = (
                await db.execute(select(Transcript).where(Transcript.asset_id == asset.id))
            ).scalar_one_or_none()
            cues: list[Cue] = []
            truncated = False
            if transcript is not None:
                query = select(TranscriptSegment).where(
                    TranscriptSegment.transcript_id == transcript.id
                )
                if start_s is not None:
                    query = query.where(TranscriptSegment.end_ms >= int(start_s * 1000))
                if end_s is not None:
                    query = query.where(TranscriptSegment.start_ms <= int(end_s * 1000))
                rows = (
                    (
                        await db.execute(
                            query.order_by(TranscriptSegment.start_ms).limit(MAX_SEGMENTS + 1)
                        )
                    )
                    .scalars()
                    .all()
                )
                truncated = len(rows) > MAX_SEGMENTS
                cues = [
                    Cue(start_s=row.start_ms / 1000, end_s=row.end_ms / 1000, text=row.text)
                    for row in rows[:MAX_SEGMENTS]
                ]
            has_captions = (
                await db.execute(
                    select(Derivative.id).where(
                        Derivative.asset_id == asset.id,
                        Derivative.kind == "subtitle",
                        Derivative.status == "ready",
                    )
                )
            ).first() is not None
        if transcript is None:
            state = "none"
        else:
            state = "speech" if transcript.segment_count else "no_speech"
        return TranscriptResult(
            id=str(asset.id),
            filename=asset.filename,
            state=state,
            language=transcript.language if transcript else None,
            model=transcript.model_name if transcript else None,
            cues=cues,
            truncated=truncated,
            captions_url=_signed(who.base_url, asset.id, "subtitle") if has_captions else None,
        )

    @server.tool()
    async def get_scenes(id: str) -> ScenesResult:
        """A video's shots: where each scene starts and ends (seconds). Use
        them to choose b-roll and in/out points. To see a scene, call
        view_media(id, ts_ms=frame_ms) — it returns the picture itself, and
        any of `sampled_ms` shows another moment inside the shot. frame_url
        is a link for the person, not something you can open."""
        who = principal()
        async with open_session() as db:
            asset = await _asset(db, id)
            if asset.media_type != "video":
                raise ToolError(f"{asset.filename} is a {asset.media_type}, not a video")
            rows = (
                await db.execute(
                    select(Frame.ts_ms, Frame.scene_number)
                    .where(Frame.asset_id == asset.id)
                    .order_by(Frame.ts_ms)
                )
            ).all()
        duration = asset.duration_s
        # One grouping for this tool and for build_selects (nle/selects.py),
        # so "the scene holding this frame" means the same in both.
        groups, numbered = scene_groups((int(ts), scene) for ts, scene in rows)
        scenes = [
            Scene(
                scene=position + 1,
                start_s=start_s,
                end_s=end_s,
                frame_ms=sampled[0],
                sampled_ms=sampled,
                frame_url=_signed(who.base_url, asset.id, "frame", f"ts={sampled[0]}"),
            )
            for position, (sampled, (start_s, end_s)) in enumerate(
                zip(groups, scene_ranges(groups, duration), strict=True)
            )
        ]
        note = (
            "Scene changes as detected."
            if numbered
            else "No scene detection for this clip; these are evenly sampled moments."
        )
        if not scenes:
            note = "No frames have been sampled from this video yet."
        return ScenesResult(
            id=str(asset.id),
            filename=asset.filename,
            duration_s=duration,
            scenes=scenes,
            note=note,
        )

    @server.tool()
    async def build_rough_cut(
        title: str,
        cuts: list[CutIn],
        profile: str | None = None,
        still_s: float = DEFAULT_STILL_S,
    ) -> RoughCut:
        """Lay clips end to end into an FCP7 XML sequence that Premiere Pro
        and DaVinci Resolve open as a timeline (Final Cut Pro 10/11 cannot:
        it needs the file converted to FCPXML first, e.g. with 7toX, so say
        so rather than promising a Final Cut timeline). Each cut is an asset
        id with in_s/out_s in seconds of its source (a photograph is held for
        out_s - in_s, or `still_s`). Pass the `profile` of the computer that
        will open it (path_profiles) so the clips link to the originals there.
        Save the returned xml as a .xml file next to the project."""
        from framefound.nle.fcp7 import Clip, Cut, build_sequence

        who = principal()
        if not cuts:
            raise ToolError("Give at least one cut")
        if len(cuts) > MAX_CUTS:
            raise ToolError(f"At most {MAX_CUTS} cuts in one sequence")
        still_s = min(max(still_s, 0.5), 60.0)
        async with open_session() as db:
            profiles = await _profile_map(db, profile)
            libraries = {lib.id: lib for lib in (await db.execute(select(Library))).scalars()}
            assets: dict[str, Asset] = {}
            for cut in cuts:
                if cut.id not in assets:
                    assets[cut.id] = await _asset(db, cut.id)

        built: list[Cut] = []
        unmapped: list[str] = []
        for cut in cuts:
            asset = assets[cut.id]
            if asset.media_type == "audio":
                raise ToolError(f"{asset.filename} is audio; a rough cut takes video and photos")
            path = _local_path(asset, profiles) if profiles else None
            if path is None:
                library = libraries.get(asset.library_id)
                root = library.root_path.rstrip("/") if library else ""
                path = f"{root}/{asset.relative_path}"
                if asset.filename not in unmapped:
                    unmapped.append(asset.filename)
            clip = Clip(
                name=asset.filename,
                path=path,
                duration_s=asset.duration_s,
                fps=asset.fps,
                width=asset.width,
                height=asset.height,
                has_audio=_has_audio(asset),
            )
            if asset.media_type == "image":
                hold = (cut.out_s - cut.in_s) if cut.out_s is not None else still_s
                built.append(Cut(clip, 0.0, max(0.5, hold), is_still=True, note=cut.note))
                continue
            out_s = cut.out_s if cut.out_s is not None else (asset.duration_s or cut.in_s)
            if asset.duration_s is not None:
                out_s = min(out_s, asset.duration_s)
            if out_s <= cut.in_s:
                raise ToolError(
                    f"{asset.filename}: out_s ({out_s:g}) must be after in_s ({cut.in_s:g})"
                    + (f"; the clip is {asset.duration_s:g}s long" if asset.duration_s else "")
                )
            built.append(Cut(clip, cut.in_s, out_s, note=cut.note))

        xml = build_sequence(title.strip() or "FrameFound rough cut", built)
        total = sum(c.out_s - c.in_s for c in built)
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "-" for ch in title).strip()
        note = f"{len(built)} cuts, {total:.1f}s."
        if unmapped:
            note += (
                f" {len(unmapped)} file(s) have no path for "
                + (f"profile {profile!r}" if profile else "any profile (none given)")
                + "; they use the server's path and will import offline until relinked."
            )
        log.info("mcp.rough_cut", cuts=len(built), seconds=round(total, 1), by=who.token_name)
        return RoughCut(
            title=title,
            filename=f"{safe or 'rough-cut'}.xml",
            profile=profile,
            duration_s=round(total, 3),
            cuts=len(built),
            xml=xml,
            unmapped=unmapped,
            note=note,
        )

    @server.tool()
    async def build_selects(
        title: str,
        query: str | None = None,
        moments: list[MomentIn] | None = None,
        library: str | None = None,
        profile: str | None = None,
        handles_s: float = DEFAULT_HANDLES_S,
        max_moment_s: float = DEFAULT_MAX_PICTURE_S,
        max_speech_s: float = DEFAULT_MAX_SPEECH_S,
        still_s: float = DEFAULT_STILL_S,
        order: Order = "rank",
        limit: int = 25,
    ) -> SelectsResult:
        """A selects reel for Premiere Pro: one FCP7 XML with a bin of the
        source clips (a marker at each selected moment, its comment saying why:
        the sentence said, the tag, or "visual: <query>") and a "Selects"
        sequence stringing the moments end to end with handles. Use it when
        the person wants material to review and cut from, not a finished edit
        (that is build_rough_cut).

        Give `query` to select from a search (what was said gives each whole
        sentence, up to max_speech_s; what it shows gives the detected shot,
        up to max_moment_s; photos hold for still_s), or `moments` you chose
        (asset id plus at_s, or in_s/out_s). Pass the `profile` of the
        computer that will open it (path_profiles) — without it every clip
        imports offline. order="captured" runs it by shooting date (year-end
        videos). Save the xml as the returned filename and import it with
        File > Import. Tell the person about anything in `unmapped` or
        `offline`."""
        from framefound.api.v1.selects import SelectsError, SelectsRequest, assemble

        who = principal()
        async with open_session() as db:
            from framefound.mcp.server import _library_id

            if profile and profile.strip():
                await _profile_map(db, profile)  # a clear error for a wrong name
            try:
                request = SelectsRequest(
                    title=title,
                    profile=(profile or "").strip(),
                    query=(query or "").strip(),
                    library_id=await _library_id(db, library),
                    moments=moments or [],
                    handles_s=min(max(handles_s, 0.0), 10.0),
                    max_moment_s=min(max(max_moment_s, 1.0), 120.0),
                    max_speech_s=min(max(max_speech_s, 2.0), 120.0),
                    still_s=min(max(still_s, 0.5), 60.0),
                    order=order,
                    limit=min(max(limit, 1), 60),
                )
                result = await assemble(db, request)
            except SelectsError as exc:
                raise ToolError(str(exc)) from exc
        log.info("mcp.selects", selects=len(result.selects), by=who.token_name)
        return result
