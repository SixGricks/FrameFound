"""Naming a listing's photographs, and the index that travels with them.

Before FrameFound did this, every shoot went through the same hand process
before its paid editing batch: pick the selects, rename each one
`01-front-exterior-brick-colonial-130-davis-rd-auction.jpg`, and write a
"Photo Index.md" saying what each photograph shows, plus contact sheets. The
file names are the SEO (portals and search engines read them), the index is
what the brochure and the ad copy are written from, and the sheets are how
anyone checks the set at a glance.

Everything here is pure: text in, text out. The export task and the API both
call it, so the name the page previews is the name the zip contains.
"""

import csv
import io
import re
from dataclasses import dataclass

_NOT_WORD = re.compile(r"[^a-z0-9]+")
# "09-24 130 Davis Rd", "2026-09-24 Old Farm", "00-00 5096 Old Philadelphia
# Pike": the shoot folders lead with a date (or a 00-00 placeholder for one),
# which says nothing about the property.
_LEADING_DATE = re.compile(r"^\s*(?P<year>\d{4}-)?(?P<month>\d{1,2})-(?P<day>\d{1,2})[\s_]+")

SUFFIX_MAX_CHARS = 120
SLUG_MAX_CHARS = 80
# A file name longer than this starts getting cut off in upload dialogs and
# portal admin screens; the descriptor gives way before the address does.
FILENAME_MAX_CHARS = 150


def slugify(text: str, *, max_words: int, max_chars: int, digits: bool = True) -> str:
    """Lowercase words joined by hyphens: nothing that could be a path, a
    space, or a character a portal would mangle."""
    words = [w for w in _NOT_WORD.split(str(text).lower()) if w]
    if not digits:
        words = [w for w in words if not w.isdigit()]
    return "-".join(words[:max_words])[:max_chars].strip("-")


def clean_suffix(text: str) -> str:
    return slugify(text, max_words=16, max_chars=SUFFIX_MAX_CHARS)


def clean_slug(text: str) -> str:
    """An operator-typed slug. Digits stay ("bedroom-2"); the AI's slugs go
    through recipe_picker.clean_slug, which drops them."""
    return slugify(text, max_words=10, max_chars=SLUG_MAX_CHARS)


def default_suffix(listing_name: str) -> str:
    """The suffix a listing gets until the operator types one: its name,
    without the date the shoot folder led with.

    "00-00 5096 Old Philadelphia Pike Kinzers" -> "5096-old-philadelphia-pike-kinzers"
    "12-14 Main St"                            -> "12-14-main-st"
    """
    name = listing_name
    match = _LEADING_DATE.match(listing_name)
    if match:
        rest = listing_name[match.end() :]
        # "12-14 Main St" is an address range, not a date. A shoot folder's
        # date gives itself away by a year, by the 00-00 placeholder, or by
        # the house number that follows it.
        if match["year"] or match["month"] == "00" or rest[:1].isdigit():
            name = rest
    return clean_suffix(name)


def export_filename(
    number: int,
    total: int,
    *,
    slug: str,
    room: str,
    suffix: str,
    naming: str = "seo",
) -> str:
    """The name one photograph carries in the zip.

    "seo": `01-kitchen-island-pantry-130-davis-rd-auction.jpg`. Hyphens,
    because search engines read them as word breaks and underscores as
    joins. Two digits until a listing passes 99 photographs, then three, so
    the names still sort in gallery order everywhere.

    "simple": the original `01_kitchen.jpg`, for portals that rename on
    upload anyway.
    """
    if naming == "simple":
        return f"{number:02d}_{room or 'photo'}.jpg"
    width = 2 if total <= 99 else 3
    lead = f"{number:0{width}d}"
    descriptor = slug or room.replace("_", "-") or "photo"
    tail = f"-{suffix}" if suffix else ""
    room_for_descriptor = FILENAME_MAX_CHARS - len(lead) - len(tail) - len(".jpg") - 1
    descriptor = descriptor[: max(room_for_descriptor, 8)].strip("-")
    return f"{lead}-{descriptor}{tail}.jpg"


@dataclass
class IndexRow:
    number: int
    filename: str
    caption: str
    room_label: str
    original: str
    edited: bool


def _cell(text: str) -> str:
    """Table-safe: a pipe would split the cell, a newline would end the row."""
    return " ".join(str(text).split()).replace("|", "\\|")


def index_markdown(title: str, notes: str, rows: list[IndexRow]) -> str:
    """The Photo Index: the listing's notes first (auction date, terms —
    whatever the photographs cannot say), then one row per photograph in
    gallery order, in the table layout the brochure workflow already reads."""
    lines = [f"# {' '.join(title.split())} — Photo Index", ""]
    if notes.strip():
        lines += [notes.strip(), ""]
    lines += [
        f"{len(rows)} photographs, in gallery order.",
        "",
        "| # | Filename | What it shows | Original |",
        "|---|---|---|---|",
    ]
    for row in rows:
        shows = row.caption or row.room_label
        lines.append(
            f"| {row.number:02d} | {_cell(row.filename)} | {_cell(shows)} | {_cell(row.original)} |"
        )
    return "\n".join(lines) + "\n"


def index_csv(rows: list[IndexRow]) -> str:
    """The same index as a spreadsheet — for mail merges, the ad tools, and
    anything that would rather not parse Markdown. The byte-order mark is for
    Excel, which otherwise reads UTF-8 as the local code page."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["number", "filename", "what_it_shows", "room", "original", "edited"])
    for row in rows:
        writer.writerow(
            [
                row.number,
                row.filename,
                row.caption,
                row.room_label,
                row.original,
                "yes" if row.edited else "no",
            ]
        )
    return "﻿" + out.getvalue()
