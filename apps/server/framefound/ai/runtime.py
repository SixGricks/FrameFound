"""Where model weights live and which ONNX Runtime provider runs them.

One place for both, so an install outside the containers (the native Mac
install, docs/mac.md) can keep its model cache somewhere other than the
container-only `/models` path and run the vision models on Apple silicon.
"""

from functools import cache

import structlog

from framefound.config import get_settings

log = structlog.get_logger()


def models_dir() -> str:
    """The model cache as a string (what hf_hub_download and faster-whisper take)."""
    return str(get_settings().models_dir)


@cache
def onnx_providers() -> list[str]:
    """Execution providers to try, best first, always ending on the CPU.

    "coreml" is only honoured when this onnxruntime build actually has the
    provider (the macOS wheels do; Linux ones do not), so a copied .env never
    stops a server from loading its models. ONNX Runtime assigns each part of
    a model to the first provider that can run it and leaves the rest on the
    CPU, which is why the CPU stays in the list."""
    wanted = get_settings().onnx_provider
    if wanted == "coreml":
        import onnxruntime as ort

        if "CoreMLExecutionProvider" in ort.get_available_providers():
            return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        log.warning("onnx.coreml_unavailable", available=ort.get_available_providers())
    return ["CPUExecutionProvider"]
