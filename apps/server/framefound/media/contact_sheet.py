"""Contact sheets: a listing's photographs on a few pages, numbered.

The check a person does before a set goes anywhere — is the front exterior
first, is there a second kitchen shot nobody wanted, did a bathroom get
missed — is a glance at a sheet, not a scroll through forty files. The
sheets are drawn from the rendered export, so they show the edits, and each
tile carries the number its file name starts with.
"""

from typing import Any

COLUMNS = 4
ROWS = 5
PER_SHEET = COLUMNS * ROWS
CELL_WIDTH = 420
# 3:2 is the camera's frame; verticals letterbox inside it rather than
# stretching the row.
CELL_IMAGE_HEIGHT = 280
LABEL_HEIGHT = 34
GUTTER = 12
BACKGROUND = (250, 250, 248)
INK = (30, 30, 30)


def thumbnail(image: Any) -> Any:
    """The tile for one photograph, made while its full render is still in
    memory — the sheet never re-reads the export."""
    from PIL import Image

    tile = image.convert("RGB")
    tile.thumbnail((CELL_WIDTH, CELL_IMAGE_HEIGHT), Image.Resampling.LANCZOS)
    return tile


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow built without FreeType: the bitmap font
        return ImageFont.load_default()


def compose(tiles: list[tuple[str, Any]], title: str = "") -> Any:
    """One sheet: up to PER_SHEET (label, thumbnail) pairs in a grid, the
    title across the top. Rows that are not needed are not drawn."""
    from PIL import Image, ImageDraw

    tiles = tiles[:PER_SHEET]
    rows = max(1, -(-len(tiles) // COLUMNS))
    header = 44 if title else 0
    cell_height = CELL_IMAGE_HEIGHT + LABEL_HEIGHT
    width = GUTTER + COLUMNS * (CELL_WIDTH + GUTTER)
    height = header + GUTTER + rows * (cell_height + GUTTER)
    sheet = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    if title:
        draw.text((GUTTER, 12), title, fill=INK, font=_font(22))

    label_font = _font(15)
    for index, (label, tile) in enumerate(tiles):
        column, row = index % COLUMNS, index // COLUMNS
        x = GUTTER + column * (CELL_WIDTH + GUTTER)
        y = header + GUTTER + row * (cell_height + GUTTER)
        # Centred in its cell, whatever its orientation.
        offset_x = x + (CELL_WIDTH - tile.width) // 2
        offset_y = y + (CELL_IMAGE_HEIGHT - tile.height) // 2
        sheet.paste(tile, (offset_x, offset_y))
        # Long names are cut rather than wrapped: the number leads, and the
        # number is what the sheet is for.
        text = label if len(label) <= 52 else label[:51] + "…"
        draw.text((x + 2, y + CELL_IMAGE_HEIGHT + 8), text, fill=INK, font=label_font)
    return sheet
