"""Seed a local SQLite catalogue with generated media, to look at the UI.

    python infrastructure/scripts/seed_dev.py <root>

`<root>` is the directory holding `ui.db` (run `alembic upgrade head` against
it first), with `data/` and `media/` created here — see CONTRIBUTING.md
"Looking at the UI with a seeded catalogue".

Three libraries that look like the real ones (Intel shoots, GELCO courses,
family storage), real JPEG originals under the media root so the editor can
open them, thumbnails/previews/frames under the data dir, faces, tags, two
listings, path profiles. Everything is synthetic; nothing leaves this machine.
Every run, `--local-ai-only` included, gives a listing photograph seeded with
an old room key ("exterior", "living-room") the room list's own
(`correct_rooms`), so a catalogue seeded before that was fixed shows its rooms.

The AI models page's demo rows come last (local_models/demo.py `seed`): run
the API with `FRAMEFOUND_LOCAL_AI_DEMO=1` and `FRAMEFOUND_MODELS_DIR=<root>/models`
to see them. On a catalogue seeded before they existed,

    python infrastructure/scripts/seed_dev.py <root> --local-ai-only

adds only those (and nothing if they are there already, except a Test of the
Editor waiting for the overnight window when the last one has run, and the
GGUF build for the second program on a catalogue seeded before it existed).
On a catalogue that already has models it runs no job: the GGUF build's
download is left queued for the review server, beside whatever a reviewer
has waiting there.

Every run, `--local-ai-only` included, then adds what the colour pages show
on first load (`seed_colour`, L4 design 8.6): three of the local model's
proposals on the "130 Davis Rd Auction" listing (one mild, one large, one
with a sky worth replacing), a suggested look on the first GELCO clip by
folder and name and a look chosen by hand on the second. Each is added once
per catalogue and never put back. None of it is a model or a job, so the AI
models page the demo's tests pin is unchanged.

Then, also on every run, what the Cuts pages show on first load (`seed_cuts`,
L5 design 8.9): three shot windows with their stills on each GELCO clip that
has none (so a draft can be asked for from a seeded folder), an imported
timeline ("Pete Dye Quick 30"), a rough cut in two versions ("Pete Dye — 30
second teaser": a local draft with the reasons its Editor gave and two things
the check changed, and a revision of it, so Compare versions has something
to compare) and a draft that failed because the Editor answered in prose.
The cut, the timeline and the failed draft are each added once per catalogue
and never put back; none is a model or a job either.

Then, on every run again, the one thing a folder review by the local model
needs beyond that (`seed_looking`, L3 design 8.9): a ready motion file for
each clip that has shot windows, which is how the catalogue knows a clip's
shots have been found. With it, **Review with AI** on a GELCO folder has
shots to look at and a clip's shot list has rows; without it the clips read
as needing a playable copy first, which the seed cannot make. It adds no
look: those are made by reviewing a folder in demo mode. Once per catalogue,
like the parts above; a clip that gets its shot windows from a later run gets
its motion file with them, so the seed never leaves a clip with one and not
the other.

With `--compare` (alone or beside `--local-ai-only`), Compare's too
(`seed_compare`, L2 design 8.9): a few decided tags, two more demo models
that read pictures (one by day, one only overnight) and one finished
comparison of two models, over the shot windows the Cuts part made, with the
shots its imported timeline used counted as used. It adds whichever of those
is missing, finishes a demo model whose download an earlier run left
unfinished, and adds nothing the second time. It is asked for, not done by
default, so a plain run gives the AI models page exactly the four models and
seven ended jobs of the demo's own seed (`tests/test_local_models_demo.py`
pins them).

    python infrastructure/scripts/seed_dev.py <root> --compare
"""

import asyncio
import contextlib
import json
import math
import os
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from PIL import Image, ImageDraw
from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.db.models import (
    Asset,
    AssetTag,
    Derivative,
    Face,
    Frame,
    Library,
    Listing,
    ListingItem,
    PathMapping,
    Person,
    Tag,
)

random.seed(7)
ROOT = Path(sys.argv[1])
DATA = ROOT / "data"
MEDIA = ROOT / "media"
LOCAL_AI_ONLY = "--local-ai-only" in sys.argv[2:]
COMPARE = "--compare" in sys.argv[2:]
DB_URL = "sqlite+aiosqlite:///" + (ROOT / "ui.db").as_posix()

PALETTES = {
    "sky": [(70, 120, 190), (200, 220, 240), (240, 200, 140)],
    "turf": [(40, 90, 40), (90, 150, 60), (160, 190, 90)],
    "room": [(220, 200, 170), (160, 130, 100), (90, 70, 60)],
    "dusk": [(30, 30, 70), (190, 90, 60), (250, 190, 100)],
    "barn": [(150, 50, 40), (90, 60, 50), (200, 190, 170)],
}


def unit(seed: float) -> list[float]:
    vector = [math.cos(seed * (i + 1)) for i in range(8)]
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def scene(size: tuple[int, int], palette: str, label: str, faces: int = 0) -> Image.Image:
    w, h = size
    top, mid, bottom = PALETTES[palette]
    img = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        a, b = (top, mid) if t < 0.5 else (mid, bottom)
        u = t * 2 if t < 0.5 else (t - 0.5) * 2
        draw.line([(0, y), (w, y)], fill=tuple(int(a[i] + (b[i] - a[i]) * u) for i in range(3)))
    # Some "subject": a block and a horizon line, so photos differ visibly.
    draw.rectangle([w * 0.1, h * 0.55, w * 0.45, h * 0.9], fill=(60, 55, 50))
    draw.line([(0, h * 0.6), (w, h * 0.58)], fill=(255, 255, 255), width=2)
    for i in range(faces):
        cx, cy, r = w * (0.6 + 0.15 * i), h * 0.45, h * 0.09
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(230, 190, 160))
        draw.ellipse([cx - r * 0.4, cy - r * 0.2, cx - r * 0.2, cy], fill=(40, 30, 30))
        draw.ellipse([cx + r * 0.2, cy - r * 0.2, cx + r * 0.4, cy], fill=(40, 30, 30))
    draw.text((16, 16), label, fill=(255, 255, 255))
    return img


