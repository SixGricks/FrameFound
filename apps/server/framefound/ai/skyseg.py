"""Sky segmentation: which pixels of a photograph are sky.

SegFormer-b0 trained on ADE20K, via the same AVX-free ONNX path as CLIP and
the face models. Measured on the production Xeons at 0.77 s per image, which
is why the editor can afford to run it per photograph rather than per
session. The model is 15 MB and downloads once into the models cache.

The mask is a soft 0..1 float array at the source image's size. Class 2 is
ADE20K's sky; everything here is deliberately dumb about what "sky" means —
the model was trained on scenes, and a photograph of a ceiling stays a
ceiling.
"""

from typing import Any

import structlog

from framefound.ai.embeddings import EmbeddingUnavailable

log = structlog.get_logger()

MODEL_REPO = "lquint/segformer-b0-finetuned-ade-512-512-onnx"
MODEL_FILE = "onnx/model.onnx"
SIDE = 512
ADE_SKY_CLASS = 2
# ImageNet normalisation, SegFormer's published preprocessing.
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

_session: Any = None


def _get_session() -> Any:
    global _session
    if _session is None:
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
        except ImportError as err:
            raise EmbeddingUnavailable(
                "Sky replacement support is not installed on this server"
            ) from err
        options = ort.SessionOptions()
        options.intra_op_num_threads = 3
        path = hf_hub_download(MODEL_REPO, MODEL_FILE, cache_dir="/models")
        log.info("skyseg.loading_model", model=MODEL_REPO)
        _session = ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])
    return _session


def sky_mask(image: Any) -> Any:
    """A float32 mask, 1.0 where sky, at the image's own size.

    Segmentation runs at 512x512 (the model's training size) and the mask is
    upsampled — the horizon line at full resolution comes from the feathering
    the compositor applies, not from per-pixel classification, which is also
    how the commercial tools do it.
    """
    import numpy as np
    from PIL import Image

    session = _get_session()
    small = image.convert("RGB").resize((SIDE, SIDE), Image.Resampling.BILINEAR)
    arr = np.asarray(small, dtype=np.float32) / 255.0
    arr = (arr - np.asarray(_MEAN, dtype=np.float32)) / np.asarray(_STD, dtype=np.float32)
    batch = arr.transpose(2, 0, 1)[None]

    (logits,) = session.run(None, {session.get_inputs()[0].name: batch})
    classes = logits[0].argmax(axis=0)  # (h, w) at the model's output stride
    mask = (classes == ADE_SKY_CLASS).astype(np.float32)

    mask_img = Image.fromarray((mask * 255.0).astype("uint8"), "L")
    mask_img = mask_img.resize(image.size, Image.Resampling.BILINEAR)
    return np.asarray(mask_img, dtype=np.float32) / 255.0


def sky_fraction(image: Any) -> float:
    """How much of the frame is sky, 0..1. The editor uses this to say
    "this looks like an interior" instead of silently doing nothing."""
    import numpy as np

    return float(np.mean(sky_mask(image)))
