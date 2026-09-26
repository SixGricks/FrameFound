"""Collect every finished photograph that shipped, paired with its original.

The Fotello bake-off showed what closes FrameFound's editing gap: a model
trained on this operator's own before/after pairs. Two to three shoots a
week go through Fotello, so pairs arrive at ~750 a month — as long as
something collects them. This does, nightly and incrementally:

- **Finals** are the photos in a shoot's "MLS", "Edited" or "Fotello
  Edited" folder — on the NAS (already catalogued) or on Google Drive,
  where most of them live. Drive finals are downloaded to
  data/training/finals/ (read-only access; nothing on Drive changes).
- **Originals** are the catalogued photos of the same shoot — the shoot
  folder itself and its "Fotello Batch" subfolders, never rejects or
  contact sheets.
- **Shoots** are matched across Drive and NAS by their names:
  "11-21 - 901 Smyrna Rd Kinzers" is "11-21 - 901 Smyrna Rd, Kinzers PA".
- **Pairs** are matched by file name when it survived, else by picture
  content (media/compare.py); each records its framing (the final is
  often cropped and lens-corrected) so training can register it
  precisely.

The result is data/training/pairs.jsonl, one pair per line, appended to —
never rewritten — so a collected pair is never lost to a later bad run.
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from framefound.media import compare

log = structlog.get_logger()

FINALS_FOLDER = re.compile(r"^\s*(mls\b|edited|final|.*fotello.*edit)", re.IGNORECASE)
SKIP_FOLDER = re.compile(r"reject|contact sheet|nadir|\braw\b|video|social", re.IGNORECASE)
_DATE_PREFIX = re.compile(
    r"^\s*(?:(?P<y>\d{4})[\s_-]+)?(?P<m>\d{1,2})[\s_-]+(?P<d>\d{1,2})\b[\s_-]*",
)
_STATES = {"pa", "de", "nj", "md", "ny", "va", "wv", "ct", "oh"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
MIN_SHOOT_SIMILARITY = 0.5
MANIFEST = Path("training") / "pairs.jsonl"
FINALS_DIR = Path("training") / "finals"


@dataclass
class TrainingPair:
    shoot: str
    original_asset_id: str
    original_path: str  # library-relative
    final: str  # "asset:<uuid>" (catalogued) or "file:<data-relative path>"
    final_name: str
    match: str
    match_score: float
    framing_box: tuple[float, float, float, float]
    framing_scale: float
    framing_score: float
    collected_at: str


def shoot_date(name: str) -> str | None:
    """mm-dd from a shoot folder's leading date, if it has one."""
    found = _DATE_PREFIX.match(name)
    if not found:
        return None
    return f"{int(found['m']):02d}-{int(found['d']):02d}"


def shoot_tokens(name: str) -> frozenset[str]:
    """The words that identify a shoot: no date, no state, no punctuation."""
    stripped = _DATE_PREFIX.sub("", name.lower())
    return frozenset(w for w in re.split(r"[^a-z0-9]+", stripped) if w and w not in _STATES)


def shoot_similarity(a: str, b: str) -> float:
    """0..1 that two folder names are the same shoot. Dates must agree when
    both have one, and so must the house number — 111 and 113 Water St
    are two properties with nearly the same name."""
    date_a, date_b = shoot_date(a), shoot_date(b)
    if date_a and date_b and date_a != date_b:
        return 0.0
    tokens_a, tokens_b = shoot_tokens(a), shoot_tokens(b)
    if not tokens_a or not tokens_b:
        return 0.0
    numbers_a = {t for t in tokens_a if t.isdigit()}
    numbers_b = {t for t in tokens_b if t.isdigit()}
    if numbers_a and numbers_b and not numbers_a & numbers_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def shoot_of(relative_path: str) -> tuple[str, str]:
    """(shoot folder, the rest) for a library-relative path: the first
    folder, or the first two when the first is a year ("2026/…")."""
    parts = relative_path.split("/")
    depth = 2 if re.fullmatch(r"\d{4}", parts[0]) and len(parts) > 2 else 1
    return "/".join(parts[:depth]), "/".join(parts[depth:])


@dataclass
class Photo:
    ref: str  # "asset:<uuid>" or "file:<data-relative>"
    name: str
    path: Path  # something decodable: a thumbnail or the downloaded file
    asset_id: str = ""
    relative_path: str = ""


@dataclass
class Shoot:
    folder: str
    originals: list[Photo]
    finals: list[Photo]


def group_catalogue(rows: list[tuple[str, str, str | None]], data_dir: Path) -> dict[str, Shoot]:
    """Catalogued images -> shoots with their originals and NAS finals.
    `rows` are (asset_id, relative_path, thumbnail data-relative path)."""
    shoots: dict[str, Shoot] = {}
    for asset_id, relative_path, thumbnail in rows:
        if not thumbnail:
            continue
        folder, rest = shoot_of(relative_path)
        subfolders = rest.split("/")[:-1]
        if any(SKIP_FOLDER.search(s) for s in subfolders):
            continue
        shoot = shoots.setdefault(folder, Shoot(folder, [], []))
        photo = Photo(
            ref=f"asset:{asset_id}",
            name=relative_path.rsplit("/", 1)[-1],
            path=data_dir / thumbnail,
            asset_id=asset_id,
            relative_path=relative_path,
        )
        if any(FINALS_FOLDER.search(s) for s in subfolders):
            shoot.finals.append(photo)
        else:
            shoot.originals.append(photo)
    return shoots


