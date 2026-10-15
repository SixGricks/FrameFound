"""Seed a local SQLite catalogue with generated media, to look at the UI.

    python infrastructure/scripts/seed_dev.py <root>

`<root>` is the directory holding `ui.db` (run `alembic upgrade head` against
it first), with `data/` and `media/` created here — see CONTRIBUTING.md
"Looking at the UI with a seeded catalogue".

Three libraries that look like the real ones (Intel shoots, GELCO courses,
family storage), real JPEG originals under the media root so the editor can
open them, thumbnails/previews/frames under the data dir, faces, tags, two
listings, path profiles. Everything is synthetic; nothing leaves this machine.
"""

import asyncio
import math
import os
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw
from sqlalchemy import select
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
        rooms = ["exterior", "kitchen", "living-room", "bedroom", "bathroom", "garage", "", "exterior"]
        for position, asset in enumerate(davis):
            db.add(ListingItem(listing_id=listing.id, asset_id=asset.id, position=position,
                               room=rooms[position % len(rooms)], room_source="suggested",
                               room_score=0.8, caption=f"Davis Rd — {rooms[position % len(rooms)] or 'view'}"))
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


asyncio.run(main())
