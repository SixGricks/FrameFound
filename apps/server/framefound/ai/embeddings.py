"""Visual embeddings: CLIP image and text encoders in one shared space.

Runtime choice (ADR-0017): ONNX Runtime rather than PyTorch. Official torch
wheels require AVX and will not load on older Xeons; ONNX Runtime dispatches
kernels at runtime from CPUID and runs on SSE4.2. Measured on a 2010-era
Westmere: 288 ms per image, 35 ms per query — indexing is a one-time
background cost, and search stays interactive regardless.

Both encoders output 512-dim vectors into the same space, which is what makes
"red barn at sunset" match a photograph nobody ever captioned.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog

from framefound.config import get_settings

log = structlog.get_logger()

MODEL_REPO = "Xenova/clip-vit-base-patch32"
DIMENSIONS = 512
IMAGE_SIZE = 224
# CLIP's published preprocessing constants; changing them silently degrades
# match quality, so they live here rather than being re-derived.
PIXEL_MEAN = (0.48145466, 0.4578275, 0.40821073)
PIXEL_STD = (0.26862954, 0.26130258, 0.27577711)
MAX_TEXT_TOKENS = 77


class EmbeddingUnavailable(RuntimeError):
    """The embedding runtime or model cannot be loaded on this host."""


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    model_name: str


class EmbeddingProvider(Protocol):
    def embed_image(self, path: Path) -> EmbeddingResult: ...
    def embed_text(self, text: str) -> EmbeddingResult: ...


def _l2_normalise(values: Any) -> list[float]:
    import numpy as np

    array = np.asarray(values, dtype="float32").reshape(-1)
    norm = float(np.linalg.norm(array))
    if norm == 0.0:
        return [0.0] * len(array)
    normalised: list[float] = (array / norm).astype("float32").tolist()
    return normalised


class ClipOnnxProvider:
    """CLIP ViT-B/32 via ONNX Runtime. Sessions load lazily and are reused."""

    def __init__(self, threads: int = 3, cache_dir: str = "/models") -> None:
        self._threads = threads
        self._cache_dir = cache_dir
        self._vision: Any = None
        self._text: Any = None
        self._tokenizer: Any = None

    @property
    def model_name(self) -> str:
        return f"clip-onnx/{MODEL_REPO.split('/')[-1]}"

    def _session(self, filename: str) -> Any:
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
        except ImportError as err:
            raise EmbeddingUnavailable(
                "Visual search support is not installed on this server"
            ) from err
        options = ort.SessionOptions()
        options.intra_op_num_threads = self._threads
        path = hf_hub_download(MODEL_REPO, filename, cache_dir=self._cache_dir)
        return ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])

    def _vision_session(self) -> Any:
        if self._vision is None:
            log.info("embeddings.loading_vision_model", model=MODEL_REPO)
            self._vision = self._session("onnx/vision_model.onnx")
        return self._vision

    def _text_session(self) -> Any:
        if self._text is None:
            log.info("embeddings.loading_text_model", model=MODEL_REPO)
            self._text = self._session("onnx/text_model.onnx")
        return self._text

    def _tok(self) -> Any:
        if self._tokenizer is None:
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(
                hf_hub_download(MODEL_REPO, "tokenizer.json", cache_dir=self._cache_dir)
            )
        return self._tokenizer

    def _preprocess(self, path: Path) -> Any:
        import numpy as np
        from PIL import Image

        with Image.open(path) as img:
            image = img.convert("RGB")
            # Resize shortest side, then centre crop — CLIP's own recipe.
            width, height = image.size
            scale = IMAGE_SIZE / min(width, height)
            image = image.resize(
                (max(IMAGE_SIZE, round(width * scale)), max(IMAGE_SIZE, round(height * scale))),
                Image.Resampling.BICUBIC,
            )
            left = (image.width - IMAGE_SIZE) // 2
            top = (image.height - IMAGE_SIZE) // 2
            image = image.crop((left, top, left + IMAGE_SIZE, top + IMAGE_SIZE))
            pixels = np.asarray(image, dtype="float32") / 255.0

        pixels = (pixels - np.asarray(PIXEL_MEAN, dtype="float32")) / np.asarray(
            PIXEL_STD, dtype="float32"
        )
        return pixels.transpose(2, 0, 1)[None, ...].astype("float32")

    def embed_image(self, path: Path) -> EmbeddingResult:
        try:
            pixels = self._preprocess(path)
        except Exception as err:
            raise EmbeddingUnavailable("The image could not be read") from err
        outputs = self._vision_session().run(None, {"pixel_values": pixels})
        return EmbeddingResult(_l2_normalise(outputs[0]), self.model_name)

    def embed_text(self, text: str) -> EmbeddingResult:
        import numpy as np

        ids = self._tok().encode(text).ids[:MAX_TEXT_TOKENS]
        if not ids:
            raise EmbeddingUnavailable("Empty query")
        outputs = self._text_session().run(None, {"input_ids": np.asarray([ids], dtype="int64")})
        return EmbeddingResult(_l2_normalise(outputs[0]), self.model_name)


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """Provider factory (cached per process). Swappable in tests."""
    global _provider
    if _provider is None:
        _provider = ClipOnnxProvider(threads=get_settings().whisper_threads)
    return _provider
