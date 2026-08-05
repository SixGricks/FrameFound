"""The AI recipe-picker: Claude judges, the local engine renders.

This is the architecture decision that matters. The model never touches
pixels — it looks at a 768px preview and returns slider values for the
develop engine, which renders them locally at the photograph's full
resolution. Full 6000px output, no invented detail, an inspectable recipe
the operator can adjust afterwards, and a cost measured in fractions of a
cent per photograph. The commercial tools' per-photo judgment, without
handing them the pixels or the pricing.

What leaves the machine: one compressed preview per photograph, to the
Anthropic API, only while the operator-initiated run is executing. The
response is numbers.
"""

import base64
import json
from typing import Any

import structlog

from framefound.media import develop as develop_lib

log = structlog.get_logger()

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
TIMEOUT_S = 90.0
PREVIEW_EDGE = 768

# The tool schema *is* the contract: the model is forced to answer in slider
# values, so there is nothing to parse and nothing to hallucinate around.
RECIPE_TOOL = {
    "name": "set_develop_recipe",
    "description": "Set the develop sliders for this real-estate photograph.",
    "input_schema": {
        "type": "object",
        "properties": {
            "auto_wb": {
                "type": "number",
                "description": "White-balance neutralisation strength 0-1. Interiors with "
                "warm/yellow cast usually want 0.7-1.0; already-neutral frames 0-0.2.",
            },
            "exposure": {
                "type": "number",
                "description": "EV -2..2. MLS finals are bright: typical +0.2 to +0.8 for "
                "interiors, 0 to +0.3 for exteriors. Never blow out.",
            },
            "contrast": {"type": "number", "description": "-1..1, subtle. Usually 0-0.15."},
            "shadows": {
                "type": "number",
                "description": "-1..1. Lift dark corners toward even illumination: 0.2-0.5 "
                "typical for interiors.",
            },
            "highlights": {"type": "number", "description": "-1..1. Negative recovers."},
            "window_pull": {
                "type": "number",
                "description": "0-1. Raise when bright windows wash out: brings the view "
                "back while keeping interior brightness.",
            },
            "local_contrast": {
                "type": "number",
                "description": "0-1. Midtone punch after lifting: 0.2-0.4 typical.",
            },
            "vibrance": {"type": "number", "description": "-1..1. Tasteful: 0.1-0.25."},
            "saturation": {"type": "number", "description": "-1..1. Rarely needed."},
            "temperature": {
                "type": "number",
                "description": "-1..1 creative nudge AFTER auto_wb. Usually 0.",
            },
            "tint": {"type": "number", "description": "-1..1. Usually 0."},
            "rotate": {
                "type": "number",
                "description": "Degrees -5..5 to level a visibly tilted horizon/counter. "
                "0 unless clearly tilted.",
            },
            "keystone": {
                "type": "number",
                "description": "-1..1. Positive corrects verticals converging toward the "
                "top. Be conservative; 0 unless walls clearly lean.",
            },
            "needs_sky_replacement": {
                "type": "boolean",
                "description": "True when the sky is blown white or drab grey and the "
                "photo is an exterior that would benefit from a sky swap.",
            },
            "notes": {"type": "string", "description": "One short sentence on what you did."},
        },
        "required": ["auto_wb", "exposure", "shadows", "notes"],
    },
}

SYSTEM = (
    "You are a real-estate photo editor matching MLS-final standards: neutral "
    "whites (no colour cast), bright and evenly lit rooms, controlled window "
    "highlights with the view visible, straight verticals, tasteful colour. "
    "The photograph must remain honest — corrected, never exaggerated. Look at "
    "the photograph and call set_develop_recipe with the slider values that get "
    "it there. The sliders are applied by a deterministic engine to the "
    "full-resolution original."
)


class RecipePickUnavailable(RuntimeError):
    """The API refused or the response was not usable."""


def preview_bytes(image: Any) -> bytes:
    """The 768px JPEG that goes to the API — small enough to be cheap, big
    enough to judge a colour cast and a blown window."""
    import io

    from PIL import Image

    copy = image.copy()
    copy.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    copy.convert("RGB").save(out, "JPEG", quality=80)
    return out.getvalue()


def pick_recipe(preview_jpeg: bytes, api_key: str, model: str) -> dict[str, Any]:
    """One photograph in, one cleaned recipe out. Synchronous — callers run
    it in a worker or a thread."""
    import httpx

    body = {
        "model": model,
        "max_tokens": 700,
        "system": SYSTEM,
        "tools": [RECIPE_TOOL],
        "tool_choice": {"type": "tool", "name": "set_develop_recipe"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(preview_jpeg).decode(),
                        },
                    },
                    {"type": "text", "text": "Edit this photograph to MLS-final standard."},
                ],
            }
        ],
    }
    response = httpx.post(
        API_URL,
        json=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        timeout=TIMEOUT_S,
    )
    if response.status_code != 200:
        # The status line is safe to surface; the body may echo request data.
        raise RecipePickUnavailable(f"Anthropic API returned {response.status_code}")
    try:
        payload = response.json()
        tool_use = next(block for block in payload["content"] if block.get("type") == "tool_use")
        raw: dict[str, Any] = dict(tool_use["input"])
    except (KeyError, StopIteration, ValueError, json.JSONDecodeError) as err:
        raise RecipePickUnavailable("The model returned no recipe") from err

    notes = str(raw.pop("notes", ""))[:200]
    needs_sky = bool(raw.pop("needs_sky_replacement", False))
    recipe = develop_lib.clean_recipe(raw)  # clamps; drops anything off-schema
    log.info(
        "recipe_picker.picked",
        fields=sorted(recipe.keys()),
        needs_sky=needs_sky,
        notes=notes,
    )
    return {"recipe": recipe, "needs_sky_replacement": needs_sky, "notes": notes}
