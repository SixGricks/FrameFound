"""File names, the photo index, and contact sheets — the pure parts of the
listing delivery package."""

import csv
import io

import pytest
from PIL import Image

from framefound.media import contact_sheet, photo_index
from framefound.media.photo_index import IndexRow


@pytest.mark.parametrize(
    ("name", "suffix"),
    [
        ("00-00 5096 Old Philadelphia Pike Kinzers", "5096-old-philadelphia-pike-kinzers"),
        ("09-24 130 Davis Rd", "130-davis-rd"),
        ("2026-09-24 Old Farm Auction", "old-farm-auction"),
        # An address range is not a date: no year, not the placeholder, and
        # no house number after it.
        ("12-14 Main St", "12-14-main-st"),
        ("130 Davis Rd.", "130-davis-rd"),
        ("  ", ""),
    ],
)
def test_default_suffix_drops_the_shoot_date_but_not_an_address(name: str, suffix: str) -> None:
    assert photo_index.default_suffix(name) == suffix


def test_seo_names_pad_to_the_listing_size() -> None:
    assert (
        photo_index.export_filename(3, 40, slug="aerial-horse-barn", room="", suffix="x-auction")
        == "03-aerial-horse-barn-x-auction.jpg"
    )
    # Past 99 photographs two digits stop sorting: 100 would land after 10.
    assert photo_index.export_filename(3, 120, slug="", room="kitchen", suffix="").startswith(
        "003-kitchen"
    )


def test_seo_names_fall_back_to_the_room_then_photo() -> None:
    kw = {"slug": "", "suffix": "12-maple-st"}
    assert photo_index.export_filename(1, 2, room="front_exterior", **kw) == (
        "01-front-exterior-12-maple-st.jpg"
    )
    assert photo_index.export_filename(2, 2, room="", **kw) == "02-photo-12-maple-st.jpg"
    assert photo_index.export_filename(2, 2, slug="", room="", suffix="") == "02-photo.jpg", (
        "no suffix, no dangling hyphen"
    )


def test_a_long_descriptor_gives_way_before_the_address() -> None:
    name = photo_index.export_filename(
        1, 5, slug="word-" * 40, room="", suffix="5096-old-philadelphia-pike-kinzers-auction"
    )
    assert len(name) <= photo_index.FILENAME_MAX_CHARS
    assert name.endswith("-5096-old-philadelphia-pike-kinzers-auction.jpg")
    assert "--" not in name


def test_slugs_hold_nothing_a_path_or_portal_would_mangle() -> None:
    assert photo_index.clean_slug("../Kitchen / Island_2!") == "kitchen-island-2"
    assert photo_index.clean_suffix("130 Davis Rd., Auction") == "130-davis-rd-auction"


ROWS = [
    IndexRow(
        number=1,
        filename="01-front-exterior-x.jpg",
        caption="Front | exterior\nat dusk",
        room_label="Front exterior",
        original="a.jpg",
        edited=True,
    ),
    IndexRow(2, "02-kitchen-x.jpg", "", "Kitchen", "b.jpg", False),
]


def test_the_markdown_index_is_a_table_a_caption_cannot_break() -> None:
    text = photo_index.index_markdown("12 Maple St", "Auction Oct 12", ROWS)
    lines = text.splitlines()
    assert lines[0] == "# 12 Maple St — Photo Index"
    assert "Auction Oct 12" in text
    assert "| 01 | 01-front-exterior-x.jpg | Front \\| exterior at dusk | a.jpg |" in lines
    assert "| 02 | 02-kitchen-x.jpg | Kitchen | b.jpg |" in lines, "no caption: the room"


def test_the_csv_index_opens_cleanly_in_excel() -> None:
    text = photo_index.index_csv(ROWS)
    assert text.startswith("﻿"), "byte-order mark, or Excel guesses the code page"
    rows = list(csv.reader(io.StringIO(text.removeprefix("﻿"))))
    assert rows[0] == ["number", "filename", "what_it_shows", "room", "original", "edited"]
    assert rows[1] == [
        "1",
        "01-front-exterior-x.jpg",
        "Front | exterior\nat dusk",  # quoted by the writer, so it survives intact
        "Front exterior",
        "a.jpg",
        "yes",
    ]
    assert rows[2][-1] == "no"


def test_a_contact_sheet_grows_by_the_rows_it_needs() -> None:
    tile = contact_sheet.thumbnail(Image.new("RGB", (6000, 4000), (90, 120, 150)))
    assert tile.size == (420, 280)
    upright = contact_sheet.thumbnail(Image.new("RGB", (4000, 6000), (90, 120, 150)))
    assert upright.height == 280 and upright.width < 420, "verticals letterbox, never stretch"

    one_row = contact_sheet.compose([("01-a", tile)] * 3, "Title")
    full = contact_sheet.compose([("01-a", tile)] * 25, "Title")
    assert one_row.width == full.width
    assert full.height > one_row.height * 4, "five rows, and the 21st tile left for the next sheet"