async def main() -> None:
    engine = create_async_engine(DB_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as db:
        libs = {}
        for name, folder in (("Intel", "intel"), ("GELCO", "gelco"), ("Grick Family Storage", "family")):
            root = MEDIA / folder
            root.mkdir(parents=True, exist_ok=True)
            lib = Library(name=name, root_path=str(root), scan_interval_minutes=60)
            db.add(lib)
            libs[name] = lib
        await db.flush()
        for lib, letter in ((libs["Intel"], "X:\\"), (libs["GELCO"], "Y:\\"), (libs["Grick Family Storage"], "W:\\")):
            db.add(PathMapping(library_id=lib.id, profile_name="DJ's PC", platform="windows", mapped_prefix=letter))
            db.add(PathMapping(library_id=lib.id, profile_name="Edit bay", platform="macos", mapped_prefix=f"/Volumes/{lib.name.split()[0]}"))

        shoots = [
            ("Intel", "2026/2026-10-02 130 Davis Rd Auction", "room", 14, (40.04, -76.30)),
            ("Intel", "2026/2026-09-18 Smyrna Rd Farm", "barn", 12, (39.98, -76.12)),
            ("Intel", "2026/2026-10-09 44 Mill Street", "room", 9, (40.11, -76.45)),
            ("GELCO", "LedgeRock", "turf", 10, (40.37, -75.93)),
            ("GELCO", "Pete Dye", "dusk", 8, (40.33, -74.60)),
            ("GELCO", "Hudson National", "sky", 7, (41.23, -73.95)),
            ("Grick Family Storage", "stef's computer files/2024 beach", "sky", 6, (39.35, -74.42)),
        ]
        assets: list[Asset] = []
        people_assets: list[Asset] = []
        for lib_name, folder, palette, count, (lat, lon) in shoots:
            lib = libs[lib_name]
            base = datetime(2026, 9, 1, 9, 0, tzinfo=UTC) + timedelta(days=random.randint(0, 40))
            for i in range(count):
                is_video = lib_name == "GELCO" and i % 4 == 3
                is_drone = lib_name != "Intel" or i % 3 == 0
                name = (f"DJI_{random.randint(100, 999):04d}_D" if is_drone else f"IMG_{random.randint(1000, 9999)}")
                ext = "mp4" if is_video else ("JPG" if is_drone else "jpg")
                rel = f"{folder}/{name}.{ext}"
                captured = base + timedelta(minutes=i * 7)
                width, height = (3840, 2160) if is_video else ((5280, 3956) if is_drone else (6000, 4000))
                asset = Asset(
                    library_id=lib.id,
                    relative_path=rel,
                    filename=f"{name}.{ext}",
                    extension=ext.lower(),
                    media_type="video" if is_video else "image",
                    size_bytes=random.randint(4_000_000, 48_000_000),
                    mtime=captured,
                    captured_at=captured,
                    width=width,
                    height=height,
                    duration_s=random.uniform(8, 95) if is_video else None,
                    fps=29.97 if is_video else None,
                    video_codec="hevc" if is_video else None,
                    camera_make="DJI" if is_drone else "Canon",
                    camera_model="FC8482" if is_drone else "Canon EOS R8",
                    gps_lat=lat + random.uniform(-0.002, 0.002),
                    gps_lon=lon + random.uniform(-0.002, 0.002),
                    gps_source="exif",
                    availability="online",
                    processing_status="ready",
                    first_indexed_at=now - timedelta(days=random.randint(0, 30)),
                )
                db.add(asset)
                await db.flush()
                assets.append(asset)
                # The original, small but real, so the editor can open it.
                faces = 2 if (lib_name == "GELCO" and not is_video and i % 5 == 1) else 0
                original = MEDIA / lib.root_path.split(os.sep)[-1] / rel
                original.parent.mkdir(parents=True, exist_ok=True)
                img = scene((1200, 800), palette, f"{folder} · {name}", faces)
                if is_video:
                    original.write_bytes(b"\x00" * 1024)  # a stand-in; nothing plays it here
                else:
                    img.save(original, "JPEG", quality=85)
                # Derivatives under the data dir.
                for kind, edge in (("thumbnail", 512), ("preview", 1600)):
                    small = img.copy()
                    small.thumbnail((edge, edge))
                    rel_d = f"derivatives/{str(asset.id)[:2]}/{asset.id}/{kind}.webp"
                    target = DATA / rel_d
                    target.parent.mkdir(parents=True, exist_ok=True)
                    small.save(target, "WEBP", quality=82)
                    db.add(Derivative(asset_id=asset.id, kind=kind, relative_path=rel_d, media_format="webp",
                                      width=small.width, height=small.height, status="ready", generated_at=now,
                                      size_bytes=target.stat().st_size))
                if is_video:
                    poster_rel = f"derivatives/{str(asset.id)[:2]}/{asset.id}/poster.webp"
                    img.copy().save(DATA / poster_rel, "WEBP")
                    db.add(Derivative(asset_id=asset.id, kind="poster", relative_path=poster_rel, media_format="webp",
                                      width=1200, height=800, status="ready", generated_at=now))
                # Frames: one for a still, several for a video.
                for n in range(6 if is_video else 1):
                    frel = f"frames/{asset.id}/{n * 10000}.jpeg"
                    ftarget = DATA / frel
                    ftarget.parent.mkdir(parents=True, exist_ok=True)
                    frame_img = img.copy()
                    frame_img.thumbnail((480, 480))
                    frame_img.save(ftarget, "JPEG", quality=80)
                    frame = Frame(asset_id=asset.id, ts_ms=n * 10000, relative_path=frel,
                                  embedding=unit(hash(folder) % 97 + n * 0.3 + i * 0.05),
                                  is_scene_change=(n == 0), scene_number=n, embedding_model="fake")
                    db.add(frame)
                    await db.flush()
                    if faces and n == 0:
                        people_assets.append(asset)
                        for k in range(faces):
                            db.add(Face(frame_id=frame.id, asset_id=asset.id, person_id=None,
                                        box_x=0.58 + 0.15 * k, box_y=0.36, box_w=0.08, box_h=0.14,
                                        detection_score=0.9 - 0.1 * k, embedding=unit(3.0 + k),
                                        source="detected"))
        await db.flush()

        # People: two named, one unnamed cluster, from the faces above.
        faces = (await db.execute(select(Face))).scalars().all()
        brian = Person(name="Brian Ley", slug="brian-ley", face_count=0, prototype=unit(3.0), threshold=0.42)
        crew = Person(name="", slug="", face_count=0, prototype=unit(4.0), threshold=0.42)
        db.add_all([brian, crew])
        await db.flush()
        for idx, face in enumerate(faces):
            if idx % 2 == 0:
                face.person_id = brian.id
                face.source = "confirmed" if idx % 4 == 0 else "detected"
                face.similarity = 0.7 + (idx % 3) * 0.08
                brian.face_count += 1
            else:
                face.person_id = crew.id
                face.similarity = 0.6
                crew.face_count += 1
        if faces:
            brian.cover_face_id = faces[0].id

        # Tags.
        tags = {}
        for name in ("drone", "exterior", "kitchen", "golf course", "sunset"):
            tag = Tag(name=name, slug=name.replace(" ", "-"))
            db.add(tag)
            tags[name] = tag
        await db.flush()
        for asset in assets:
            if "DJI" in asset.filename:
                db.add(AssetTag(asset_id=asset.id, tag_id=tags["drone"].id, source="manual"))
            if "GELCO" in asset.relative_path or asset.library_id == libs["GELCO"].id:
                db.add(AssetTag(asset_id=asset.id, tag_id=tags["golf course"].id, source="suggested", confidence=0.71))
            if "Davis" in asset.relative_path and "IMG" in asset.filename:
                db.add(AssetTag(asset_id=asset.id, tag_id=tags["kitchen"].id, source="manual"))

        # A listing from the Davis Rd shoot, and a showcase-style one.
        davis = [a for a in assets if "Davis" in a.relative_path]
        listing = Listing(name="130 Davis Rd Auction", file_suffix="130-davis-rd-auction", notes="MLS set for Oct 20")
        db.add(listing)
        await db.flush()
        for position, asset in enumerate(davis):
            room = DAVIS_ROOMS[position % len(DAVIS_ROOMS)]
            db.add(ListingItem(listing_id=listing.id, asset_id=asset.id, position=position,
                               room=room, room_source="suggested",
                               room_score=0.8, caption=_davis_caption(room)))
        gelco = [a for a in assets if a.library_id == libs["GELCO"].id and a.media_type == "image"][:12]
        calendar = Listing(name="GELCO Masterpiece Calendar — shortlist", file_naming="place")
        db.add(calendar)
        await db.flush()
        for position, asset in enumerate(gelco):
            place = asset.relative_path.split("/")[0].lower().replace(" ", "-")
            db.add(ListingItem(listing_id=calendar.id, asset_id=asset.id, position=position,
                               slug=f"{place}-{position + 1:02d}-fall-drone", caption=f"{asset.relative_path.split('/')[0]} — Fall · Drone · October 2026"))
        await db.commit()
    await engine.dispose()
    print(f"seeded {len(assets)} assets, {len(faces)} faces, 2 listings")


# The Davis Rd photographs' suggested rooms, in gallery order. Keys from the
# room list (framefound/ai/rooms.py): a key not in it reads as no room at all,
# the tile's select showing "— clear label —" and the viewer "no room label
# yet", which is what catalogues seeded before 2026-11-14 show.
DAVIS_ROOMS = ["front_exterior", "kitchen", "living_room", "bedroom", "bathroom", "garage", "",
               "front_exterior"]
# What those catalogues were given instead, and the key each one meant.
OLD_ROOM_KEYS = {"exterior": "front_exterior", "living-room": "living_room"}


def _davis_caption(room: str) -> str:
    from framefound.ai.rooms import ROOM_LABELS

    return f"Davis Rd — {ROOM_LABELS[room] if room else 'view'}"


async def correct_rooms() -> None:
    """Give a catalogue seeded with the old room keys the room list's own.

    Every run, because `main` does not run again on a catalogue seeded before.
    Only the old keys change: the API refuses a room not in the list, so a
    listing item holding one was given it by this seed, never by a person. A
    caption changes only while it is still the one the seed wrote with it.
    """
    engine = create_async_engine(DB_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    corrected = 0
    try:
        async with factory() as db:
            items = (await db.scalars(
                select(ListingItem).where(ListingItem.room.in_(list(OLD_ROOM_KEYS))))).all()
            for item in items:
                if item.caption == f"Davis Rd — {item.room}":
                    item.caption = _davis_caption(OLD_ROOM_KEYS[item.room])
                item.room = OLD_ROOM_KEYS[item.room]
                corrected += 1
            await db.commit()
    finally:
        await engine.dispose()
    if corrected:
        print(f"Rooms: gave {corrected} listing photographs the room list's own key for their room.")


async def seed_local_ai() -> None:
    """The AI models page in demo mode: models, roles, the software, a Test, jobs."""
    # Demo mode under ROOT, as CONTRIBUTING.md runs the API. Put in the
    # environment as well, because the code the demo runs reads the settings
    # for itself in places; nothing must land outside ROOT.
    for name in ("FRAMEFOUND_RUNTIMES_DIR", "FRAMEFOUND_DOMAIN", "FRAMEFOUND_BULK_DIR"):
        os.environ.pop(name, None)
    os.environ.update(
        {
            "FRAMEFOUND_DATABASE_URL": DB_URL,
            "FRAMEFOUND_DATA_DIR": str(DATA),
            "FRAMEFOUND_MEDIA_ROOT": str(MEDIA),
            "FRAMEFOUND_MODELS_DIR": str(ROOT / "models"),
            "FRAMEFOUND_LOCAL_AI_DEMO": "1",
        }
    )
    from framefound.config import get_settings
    from framefound.local_models import demo

    get_settings.cache_clear()
    engine = create_async_engine(DB_URL)
    try:
        said = await demo.seed(async_sessionmaker(engine, expire_on_commit=False), get_settings())
    finally:
        await engine.dispose()
    print(said)


# --- Colour (L4 design 8.6) -----------------------------------------------------------
# What the clip card and the listing's "What the local model suggests" panel
# show on first load, so a reviewer sees each state without pressing Suggest
# or Ask first. Made by the demo's Looker, as pressing those would be, and
# marked demo. Each of the three parts is added once per catalogue and then
# remembered (local_models/demo.py `note_seeded`), so nothing a reviewer did
# is undone by seeding again: a proposal applied or dismissed, a suggestion
# replaced, a choice cleared (which deletes its row, so the rows alone could
# not say it had ever been there). A listing a model has already looked at,
# and a clip that has any look of its own, were a reviewer's before the seed
# came: they are left as they are and remembered the same way. Found again by
# the photographs' folder, which nobody can rename.

DAVIS_FOLDER = "2026/2026-10-02 130 Davis Rd Auction"
PROPOSALS_PART, SUGGESTED_PART, CHOSEN_PART = "colour_proposals", "colour_suggested", "colour_chosen"
# The listing's first three photographs in gallery order, as photo_judge
# cleans an answer: (rotate, keystone, sky worth replacing, caption, slug,
# note). One mild, one large (3 degrees or more), one with a sky to replace.
PROPOSALS = (
    (1.2, 0.0, False, "Front exterior over the front lawn", "front-exterior-lawn",
     "Levelled the roofline."),
    (4.0, 0.7, False, "Kitchen with island", "kitchen-island", "Strong lean; look before applying."),
    (-0.5, 0.0, True, "Rear yard and deck", "rear-yard-deck", "The sky is flat grey."),
)
# A mild look, as a model would suggest it: every slider one a LUT carries.
SUGGESTED_LOOK = {"exposure": 0.15, "contrast": 0.1, "vibrance": 0.15, "temperature": 0.04}
SUGGESTED_REASON = "A little brighter and warmer; the greens read flat as shot."
# Sliders a person set by hand ("Adjust before using", then Use).
CHOSEN_LOOK = {"exposure": -0.1, "contrast": 0.08, "saturation": 0.12}


async def _provenance(db, prompt_version: str, repo_id: str | None = None) -> dict[str, str | None]:
    """One of the demo's models as a stored answer names it (the five
    provenance names and variant): the Looker unless another is named. Its
    row is the plain seed's; without one, its name."""
    from framefound.db.models import LocalModel
    from framefound.local_models import demo

    repo_id = repo_id or demo.LOOKER_MODEL
    model = await db.scalar(select(LocalModel).where(LocalModel.repo_id == repo_id).limit(1))
    return {
        "model": repo_id,
        "model_revision": model.revision if model is not None else None,
        "variant": (model.variant or "") if model is not None else "",
        "runtime": model.runtime if model is not None else "demo",
        "runtime_version": demo.SEED_VERSIONS[-1],
        "prompt_version": prompt_version,
    }


async def _proposals(db, seen: frozenset[str]) -> tuple[str, str]:
    """The three proposals on the Davis Rd listing ("added" | "there" | "not
    added", what to say). There already when a seed added them before, or
    when the listing has any suggestion at all, in whatever state: a reviewer
    who asked, applied or dismissed has seen the panel, and re-seeding must
    not undo what they did."""
    from framefound.db.models import ListingEditSuggestion
    from framefound.local_models import demo
    from framefound.media import photo_judge

    if PROPOSALS_PART in seen:
        return "there", ""
    listing = await db.scalar(
        select(Listing).join(ListingItem, ListingItem.listing_id == Listing.id)
        .join(Asset, Asset.id == ListingItem.asset_id)
        .where(Asset.relative_path.startswith(DAVIS_FOLDER + "/"))
        .order_by(Listing.created_at, Listing.id).limit(1))
    if listing is None:
        return "not added", "the listing's proposals (no listing holds the Davis Rd photographs)"
    if await db.scalar(select(ListingEditSuggestion.id)
                       .where(ListingEditSuggestion.listing_id == listing.id).limit(1)):
        await demo.note_seeded(db, PROPOSALS_PART)
        return "there", ""
    items = (await db.execute(
        select(ListingItem.id, ListingItem.asset_id).join(Asset, Asset.id == ListingItem.asset_id)
        .where(ListingItem.listing_id == listing.id, Asset.media_type == "image")
        .order_by(ListingItem.position, ListingItem.id).limit(len(PROPOSALS)))).all()
    if len(items) < len(PROPOSALS):
        return "not added", f"the proposals on {listing.name} (it has fewer than three photographs)"
    said = await _provenance(db, photo_judge.PROMPT_VERSION)
    for (item_id, asset_id), (rotate, keystone, sky, caption, slug, note) in zip(items, PROPOSALS):
        db.add(ListingEditSuggestion(listing_id=listing.id, item_id=item_id, asset_id=asset_id,
                                     rotate=rotate, keystone=keystone, sky_worth_replacing=sky,
                                     caption=caption, slug=slug, note=note, state="proposed",
                                     demo=True, **said))
    await demo.note_seeded(db, PROPOSALS_PART)
    return "added", f"three of the local model's proposals on {listing.name}"


async def _reference_still(db, video: Asset):
    """The still a clip's looks are measured on (the clip card's own rule:
    the middle of its longest shot, else the sampled frame nearest its
    middle), and the moment it shows; (None, -1) when there is none. Never
    grabs from a proxy: the seed has none."""
    from framefound.db.models import ShotWindow
    from framefound.media import clip_look
    from framefound.processing import shots

    windows = [(int(start), int(end)) for start, end in (await db.execute(
        select(ShotWindow.start_ms, ShotWindow.end_ms).where(ShotWindow.asset_id == video.id))).all()]
    frames = [int(ts) for ts in await db.scalars(select(Frame.ts_ms).where(Frame.asset_id == video.id))]
    reference = clip_look.reference_moment(
        windows, frames, int(video.duration_s * 1000) if video.duration_s else None)
    if reference is None:
        return None, -1
    return await shots.still_at(db, video, reference, data_dir=DATA)


async def _clip_looks(db, seen: frozenset[str]) -> list[tuple[str, str]]:
    """A suggested look on the first GELCO clip by path (its folder, then its
    name) and a chosen one (set by hand) on the second. Each once: not where a
    seed added it before, whatever became of it since, and not on a clip that
    has a look of any kind, which a reviewer asked for or chose."""
    from framefound.db.models import ClipLook
    from framefound.local_models import demo
    from framefound.media import clip_look, develop
    from framefound.processing import colour_jobs

    parts = ((colour_jobs.SUGGESTED, SUGGESTED_PART), ("chosen", CHOSEN_PART))
    if all(part in seen for _kind, part in parts):
        return [("there", "")]
    library = await db.scalar(select(Library).where(Library.name == "GELCO"))
    videos = [] if library is None else list((await db.scalars(
        select(Asset).where(Asset.library_id == library.id, Asset.media_type == "video",
                            Asset.proxy_of_id.is_(None))
        .order_by(Asset.relative_path).limit(2))).all())
    if len(videos) < 2:
        return [("not added", "the clip looks (this catalogue has fewer than two GELCO clips)")]
    outcomes = []
    for video, (kind, part) in zip(videos, parts):
        if part in seen:
            outcomes.append(("there", ""))
            continue
        if await db.scalar(select(ClipLook.id).where(ClipLook.asset_id == video.id).limit(1)):
            await demo.note_seeded(db, part)
            outcomes.append(("there", ""))
            continue
        still, shown = await _reference_still(db, video)
        look = "a suggested look" if kind == colour_jobs.SUGGESTED else "a look set by hand"
        # By its folder too: the two clips are the first by path, not by name.
        what = f"{look} on {video.relative_path}"
        if still is None or shown < 0:
            outcomes.append(("not added", f"{what} (it has no still to measure a look on)"))
            continue
        if kind == colour_jobs.SUGGESTED:
            await colour_jobs.adopt(db, video.id, SUGGESTED_LOOK, reason=SUGGESTED_REASON,
                                    still_ts_ms=shown, still=still,
                                    provenance=await _provenance(db, clip_look.PROMPT_VERSION),
                                    demo=True)
        else:
            recipe = clip_look.clean_look(CHOSEN_LOOK)
            db.add(ClipLook(asset_id=video.id, kind=kind, source="custom", recipe=recipe,
                            measured=develop.measure(still, recipe), left_out=[],
                            still_ts_ms=shown, features=clip_look.features_of(still)))
        await demo.note_seeded(db, part)
        outcomes.append(("added", what))
    return outcomes


async def seed_colour() -> None:
    """The colour pages (after `seed_local_ai`, whose settings it uses)."""
    from sqlalchemy.exc import OperationalError

    from framefound.local_models import demo

    engine = create_async_engine(DB_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            seen = await demo.seeded_parts(db)
            outcomes = [await _proposals(db, seen), *await _clip_looks(db, seen)]
            await db.commit()
    except OperationalError as err:
        print(f"Colour: the catalogue's tables are older than this code ({err.orig}). "
              "Run `alembic upgrade head` against ui.db first.")
        return
    finally:
        await engine.dispose()
    added = [said for how, said in outcomes if how == "added"]
    missing = [said for how, said in outcomes if how == "not added"]
    if added:
        print("Colour: added " + "; ".join(added) + ".")
    if missing:
        print("Colour: not added: " + "; ".join(missing) + ".")
    if not added and not missing:
        print("Colour: everything it needs is already in this catalogue; nothing was added.")


# --- Cuts (L5 design 8.9) ---------------------------------------------------------------
# What the Cuts pages show on first load: a rough cut in two versions (a local
# draft, and a revision of it kept beside it as Claude's are, so Compare
# versions has two to set side by side), an imported timeline standing alone,
# and a draft that failed. The local draft is made as pressing Draft makes
# one, short of asking a model: the real chooser picks the shots the Editor is
# offered, an answer written here goes through the real check (which finds
# the two faults written into it and says so), and the cut is written by the
# one function every cut is written by. No job row is made, so the AI models
# page's list of jobs stays what the demo's own seed left.
#
# Each of the three is added once per catalogue and then remembered
# (local_models/demo.py `note_seeded`): a version deleted on a cut's page, a
# failed draft cleared from the list, a timeline deleted, all stay gone when
# the catalogue is seeded again. The shot windows are not such a part: a clip
# with none gets them.

FAMILY_PART, IMPORT_PART, FAILED_PART = "cuts_family", "cuts_import", "cuts_failed"
TEASER = "Pete Dye — 30 second teaser"
TEASER_FOLDER = "Pete Dye"
TEASER_BRIEF = "Thirty seconds of smooth aerials over the course at golden hour, opening wide."
# The seeded draft needs the folder's two clips of three shots each.
TEASER_SHOTS = 6
FAILED_FOLDER = "LedgeRock"
FAILED_BRIEF = "A calm minute of LedgeRock in morning light: slow moves only, nobody in shot."
# What a model that ignored the form writes: a paragraph, where a cut was asked for.
FAILED_ANSWER = (
    "Here is how I would cut a calm minute of LedgeRock. Open on the long glide over the "
    "fairway, because it is the steadiest move in the folder, and let it run. Then the push-in "
    "toward the green, held until the flag is centred. The orbit to the left gives some variety "
    "in the middle, and I would close on the glide from the second clip so the piece ends as "
    "quietly as it began. Tell me if you would like it shorter, or with quicker cuts."
)


def _drop_event(_logger, _method, _event):
    raise structlog.DropEvent


@contextlib.contextmanager
def _hushed(module):
    """`module`'s own log lines held back while the seed calls into it.

    Its logger is stood aside for one that drops every event, and put back.
    structlog's `capture_logs` did this before and worked only by luck of
    order: it empties the processors of the configuration in force now, and
    a logger keeps the list it was first used with (`cache_logger_on_first_use`).
    The API configures logging every time it is built, so in a process that
    had built it since the writer first logged (the tests, one file after
    another) the writer went on printing among the seed's lines."""
    kept = module.log
    module.log = structlog.wrap_logger(None, processors=[_drop_event])
    try:
        yield
    finally:
        module.log = kept


def _editor_answer(candidates):
    """What the Editor "wrote" for the teaser: seven cuts in timeline order,
    as `ai/cut_editor.EditorAnswer`, and the one shot of the six that the
    revision leaves out.

    Two of the seven are the mistakes a small model makes, so the check's
    sentences are on the draft for a reviewer to read: one names a shot that
    is not on the list, one runs past the end of its shot. Seconds are to a
    tenth, as a model writes them, and held inside each shot except where the
    fault is the point.

    The shot left out of the revision is the last by clip name and start: the
    one the imported timeline did not use either (`_quick_30` takes the first
    five in that order). So the shots finished work has used are the same
    five with or without this cut, and Compare's seeded comparison, which is
    drawn afterwards and counts them, reads the same figures either way.
    """
    from framefound.ai import cut_editor

    first, second, third, fourth, fifth, spare = sorted(
        candidates, key=lambda c: (c.filename, c.start_s))[:TEASER_SHOTS]

    def move(shot) -> str:
        # The move in the word the shot's own line uses ("0.0-25.9s pull-out smooth 0.97 ...").
        return shot.line.split()[1]

    def cut(shot, offset: float, length: float, reason: str):
        # In rounded up and out rounded down, so neither leaves the shot.
        end = math.floor(shot.end_s * 10) / 10
        in_s = math.ceil((shot.start_s + offset) * 10) / 10
        if in_s + length > end:  # a short shot: as late as the cut can start
            in_s = max(math.ceil(shot.start_s * 10) / 10, end - length)
        return cut_editor.EditorCut(shot=shot.n, in_s=round(in_s, 1),
                                    out_s=round(min(in_s + length, end), 1), reason=reason)

    not_listed = max(shot.n for shot in candidates) + 3
    late = math.floor(spare.end_s * 10) / 10 - 4.0
    cuts = [
        cut(first, 1.0, 5.0, f"Opens wide on the {move(first)}; the course comes into view."),
        cut(fourth, 2.0, 4.5, f"A steady {move(fourth)} to follow the opening."),
        cut(second, 1.5, 5.0, "The warmest light in the folder."),
        cut_editor.EditorCut(shot=not_listed, in_s=2.0, out_s=6.0,
                             reason="The clubhouse from the north side."),
        cut(fifth, 3.0, 5.0, f"A slower {move(fifth)} before the close."),
        cut_editor.EditorCut(shot=spare.n, in_s=round(late, 1), out_s=round(late + 5.5, 1),
                             reason="The far green, held a little longer."),
        cut(third, 1.0, 5.5, f"Closes on the {move(third)}."),
    ]
    return cut_editor.EditorAnswer(title="", cuts=cuts, notes=""), spare


def _revision(pieces, candidates, spare):
    """The second version, as an editor revising the draft would leave it:
    (clip, in, out, why) in timeline order. The opening and the close kept,
    one shot moved up, one trimmed, one stretch added from the opening shot
    where it has room, and the piece from `spare` left out — so comparing the
    two versions shows a shot of every kind."""
    by_number = {shot.n: shot for shot in candidates}
    kept = [piece for piece in pieces if piece.n != spare.n]
    opening, second, third, fourth, closing = kept
    opened = by_number[opening.n]
    # A second and a half off the end; less from a piece too short to give it.
    trim = min(1.5, second.length / 3)
    revised = [
        (opening.asset_id, opening.in_s, opening.out_s, "Kept as the opening."),
        (third.asset_id, third.in_s, third.out_s, "Moved up: it follows the opening better."),
        (second.asset_id, second.in_s, second.out_s - trim, "Trimmed: it drifts at the end."),
    ]
    again = opening.out_s + 3.0
    if again + 4.0 <= opened.end_s - 0.5:
        revised.append((opening.asset_id, again, again + 4.0,
                        "Added: the same move further on, where the course fills the frame."))
    revised += [
        (fourth.asset_id, fourth.in_s, fourth.out_s, "Kept."),
        (closing.asset_id, closing.in_s, closing.out_s, "Kept as the close."),
    ]
    return revised


async def _imported(db, library: Library, videos: list[Asset], seen: frozenset[str]) -> tuple[str, str]:
    """The imported timeline, once ("added" | "there" | "not added", what to
    say). Found again by its pieces, whatever it is called now, on a
    catalogue that had it before the seed kept a note; never put back once a
    reviewer has deleted it on the Cuts page."""
    from framefound.local_models import demo

    if IMPORT_PART in seen:
        return "there", ""
    how = await _quick_30(db, library, videos)
    if how == "not added":
        return how, f"the imported timeline (the {TEASER_FOLDER} folder has no analysed shots)"
    await demo.note_seeded(db, IMPORT_PART)
    return how, f"the imported timeline {QUICK_30}"


async def _teaser(db, library: Library, seen: frozenset[str]) -> tuple[str, str]:
    """The rough cut in two versions ("added" | "there" | "not added", what to say).

    Version 1 is a local draft with its request (`cut_drafts`, done, the shots
    it was chosen from, what the check changed); version 2 revises it through
    the same function with `parent`, as Claude's `build_rough_cut` does. The
    draft marks no shot used (nobody downloaded it); the revision does, as
    any cut Claude builds."""
    from framefound.ai import cut_editor
    from framefound.db.models import Cut, CutDraft, CutPiece
    from framefound.local_models import demo
    from framefound.nle import drafts, roughcut

    if FAMILY_PART in seen:
        return "there", ""
    what = "the Pete Dye teaser"
    request = drafts.DraftRequest(
        brief=TEASER_BRIEF, title=TEASER, target_s=30.0,
        scope=drafts.Scope(kind="folder", library_id=library.id, folder=TEASER_FOLDER))
    try:
        offer = await drafts.candidates_for(db, request)
    except drafts.DraftError as err:
        return "not added", f"{what} (nothing to draft it from: {err})"
    if len(offer.candidates) < TEASER_SHOTS:
        return "not added", (f"{what} (the {TEASER_FOLDER} folder has fewer than "
                             f"{TEASER_SHOTS} analysed shots)")
    limits = drafts.constraints(request)
    answer, spare = _editor_answer(offer.candidates)
    pieces, problems = cut_editor.check(answer, offer.candidates, limits)
    if len(pieces) != TEASER_SHOTS:
        return "not added", f"{what} (this catalogue's {TEASER_FOLDER} shots are too short for it)"
    clips = {asset.id: asset for asset in await db.scalars(
        select(Asset).where(Asset.id.in_({piece.asset_id for piece in pieces})))}
    said = await _provenance(db, cut_editor.PROMPT_VERSION, demo.EDITOR_MODEL)
    # As if asked for yesterday and revised two hours later.
    asked = datetime.now(UTC) - timedelta(hours=26)
    made: list[uuid.UUID] = []
    try:
        # The writer logs each cut it builds; among the seed's own lines that
        # is noise, so its log lines are held back while it writes these two.
        with _hushed(roughcut):
            first = await roughcut.build_rough_cut(
                db, TEASER,
                [roughcut.CutIn(id=str(piece.asset_id), in_s=piece.in_s, out_s=piece.out_s,
                                note=piece.reason) for piece in pieces],
                source="local", must_record=True, by="seed",
                provenance={name: value for name, value in said.items() if value is not None},
                sequence_fps=roughcut.majority_rate(
                    [(clips[piece.asset_id].fps, piece.length) for piece in pieces]))
        made.append(uuid.UUID(first.cut_id))
        db.add(CutDraft(
            state="done", title=TEASER, brief=TEASER_BRIEF,
            scope=drafts.scope_json(request, offer.label), target_s=limits.target_s,
            min_shot_s=limits.min_shot_s, max_shot_s=limits.max_shot_s,
            candidates=[shot.as_json() for shot in offer.candidates],
            shots_in_scope=offer.shots_in_scope, clips=offer.clips,
            answer_text=answer.model_dump_json(), problems=problems, cut_id=made[0],
            seconds=38.4, demo=True, created_at=asked, started_at=asked + timedelta(seconds=2),
            finished_at=asked + timedelta(seconds=41), **said))
        with _hushed(roughcut):
            second = await roughcut.build_rough_cut(
                db, TEASER,
                [roughcut.CutIn(id=str(asset_id), in_s=in_s, out_s=out_s, note=why)
                 for asset_id, in_s, out_s, why in _revision(pieces, offer.candidates, spare)],
                source="mcp", must_record=True, by="seed", parent=first.cut_id)
        made.append(uuid.UUID(second.cut_id))
        for cut_id, when in zip(made, (asked + timedelta(seconds=41), asked + timedelta(hours=2))):
            await db.execute(update(Cut).where(Cut.id == cut_id).values(created_at=when))
        await demo.note_seeded(db, FAMILY_PART)
        await db.commit()
    except BaseException:
        # Each version is committed as it is written, and the request with
        # the second: half a family with no note would be added to again by
        # the next seed. The family goes by its root, not by what got as far
        # as `made`, so a revision the writer kept before it failed goes too.
        await db.rollback()
        if made:
            family = list((await db.scalars(select(Cut.id).where(
                or_(Cut.id == made[0], Cut.root_id == made[0])))).all())
            await db.execute(delete(CutDraft).where(CutDraft.cut_id.in_(family)))
            await db.execute(delete(CutPiece).where(CutPiece.cut_id.in_(family)))
            await db.execute(delete(Cut).where(Cut.id.in_(family)))
            await db.commit()
        raise
    return "added", (f"{what} in two versions (a local draft of {len(pieces)} shots, "
                     "and a revision of it)")


async def _failed_draft(db, library: Library, seen: frozenset[str]) -> tuple[str, str]:
    """A draft that failed because the Editor answered in prose, with what it
    wrote kept ("added" | "there" | "not added", what to say). It has no job
    row: a finished draft reads from its own row."""
    from framefound.ai import cut_editor
    from framefound.db.models import CutDraft
    from framefound.local_models import demo
    from framefound.nle import drafts

    if FAILED_PART in seen:
        return "there", ""
    what = "the draft that failed"
    request = drafts.DraftRequest(
        brief=FAILED_BRIEF,
        scope=drafts.Scope(kind="folder", library_id=library.id, folder=FAILED_FOLDER))
    try:
        offer = await drafts.candidates_for(db, request)
    except drafts.DraftError as err:
        return "not added", f"{what} (nothing to ask about: {err})"
    limits = drafts.constraints(request)
    asked = datetime.now(UTC) - timedelta(minutes=40)
    db.add(CutDraft(
        state="failed", message=drafts.NOT_A_FORM, title=drafts.short_title(FAILED_BRIEF),
        brief=FAILED_BRIEF, scope=drafts.scope_json(request, offer.label),
        target_s=limits.target_s, min_shot_s=limits.min_shot_s, max_shot_s=limits.max_shot_s,
        candidates=[shot.as_json() for shot in offer.candidates],
        shots_in_scope=offer.shots_in_scope, clips=offer.clips, answer_text=FAILED_ANSWER,
        seconds=27.9, demo=True, created_at=asked, started_at=asked + timedelta(seconds=2),
        finished_at=asked + timedelta(seconds=30),
        **await _provenance(db, cut_editor.PROMPT_VERSION, demo.EDITOR_MODEL)))
    await demo.note_seeded(db, FAILED_PART)
    return "added", "a draft that failed"


async def seed_cuts() -> None:
    """The Cuts pages (after `seed_local_ai`, whose settings and Editor it uses)."""
    from sqlalchemy.exc import OperationalError

    from framefound.local_models import demo

    engine = create_async_engine(DB_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            library = await db.scalar(select(Library).where(Library.name == "GELCO"))
            if library is None:
                print("Cuts: not added: the cuts (this catalogue has no GELCO library).")
                return
            seen = await demo.seeded_parts(db)
            videos = list((await db.execute(
                select(Asset).where(Asset.library_id == library.id, Asset.media_type == "video")
                .order_by(Asset.relative_path))).scalars().all())
            # Looking's part sweeps a catalogue for clips with windows and no
            # motion file once, and not again. So a clip that gets its windows
            # after that sweep gets its file here, in the same commit: windows
            # without the file is a clip a review asks about for ever
            # (docs/local-models.md, "A known fault").
            windows, files = await _shots(db, videos, with_motion=MOTION_PART in seen)
            outcomes = [await _imported(db, library, videos, seen)]
            # The windows and the timeline first, and kept: the draft is chosen
            # from the one and reads "used" off the other.
            await db.commit()
            outcomes.append(await _teaser(db, library, seen))
            outcomes.append(await _failed_draft(db, library, seen))
            await db.commit()
    except OperationalError as err:
        print(f"Cuts: the catalogue's tables are older than this code ({err.orig}). "
              "Run `alembic upgrade head` against ui.db first.")
        return
    finally:
        await engine.dispose()
    added = [f"{windows} shot windows with their stills"] if windows else []
    if files:
        added[0] += (" and the clip's motion file" if files == 1
                     else f" and the {files} clips' motion files")
    added += [said for how, said in outcomes if how == "added"]
    missing = [said for how, said in outcomes if how == "not added"]
    if added:
        print("Cuts: added " + "; ".join(added) + ".")
    if missing:
        print("Cuts: not added: " + "; ".join(missing) + ".")
    if not added and not missing:
        print("Cuts: everything it needs is already in this catalogue; nothing was added.")


# --- Looking (L3 design 8.9) ------------------------------------------------------------
# What a folder review by the local model needs on top of the Cuts part's
# shot windows: the catalogue's own sign that a clip's shots have been found,
# which is a ready `motion` file (looking/scope.py `clip_state`). The real
# pipeline writes that file from the playable copy before it writes the
# windows; the seed has no playable copies and nothing to decode them with,
# so its clips had windows and no file, and a review counted every one as
# "needs a playable copy first" — which on a review server ends with each
# clip judged from one still. With the file, Review with AI on a GELCO folder
# counts its clips' shots and looks at them one by one.
#
# It adds no look: those are made by reviewing a folder in demo mode, which
# is the thing to be looked at. Added once per catalogue and then remembered
# (local_models/demo.py `note_seeded`), like the colour and the cuts.

MOTION_PART = "looking_motion"


async def _motion_standing(db, video: Asset) -> str | None:
    """The status of a clip's motion file ("ready", "pending", "failed"),
    or None when it has none on record."""
    return await db.scalar(select(Derivative.status).where(
        Derivative.asset_id == video.id, Derivative.kind == "motion").limit(1))


def _motion_file(db, video: Asset) -> None:
    """A ready motion file for a clip that has none on record. The file is
    the real track's own shape for a clip nothing was sampled from
    (`media/motion.track`): its header and no samples, because nothing was
    measured."""
    from framefound.media import motion
    from framefound.processing.derivatives import derivative_relpath

    fps = float(video.fps or 30.0)
    track = motion.track(motion.Analysis(
        fps=fps, width=motion.ANALYSIS_WIDTH, height=motion.ANALYSIS_WIDTH * 9 // 16,
        frames=round((video.duration_s or 0.0) * fps)))
    track["source_fps"] = round(fps, 3)
    relative = derivative_relpath(video.id, "motion", "json")
    target = DATA / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(track, separators=(",", ":")))
    db.add(Derivative(asset_id=video.id, kind="motion", relative_path=relative,
                      media_format="json", status="ready", generated_at=datetime.now(UTC),
                      size_bytes=target.stat().st_size))


async def _motion_files(db, seen: frozenset[str]) -> tuple[str, str]:
    """A ready motion file for each clip that has shot windows and none
    ("added" | "there" | "not added", what to say).

    Every such clip in the catalogue, whichever library it is in: here only
    the seed gives a clip windows without the file. A clip that has a motion
    file of any standing (one a real pipeline made, or is making) is left as
    it is.

    Once per catalogue: a file that goes afterwards is not put back. A clip
    the seed gives windows to after this has run is not this sweep's: it
    gets its file with its windows (`_shots`)."""
    from framefound.db.models import ShotWindow, catalogued
    from framefound.local_models import demo

    if MOTION_PART in seen:
        return "there", ""
    clips = list((await db.scalars(
        select(Asset).where(Asset.media_type == "video", catalogued(),
                            exists().where(ShotWindow.asset_id == Asset.id))
        .order_by(Asset.relative_path))).all())
    if not clips:
        return "not added", "the clips' motion files (no clip in this catalogue has shot windows)"
    made = 0
    for video in clips:
        if await _motion_standing(db, video) is None:
            _motion_file(db, video)
            made += 1
    await demo.note_seeded(db, MOTION_PART)
    if not made:
        return "there", ""
    clips_said = "1 clip that has" if made == 1 else f"{made} clips that have"
    return "added", f"a motion file for each of the {clips_said} shots, so a review looks at them"


async def seed_looking() -> None:
    """What Review with AI needs of the seeded clips (after `seed_cuts`,
    which gives them their shot windows)."""
    from sqlalchemy.exc import OperationalError

    from framefound.local_models import demo

    engine = create_async_engine(DB_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            how, said = await _motion_files(db, await demo.seeded_parts(db))
            await db.commit()
    except OperationalError as err:
        print(f"Looking: the catalogue's tables are older than this code ({err.orig}). "
              "Run `alembic upgrade head` against ui.db first.")
        return
    finally:
        await engine.dispose()
    if how == "added":
        print(f"Looking: added {said}.")
    elif how == "not added":
        print(f"Looking: not added: {said}.")
    else:
        print("Looking: everything it needs is already in this catalogue; nothing was added.")


# --- Compare (L2 design 8.9) ----------------------------------------------------------
# What the Compare pages need on top of the AI models page and the Cuts part
# (which runs first and leaves three shot windows with their stills on each
# GELCO clip, and the imported timeline that used five of those shots): a few
# tags a person decided, two more installed models that read pictures, and
# one finished comparison of two models whose answers are written here,
# chosen so its figures are worth reading. Each part is added only when it is
# missing, so seeding a review catalogue again adds nothing twice and leaves
# alone whatever a reviewer made. The seeded rows are found again by what
# they are, never by a name a reviewer can change. The helpers that make the
# shot windows and the timeline (`_shots`, `_quick_30`) are kept here, where
# they were written; the Cuts part calls them.

SMALL_MODEL = "mlx-community/demo-small-4bit"
# Reads pictures but fits only the overnight budget, so a column of it in a
# comparison started by day shows "waiting until 21:30". L1's demo-large is
# the download that dropped, so it cannot be compared.
NIGHT_MODEL = "mlx-community/demo-medium-4bit"
# Kept in demo_data/compare/, out of the fixture list L1's tests walk. The
# running API's own demo hub reads them too (local_models/demo._pages).
COMPARE_PAGES = {
    SMALL_MODEL: ("compare/demo-small-4bit", "compare/demo-small"),
    NIGHT_MODEL: ("compare/demo-medium-4bit", "compare/demo-medium"),
}
QUICK_30 = "Pete Dye Quick 30"
SEEDED_COMPARISON = "GELCO drone shots: the Looker beside demo-small"
# The seeded comparison is the one drawn with this seed over GELCO by nobody
# (a comparison made in the browser always has the admin who made it).
SEEDED_SEED = 8
STILL_SIZE = (480, 270)
FOLDER_PALETTE = {"LedgeRock": "turf", "Pete Dye": "dusk", "Hudson National": "sky"}
# (move, direction, speed, smooth, energy, light, sky) for a clip's shots, by folder.
WINDOW_FACTS = {
    "LedgeRock": (
        ("lateral glide", "right", 0.12, 0.94, "low", "mid sun", "direct"),
        ("push-in", None, 0.08, 0.91, "low", "mid sun", "mixed"),
        ("orbit", "left", 0.15, 0.88, "medium", "mid sun", "direct"),
    ),
    "Pete Dye": (
        ("rise", None, 0.06, 0.96, "low", "golden hour", "direct"),
        ("orbit", "right", 0.10, 0.93, "low", "golden hour", "mixed"),
        ("pull-out", None, 0.04, 0.97, "low", "blue hour", "overcast"),
    ),
    "Hudson National": (
        ("top-down drift", "forward", 0.03, 0.98, "low", "high sun", "direct"),
        ("pan", "right", 0.20, 0.85, "medium", "high sun", "direct"),
        ("static", None, 0.0, 0.99, "low", "high sun", "mixed"),
    ),
}
CAPTIONS = {
    "LedgeRock": "A drone glides over green fairways with a line of trees behind.",
    "Pete Dye": "A drone shot of a golf course at dusk, the sky turning orange.",
    "Hudson National": "A high view of a golf course and a river under a clear sky.",
}


def _folder_of(relative_path: str) -> str:
    return relative_path.split("/", 1)[0]


def _still(palette: str, label: str, along: float) -> Image.Image:
    """A shot's still: the seed's scene, with a sun that moves along the clip so
    a shot's start, middle and end are visibly not the same picture."""
    img = scene(STILL_SIZE, palette, label)
    draw = ImageDraw.Draw(img)
    w, h = STILL_SIZE
    cx, cy, r = w * (0.1 + 0.8 * along), h * 0.22, h * 0.07
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 236, 170))
    return img


async def _shots(db, videos: list[Asset], *, with_motion: bool = False) -> tuple[int, int]:
    """Three windows and their stills for each clip that has none (how many
    windows, how many motion files).

    `with_motion` is for a catalogue Looking's part has already swept: a clip
    gets its ready motion file with its windows, as the real pipeline writes
    the two together, and a clip whose motion file is the pipeline's own and
    not ready (being made, or failed) gets neither. Windows beside a file
    that is not ready is the one state a folder review cannot finish."""
    from framefound.db.models import ShotWindow
    from framefound.processing.shots import still_relpath, window_moments

    made = files = 0
    for n, video in enumerate(videos):
        if await db.scalar(select(ShotWindow.id).where(ShotWindow.asset_id == video.id).limit(1)):
            continue
        if with_motion:
            standing = await _motion_standing(db, video)
            if standing not in (None, "ready"):
                continue
            if standing is None:
                _motion_file(db, video)
                files += 1
        folder = _folder_of(video.relative_path)
        facts = WINDOW_FACTS.get(folder, WINDOW_FACTS["LedgeRock"])
        length = int((video.duration_s or 30.0) * 1000)
        for idx in range(3):
            start, end = length * idx // 3, length * (idx + 1) // 3
            move, direction, speed, smooth, energy, light, sky = facts[(n + idx) % len(facts)]
            db.add(ShotWindow(asset_id=video.id, idx=idx, start_ms=start, end_ms=end, move=move,
                              direction=direction, speed=speed, smooth=smooth, energy=energy,
                              energy_score={"low": 0.15, "medium": 0.5}[energy], light=light,
                              look={"sky": sky}, subjects={"golf course": 0.81, "trees": 0.64},
                              described_at=datetime.now(UTC)))
            for moment in dict.fromkeys(window_moments(start, end)):
                target = DATA / still_relpath(video.id, moment)
                if target.is_file():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                label = f"{folder} · {video.filename} · {moment // 60000}:{moment // 1000 % 60:02d}"
                _still(FOLDER_PALETTE.get(folder, "sky"), label, moment / max(1, length)).save(
                    target, "JPEG", quality=82)
            made += 1
    await db.flush()
    return made, files


async def _rulings(db, videos: list[Asset]) -> int:
    """`exterior` said yes to on four clips and `sunset` said no to on three, as a
    person would on the Tags page. How many rulings were added."""
    made = 0
    for name, source, wanted in (
        ("exterior", "confirmed", [v for v in videos if _folder_of(v.relative_path) != "Hudson National"]),
        ("sunset", "rejected", [v for v in videos if _folder_of(v.relative_path) != "Pete Dye"]),
    ):
        tag = await db.scalar(select(Tag).where(Tag.slug == name))
        if tag is None:
            tag = Tag(name=name, slug=name)
            db.add(tag)
            await db.flush()
        for video in wanted[:4 if source == "confirmed" else 3]:
            ruled = await db.scalar(select(AssetTag.id).where(AssetTag.asset_id == video.id,
                                                              AssetTag.tag_id == tag.id))
            if ruled is None:
                db.add(AssetTag(asset_id=video.id, tag_id=tag.id, source=source, confidence=0.7,
                                origin="clip"))
                made += 1
    await db.flush()
    return made


async def _quick_30(db, library: Library, videos: list[Asset]) -> str:
    """The finished cut, imported from Premiere: five pieces from the middle of
    five Pete Dye shots, so those count as used. "added", "there", or "not
    added" when the folder has no analysed shot to take a piece from.

    It is there already when an imported cut of GELCO has exactly these
    pieces, whatever it is called now: a cut renamed on the Cuts page must
    not make the next seed import a second copy."""
    from framefound.db.models import Cut, CutPiece, ShotWindow

    ids = [v.id for v in videos if _folder_of(v.relative_path) == "Pete Dye"]
    names = {v.id: v.filename for v in videos}
    windows = (await db.execute(select(ShotWindow).where(ShotWindow.asset_id.in_(ids))
                                .order_by(ShotWindow.asset_id, ShotWindow.start_ms))).scalars().all()
    windows = sorted(windows, key=lambda w: (names[w.asset_id], w.start_ms))[:5]
    if not windows:
        return "not added"
    pieces = []
    for window in windows:
        middle, half = (window.start_ms + window.end_ms) // 2, min(2000, (window.end_ms - window.start_ms) // 4)
        pieces.append((window, middle - half, middle + half))
    wanted = {(window.asset_id, in_ms, out_ms) for window, in_ms, out_ms in pieces}
    imported: dict = {}
    for cut_id, asset_id, in_ms, out_ms in (await db.execute(
            select(CutPiece.cut_id, CutPiece.asset_id, CutPiece.in_ms, CutPiece.out_ms)
            .join(Cut, Cut.id == CutPiece.cut_id)
            .where(Cut.source == "import", Cut.library_id == library.id))).all():
        imported.setdefault(cut_id, set()).add((asset_id, in_ms, out_ms))
    if wanted in imported.values():
        return "there"
    cut = Cut(title=QUICK_30, client="GELCO", library_id=library.id, source="import", duration_s=0.0,
              piece_count=len(windows), created_at=datetime.now(UTC) - timedelta(days=12))
    db.add(cut)
    await db.flush()
    for position, (window, in_ms, out_ms) in enumerate(pieces):
        db.add(CutPiece(cut_id=cut.id, position=position, asset_id=window.asset_id,
                        filename=names[window.asset_id], in_ms=in_ms, out_ms=out_ms,
                        shot={"move": window.move, "light": window.light}))
        cut.duration_s += (out_ms - in_ms) / 1000
    await db.flush()
    return "added"


async def _install(factory, settings, repo_id: str) -> tuple[str, str]:
    """One of Compare's demo models, downloaded by the real handler from the
    demo's stand-in Hugging Face: ("there" | "added" | "not added", what to say).

    Only an installed row counts as there. A row an earlier run left behind
    unfinished — that run was stopped, the lane was paused, or the running
    API's demo lane took the job and, not knowing these pages, failed it — is
    downloaded again here as Resume would, so seeding again repairs it."""
    from dataclasses import replace

    from framefound.db.models import LocalAiJob, LocalModel
    from framefound.local_models import demo, fake, jobs, preflight, registry
    from framefound.local_models.settings_store import load_config

    # The lane is run below, on a catalogue that may hold a reviewer's queued
    # work: a job of a kind this process did not know would be ended as failed.
    demo.know_every_kind()
    made = demo.deps(settings, slow=False)
    hub = fake.FakeHub.from_fixtures(chunk_bytes=demo.SMALL_FILE_BYTES)
    for names in COMPARE_PAGES.values():
        for name in names:
            page = fake.fixture(name)
            hub.payloads[page["id"]] = page
    hub.small_files(demo.SMALL_FILE_BYTES)
    async with factory() as db:
        model = await db.scalar(select(LocalModel).where(LocalModel.repo_id == repo_id).limit(1))
        if model is not None and model.state == registry.INSTALLED:
            return "there", ""
        job = None
        if model is None:
            found = preflight.preflight(hub, repo_id, config=await load_config(db), kinds=made.kinds,
                                        installed={fake.DemoKind.name: demo.SEED_VERSIONS[-1]},
                                        free_bytes=None, min_free_bytes=0)
            try:
                model = await registry.add_model(db, found, None)
            except registry.Refusal as err:
                # The catalogue's own rules (a reviewer may have blocked a
                # publisher on the AI models page) are not overruled here.
                return "not added", f"{repo_id} (the check refused it: {err})"
        else:
            live = (await db.execute(select(LocalAiJob).where(
                LocalAiJob.model_id == model.id, LocalAiJob.kind == "download",
                LocalAiJob.state.in_(jobs.ACTIVE)))).scalars().all()
            for found_job in live:
                await jobs.reap(db, found_job)  # a worker that died ends as the page would end it
            live = [found_job for found_job in live if found_job.state in jobs.ACTIVE]
            if any(found_job.state == "running" for found_job in live):
                return "not added", (f"{repo_id} (its download is running in the API now; "
                                     "seed again once it has finished)")
            job = live[0] if live else None
            if job is None:
                model.state, model.error = registry.DOWNLOADING, None
        if job is None:
            variant = f"/{model.variant}" if model.variant else ""
            job = await jobs.create(db, "download", subject=f"{model.repo_id}@{model.revision[:8]}{variant}",
                                    model_id=model.id)
        job_id, model_id = job.id, model.id
        await db.commit()
    # The lane runs the most urgent job first: something a reviewer queued may
    # go before ours. Stop as soon as ours has run.
    for _ in range(10):
        async with factory() as db:
            if await db.scalar(select(LocalAiJob.state).where(LocalAiJob.id == job_id)) != "queued":
                break
        if not await jobs.run_next(factory, replace(made, hub_for=hub.with_token)):
            break
    async with factory() as db:
        ended = await db.get(LocalAiJob, job_id)
        model = await db.get(LocalModel, model_id)
    if model is not None and model.state == registry.INSTALLED:
        return "added", repo_id
    if ended is None:
        return "not added", f"{repo_id} (its download was removed; seed again to start it again)"
    if ended.state in jobs.ACTIVE:
        return "not added", (f"{repo_id} (its download is still {ended.state}; "
                             "seed again once it has finished)")
    return "not added", (f"{repo_id} (its download ended: {ended.message or ended.state}; "
                         "seed again to try again)")


def _answer_a(item, used_n: int, unused_n: int) -> dict:
    """The Looker: sees the drone and the outside on every clip, scores the used
    shots high but not always above the rest, and is unsure about the sky."""
    folder = _folder_of(item.relative_path)
    score = (86, 91, 79, 88, 83)[used_n % 5] if item.used else (62, 74, 55, 81, 68, 47, 72, 90, 58, 66)[unused_n % 10]
    matches = [{"tag": "drone", "confidence": 0.93}]
    if item.start_ms:  # all but a clip's first shot, which opens on the sky
        matches.append({"tag": "exterior", "confidence": 0.78})
    if folder == "Pete Dye":
        matches.append({"tag": "sunset", "confidence": 0.55})
    if folder == "Hudson National" and not item.start_ms:
        matches.append({"tag": "sunset", "confidence": 0.48})
    new = [{"tag": "golf course", "confidence": 0.88}]
    if folder == "LedgeRock" and item.start_ms and item.start_ms > item.end_ms // 2:
        new.append({"tag": "bunker", "confidence": 0.66})
    reason = ("Steady move and a clean horizon; the course is in focus." if score >= 80
              else "Usable, but the framing drifts off the green." if score >= 60
              else "Soft, and the light is flat.")
    return {"caption": CAPTIONS.get(folder, CAPTIONS["LedgeRock"]), "score": score, "reason": reason,
            "matches": matches, "new_tags": new}


def _answer_b(item, used_n: int, unused_n: int) -> dict:
    """demo-small: scores nearly everything alike, mistakes a bright sky for a
    sunset, and sees the outside only now and then."""
    folder = _folder_of(item.relative_path)
    score = (70, 75, 70, 65, 70)[used_n % 5] if item.used else (70, 70, 75, 70, 65, 70, 70, 75, 70, 70)[unused_n % 10]
    matches = [{"tag": "drone", "confidence": 0.81}]
    if folder == "LedgeRock":
        matches.append({"tag": "exterior", "confidence": 0.58})
    if folder != "Pete Dye":
        matches.append({"tag": "sunset", "confidence": 0.71})
    new = [{"tag": "grass", "confidence": 0.62}] if folder == "LedgeRock" else []
    return {"caption": "Aerial view of a green landscape.", "score": score,
            "reason": "Clear picture." if score >= 70 else "A little dark.",
            "matches": matches, "new_tags": new}


async def _comparison(factory, settings, library: Library) -> tuple[str, str]:
    """The finished comparison: the Looker and demo-small on the fifteen GELCO
    shots, frozen as Start freezes them, with answers chosen here.
    ("there" | "added" | "not added", what to say).

    It is there already when a comparison drawn with SEEDED_SEED over GELCO by
    nobody exists, whatever it is called now: renaming it on the page must not
    make the next seed build a second one."""
    from dataclasses import asdict

    from framefound.ai.tagging import slugify
    from framefound.db.models import Comparison, ComparisonAnswer, LocalAiJob, LocalModel
    from framefound.local_models import comparisons, compare_job, demo, registry, sampling
    from framefound.looking import prompt

    async with factory() as db:
        seeded = (await db.execute(select(Comparison.source).where(
            Comparison.created_by.is_(None), Comparison.seed == SEEDED_SEED,
            Comparison.source_kind == "folder"))).scalars().all()
        if any((source or {}).get("library_id") == str(library.id) for source in seeded):
            return "there", ""
        what = f'the comparison "{SEEDED_COMPARISON}"'
        models = []
        for repo_id in (demo.LOOKER_MODEL, SMALL_MODEL):
            model = await db.scalar(select(LocalModel).where(LocalModel.repo_id == repo_id,
                                                             LocalModel.state == registry.INSTALLED))
            if model is None:
                return "not added", f"{what} ({repo_id} is not installed)"
            models.append(model)
        spec = sampling.SampleSpec(kind="folder", library_id=library.id, folder="", what="shots",
                                   size=15, stills=3, seed=SEEDED_SEED)
        try:
            drawn = await sampling.draw(db, spec, settings)
        except sampling.SampleError as err:
            return "not added", f"{what} ({err})"
        comparison = await comparisons.create(db, spec, drawn, name=SEEDED_COMPARISON, user_id=None)
        frozen = await asyncio.to_thread(sampling.freeze, comparison.id, drawn.items, settings)
        try:
            await comparisons.set_pictures(db, comparison, frozen)
            entries = await comparisons.add_models(db, comparison, models, user_id=None)
            items = await comparisons.items_of(db, comparison.id)
            slugs = {slugify(name) for name, _n in prompt.most_used(comparison.vocabulary)}
            for entry, model, answer, pace, load, memory, ran_from in (
                # The memory is what the demo's server reports for each model
                # (its estimate x local_models/demo.MEASURED_SHARE), so a new
                # comparison of the same model shows the same figure.
                (entries[0], models[0], _answer_a, 5.4, 14.2, 5.2, "this_machine"),
                (entries[1], models[1], _answer_b, 2.6, 6.4, 3.1, "library"),
            ):
                started = datetime.now(UTC)
                done = failed = used_n = unused_n = 0
                for item in items:
                    seconds = round(pace + (item.position % 4) * 0.35, 2)
                    if answer is _answer_b and item.used is False and unused_n == 4:
                        # One answer in prose, twice: a finding, kept with what it wrote.
                        db.add(ComparisonAnswer(entry_id=entry.id, item_id=item.id, ok=False,
                                                problem="bad_answer", attempts=2, tags=[],
                                                message="Its answer was not the form asked for, twice.",
                                                raw="The pictures show a golf course from the air. It is a nice shot.",
                                                created_at=datetime.now(UTC)))
                        failed += 1
                        unused_n += 1
                        continue
                    look = prompt.LookAnswer.model_validate(answer(item, used_n, unused_n))
                    keep = compare_job.kept(look, slugs)
                    second_try = answer is _answer_b and item.position == 2
                    db.add(ComparisonAnswer(entry_id=entry.id, item_id=item.id, ok=True,
                                            tags=[asdict(tag) for tag in keep.tags], caption=keep.caption,
                                            score=keep.score, reason=keep.reason, seconds=seconds,
                                            attempts=2 if second_try else 1, memory_gb=memory,
                                            raw=look.model_dump_json() if second_try else None,
                                            created_at=datetime.now(UTC)))
                    done += 1
                    if item.used:
                        used_n += 1
                    else:
                        unused_n += 1
                entry.state, entry.message, entry.done, entry.failed = "done", "", done, failed
                entry.runtime, entry.runtime_version, entry.ran_from = "demo", demo.SEED_VERSIONS[-1], ran_from
                entry.memory_gb, entry.memory_source, entry.load_seconds = memory, "runtime", load
                entry.started_at, entry.finished_at = started, datetime.now(UTC)
                job = await db.get(LocalAiJob, entry.job_id)
                job.state, job.phase, job.done, job.total = "done", "looking", len(items), len(items)
                job.result = {"answered": done, "failed": failed}
                job.started_at, job.finished_at = started, entry.finished_at
                job.beat_at = entry.finished_at
                job.model, job.model_revision = model.repo_id, model.revision
                job.runtime, job.runtime_version = "demo", demo.SEED_VERSIONS[-1]
                job.prompt_version, job.memory_gb = comparison.prompt_version, memory
            await db.commit()
        except BaseException:
            await db.rollback()
            sampling.drop_pictures(comparison.id, settings)
            raise
    return "added", f"{what} ({len(items)} shots, two models, finished)"


async def seed_compare() -> None:
    """The Compare pages in demo mode (after `seed_local_ai`, whose settings it uses)."""
    from sqlalchemy.exc import OperationalError

    from framefound.config import get_settings

    settings = get_settings()
    engine = create_async_engine(DB_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            library = await db.scalar(select(Library).where(Library.name == "GELCO"))
            if library is None:
                print("Compare: no GELCO library in this catalogue, so nothing was added for it.")
                return
            videos = list((await db.execute(
                select(Asset).where(Asset.library_id == library.id, Asset.media_type == "video")
                .order_by(Asset.relative_path))).scalars().all())
            # The shot windows and the imported timeline are the Cuts part's,
            # made before this runs; a timeline a reviewer deleted is not put
            # back for the comparison's sake.
            rulings = await _rulings(db, videos)
            await db.commit()
        outcomes = [await _install(factory, settings, repo_id) for repo_id in COMPARE_PAGES]
        outcomes.append(await _comparison(factory, settings, library))
    except OperationalError as err:
        print(f"Compare: the catalogue's tables are older than this code ({err.orig}). "
              "Run `alembic upgrade head` against ui.db first.")
        return
    finally:
        await engine.dispose()
    added = [f"{rulings} decided tags"] if rulings else []
    added += [said for how, said in outcomes if how == "added"]
    missing = [said for how, said in outcomes if how == "not added"]
    if added:
        print("Compare: added " + "; ".join(added) + ".")
    if missing:
        # Said apart from what was added, so a part that could not be made is
        # never read as made.
        print("Compare: not added: " + "; ".join(missing) + ".")
    if not added and not missing:
        print("Compare: everything it needs is already in this catalogue; nothing was added.")


async def seed() -> None:
    if not LOCAL_AI_ONLY:
        await main()
    await correct_rooms()
    await seed_local_ai()
    await seed_colour()
    await seed_cuts()
    await seed_looking()
    if COMPARE:
        await seed_compare()


asyncio.run(seed())
