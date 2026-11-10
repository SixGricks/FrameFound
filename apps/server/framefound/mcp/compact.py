"""Compact answers: one line per item, only what is there.

Cutting the Brookside Quick 30 through the connector, a 100-item
`find_media` answer had to be trimmed with jq before Claude could read it:
every item carried a signed URL, a server path nobody's computer opens, the
suggested tags and every empty field. The MCP list tools now answer with a
short text table by default; `verbose=true` (or a `fields` list) brings the
full structure back. The web UI and the panels are untouched — they use the
REST API, not these tools.

Ids are the first eight hex characters of the asset's UUID. Every tool that
takes an asset id accepts one (server._asset), so a line can be passed on
as it is.
"""

import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

SHORT = 8
_DJI = re.compile(r"^DJI_\d{14}_(\d{4})_\w+$", re.IGNORECASE)


def short_id(value: uuid.UUID | str) -> str:
    return str(value).replace("-", "")[:SHORT]


def clip_name(filename: str, width: int = 24) -> str:
    """A name short enough for a table: DJI_20261103051509_0639_D.MP4 is
    DJI_0639 (the counter is what the operator says out loud); anything
    else loses its extension and is cut to `width`."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    found = _DJI.match(stem)
    if found:
        return f"DJI_{found.group(1)}"
    stem = stem.replace(" ", "_")
    return stem if len(stem) <= width else stem[: width - 1] + "…"


def seconds(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.1f}"


def score(value: float | None) -> str:
    """.91 rather than 0.91: two characters saved on every line."""
    if value is None:
        return ""
    text = f"{value:.2f}"
    return text[1:] if text.startswith("0") else text


LIGHT_SHORT = {
    "golden hour": "golden",
    "blue hour": "blue",
    "low sun": "low-sun",
    "mid sun": "mid-sun",
    "high sun": "high-sun",
}


def table(header: str, lines: Iterable[str], footer: str = "") -> CallToolResult:
    body = "\n".join([header, *lines] + ([footer] if footer else []))
    return CallToolResult(content=[TextContent(type="text", text=body)])


def structured(model: BaseModel | Sequence[BaseModel], summary: str = "") -> CallToolResult:
    """The verbose answer: the full structure, as the tools gave it before."""
    if not isinstance(model, BaseModel):
        data: Any = {"result": [item.model_dump(mode="json") for item in model]}
    else:
        data = model.model_dump(mode="json")
    import json

    text = json.dumps(data, indent=1)
    return CallToolResult(
        content=[TextContent(type="text", text=summary + ("\n" if summary else "") + text)],
        structured_content=data,
    )


def pick(row: dict[str, Any], fields: list[str], formats: dict[str, Callable[[Any], str]]) -> str:
    """One line holding just `fields`, in order, each formatted; a field
    with nothing in it is left out rather than shown empty."""
    parts = []
    for name in fields:
        value = row.get(name)
        if value in (None, "", [], {}):
            continue
        fmt = formats.get(name)
        parts.append(fmt(value) if fmt else str(value))
    return " ".join(parts)
