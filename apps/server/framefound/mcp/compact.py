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

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

# The short words of a line moved to nle/shotlist.py, so the pages and jobs
# that show a model the same lines can import them without the connector.
# The names stay here for every tool that uses them.
from framefound.nle.shotlist import LIGHT_SHORT as LIGHT_SHORT
from framefound.nle.shotlist import SHORT as SHORT
from framefound.nle.shotlist import clip_name as clip_name
from framefound.nle.shotlist import score as score
from framefound.nle.shotlist import short_id as short_id


def seconds(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.1f}"


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
