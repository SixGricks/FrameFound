"""Collecting before/after pairs from what shipped (training/pairs.py)."""

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

from framefound.media import develop as develop_lib
from framefound.training import pairs


def _scene(seed: int) -> Image.Image:
    rng = random.Random(seed)
    image = Image.new("RGB", (480, 320), (rng.randint(60, 160),) * 3)
    draw = ImageDraw.Draw(image)
    for _ in range(8):
        x0, y0 = rng.randint(0, 400), rng.randint(0, 260)
        colour = tuple(rng.randint(20, 235) for _ in range(3))
        draw.rectangle((x0, y0, x0 + rng.randint(30, 160), y0 + rng.randint(20, 100)), fill=colour)
    return image


def test_shoot_names_match_across_drive_and_nas() -> None:
    nas = "11-21 - 901 Smyrna Rd, Kinzers PA"
    assert pairs.shoot_similarity("11-21 - 901 Smyrna Rd Kinzers", nas) >= 0.9
    assert pairs.shoot_similarity("08-25 - 111 Water St", "08-25 - 113 Water St") == 0.0, (
        "two properties a house number apart are two shoots"
    )
    assert pairs.shoot_similarity("10-01 338 Churchtown Rd", "09-01 338 Churchtown Rd") == 0.0
    assert (
        pairs.shoot_similarity(
            "2026 08 13 - 678 Susquehanna Trail", "08-13 - 678 Susquehanna Trail"
        )
        > 0.9
    )


def test_shoot_folders_and_what_counts_as_a_final() -> None:
    assert pairs.shoot_of("2026/11-21 - Davis/Fotello Batch 1/01.jpg") == (
        "2026/11-21 - Davis",
        "Fotello Batch 1/01.jpg",
    )
    assert pairs.shoot_of("Chambersburg/drone/a.jpg")[0] == "Chambersburg"
    for folder in ("MLS", "MLS - Fotello Edited", "Edited Photos", "Fotello_Edited"):
        assert pairs.FINALS_FOLDER.search(folder), folder
    for folder in ("Fotello Batch 1", "Originals", "Sunrise"):
        assert not pairs.FINALS_FOLDER.search(folder), folder
    assert pairs.SKIP_FOLDER.search("Reject") and pairs.SKIP_FOLDER.search("Contact Sheets")


def _catalogue(tmp: Path) -> list[tuple[str, str, str | None]]:
    """Davis-style NAS shoot: originals in Fotello Batch 1, finals in
    MLS - Fotello Edited under the same names, plus a reject."""
    rows = []
    for n in range(4):
        name = f"0{n + 1}-room-{n}-130-davis-rd-auction.jpg"
        for folder, image in (
            ("Fotello Batch 1", _scene(n)),
            ("MLS - Fotello Edited", develop_lib.apply_recipe(_scene(n), {"exposure": 0.5})),
        ):
            thumb = Path("derivatives") / folder.replace(" ", "_") / f"{n}.webp"
            (tmp / thumb).parent.mkdir(parents=True, exist_ok=True)
            image.save(tmp / thumb, "WEBP")
            rows.append(
                (f"{folder[:3]}-{n}", f"2026/11-21 - 130 Davis Rd/{folder}/{name}", str(thumb))
            )
    rows.append(("rej-1", "2026/11-21 - 130 Davis Rd/Reject/r01.jpg", str(thumb)))
    return rows


def test_nas_finals_pair_by_name_and_a_second_run_adds_nothing(tmp_path: Path) -> None:
    rows = _catalogue(tmp_path)
    summary = pairs.collect(rows, tmp_path)
    assert summary["new_pairs"] == 4
    lines = (tmp_path / pairs.MANIFEST).read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert {r["match"] for r in records} == {"name"}
    assert all(r["original_asset_id"].startswith("Fot") for r in records), "batch = originals"
    assert all(r["final"].startswith("asset:MLS") for r in records)
    assert pairs.collect(rows, tmp_path)["new_pairs"] == 0, "append-only, never duplicated"


class FakeDrive:
    """One shared-drive root holding one shoot with an MLS folder of
    SEO-renamed, re-toned finals."""

    def __init__(self, finals: dict[str, Image.Image]):
        self.finals = finals
        self.downloads = 0

    def list_folders(self, folder_id: str) -> list[dict[str, str]]:
        if folder_id == "root":
            return [{"id": "shoot", "name": "09-15 5096 Old Philadelphia Pike Kinzers"}]
        if folder_id == "shoot":
            return [{"id": "mls", "name": "MLS"}, {"id": "raw", "name": "Originals"}]
        return []

    def list_image_files(self, folder_id: str) -> list[dict[str, object]]:
        return [{"id": name, "name": name, "size": 0} for name in self.finals]

    def download(self, file_id: str, destination: Path) -> None:
        self.downloads += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.finals[file_id].save(destination, "JPEG", quality=92)


def test_drive_finals_are_fetched_matched_to_the_nas_shoot_and_paired_by_content(
    tmp_path: Path,
) -> None:
    rows = []
    finals = {}
    for n in range(4):
        thumb = Path("derivatives") / f"k{n}.webp"
        (tmp_path / thumb).parent.mkdir(parents=True, exist_ok=True)
        _scene(10 + n).save(tmp_path / thumb, "WEBP")
        rows.append(
            (
                f"orig-{n}",
                f"2026/09-15 5096 Old Philadelphia Pike, Kinzers PA/IMG_{n}.JPG",
                str(thumb),
            )
        )
        finals[f"0{n + 1}-front-{n}-5096-old-philadelphia-pike.jpg"] = develop_lib.apply_recipe(
            _scene(10 + n), {"exposure": 0.4, "shadows": 0.3, "vibrance": 0.2}
        )
    drive = FakeDrive(finals)
    summary = pairs.collect(rows, tmp_path, drive, ["root"])
    assert drive.downloads == 4 and summary["new_pairs"] == 4
    records = [json.loads(line) for line in (tmp_path / pairs.MANIFEST).read_text().splitlines()]
    truth = {f"orig-{n}": f"0{n + 1}-front-{n}-5096-old-philadelphia-pike.jpg" for n in range(4)}
    for record in records:
        assert record["match"] == "content"
        assert truth[record["original_asset_id"]] == record["final_name"], record
        assert record["final"].startswith("file:training/finals/")