def best_shoot(drive_name: str, shoots: dict[str, Shoot]) -> str | None:
    scored = [
        (shoot_similarity(drive_name, folder.rsplit("/", 1)[-1]), folder) for folder in shoots
    ]
    scored = [s for s in scored if s[0] >= MIN_SHOOT_SIMILARITY]
    return max(scored)[1] if scored else None


def _small(path: Path) -> Any:
    from PIL import Image, ImageOps

    with Image.open(path) as img:
        img.draft("RGB", (512, 512))
        upright = ImageOps.exif_transpose(img) or img
        small = upright.convert("RGB")
    small.thumbnail((512, 512))
    return small


def pair_shoot(shoot: Shoot, known: set[tuple[str, str]]) -> list[TrainingPair]:
    """Pairs for one shoot, skipping any already in the manifest."""
    if not shoot.originals or not shoot.finals:
        return []
    originals: dict[str, Any] = {}
    finals: dict[str, Any] = {}
    by_name_o: dict[str, Photo] = {}
    by_name_f: dict[str, Photo] = {}
    orientation: dict[str, bool] = {}
    for group, target, index in (
        (shoot.originals, originals, by_name_o),
        (shoot.finals, finals, by_name_f),
    ):
        for number, photo in enumerate(group):
            try:
                small = _small(photo.path)
            except OSError:
                continue
            # "n/name": unique, and compare.name_key drops everything up to
            # the last slash, so file names still match by name.
            key = f"{number}/{photo.name}"
            target[key] = (compare.descriptor(small), small)
            index[key] = photo
            orientation[key] = small.height > small.width
    pairs = compare.match(
        {k: v[0] for k, v in originals.items()},
        {k: v[0] for k, v in finals.items()},
        orientation,
    )
    collected: list[TrainingPair] = []
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for pair in pairs:
        original, final = by_name_o[pair.original], by_name_f[pair.reference]
        if (original.asset_id, final.ref) in known:
            continue
        framing = compare.align(originals[pair.original][1], finals[pair.reference][1])
        collected.append(
            TrainingPair(
                shoot=shoot.folder,
                original_asset_id=original.asset_id,
                original_path=original.relative_path,
                final=final.ref,
                final_name=final.name,
                match=pair.method,
                match_score=pair.score,
                framing_box=framing.box,
                framing_scale=framing.scale,
                framing_score=framing.score,
                collected_at=now,
            )
        )
    return collected


def load_known(data_dir: Path) -> set[tuple[str, str]]:
    path = data_dir / MANIFEST
    known: set[tuple[str, str]] = set()
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
                known.add((record["original_asset_id"], record["final"]))
            except (ValueError, KeyError):
                continue
    return known


def append(data_dir: Path, pairs: list[TrainingPair]) -> None:
    if not pairs:
        return
    path = data_dir / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        for pair in pairs:
            out.write(json.dumps(asdict(pair)) + "\n")


def fetch_drive_finals(
    client: Any, folder_ids: list[str], shoots: dict[str, Shoot], data_dir: Path
) -> dict[str, int]:
    """Download the finals of every Drive shoot that matches a catalogued
    shoot, adding them to that shoot. Already-downloaded files (same size)
    are not fetched again. Returns per-shoot download counts."""
    fetched: dict[str, int] = {}
    for root in folder_ids:
        for drive_shoot in client.list_folders(root):
            target = best_shoot(drive_shoot["name"], shoots)
            if target is None:
                continue
            for sub in client.list_folders(drive_shoot["id"]):
                if not FINALS_FOLDER.search(sub["name"]):
                    continue
                safe_shoot = re.sub(r"[^A-Za-z0-9 ._-]", "_", drive_shoot["name"]).strip()
                for item in client.list_image_files(sub["id"]):
                    name = str(item["name"])
                    if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                        continue
                    relative = FINALS_DIR / safe_shoot / re.sub(r"[/\\]", "_", name)
                    destination = data_dir / relative
                    size = int(item.get("size") or 0)
                    if not (destination.is_file() and destination.stat().st_size == size):
                        client.download(item["id"], destination)
                        fetched[target] = fetched.get(target, 0) + 1
                    shoots[target].finals.append(
                        Photo(ref=f"file:{relative.as_posix()}", name=name, path=destination)
                    )
    return fetched


def collect(
    rows: list[tuple[str, str, str | None]],
    data_dir: Path,
    drive_client: Any = None,
    drive_folder_ids: list[str] | None = None,
) -> dict[str, Any]:
    """One collection pass. Returns a summary for the log."""
    shoots = group_catalogue(rows, data_dir)
    fetched: dict[str, int] = {}
    if drive_client is not None and drive_folder_ids:
        fetched = fetch_drive_finals(drive_client, drive_folder_ids, shoots, data_dir)
    known = load_known(data_dir)
    added: dict[str, int] = {}
    for shoot in shoots.values():
        new = pair_shoot(shoot, known)
        if new:
            append(data_dir, new)
            added[shoot.folder] = len(new)
            known.update((p.original_asset_id, p.final) for p in new)
    summary = {
        "shoots_with_finals": sum(1 for s in shoots.values() if s.finals),
        "downloaded": sum(fetched.values()),
        "new_pairs": sum(added.values()),
        "total_pairs": len(known),
        "by_shoot": added,
    }
    log.info("training.pairs_collected", **{k: v for k, v in summary.items() if k != "by_shoot"})
    return summary
