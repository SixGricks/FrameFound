"""Object removal: LaMa inpainting with a crop-around-the-mask strategy.

The LaMa graph takes fixed 512x512 inputs, so removing a trash can from a
24-megapixel frame means: find the mask's bounding box, take a square crop
around it with enough context for the model to understand the scene, resize
to 512, inpaint, resize back, and blend only the masked pixels into the
original. Full-frame quality everywhere except inside the mask, where the
model's 512 has to be enough — the same trade IOPaint and the commercial
tools make.

Measured on the production Xeons: 18.3 s per 512 tile. That is a queued
task, not a live brush; the editor says so instead of pretending.

The geometry (crop box, blending) is pure and tested; the model run is an
injected callable so CI, which has no ONNX runtime, never needs to load a
208 MB network to prove the maths.
"""

from typing import Any

import structlog

from framefound.ai.embeddings import EmbeddingUnavailable

log = structlog.get_logger()

MODEL_REPO = "Carve/LaMa-ONNX"
MODEL_FILE = "lama_fp32.onnx"
SIDE = 512
# Context around the mask: the model needs to see the wall to rebuild the
# wall. Margin is proportional to the mask, floored so a tiny blemish still
# gets real surroundings.
MARGIN_FACTOR = 0.6
MIN_MARGIN = 64
# The blend into the original is feathered by this many pixels (at the
# crop's scale) so the invented region has no hard seam.
FEATHER = 6

_session: Any = None


def _get_session() -> Any:
    global _session
    if _session is None:
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
        except ImportError as err:
            raise EmbeddingUnavailable(
                "Object removal support is not installed on this server"
            ) from err
        options = ort.SessionOptions()
        options.intra_op_num_threads = 5
        path = hf_hub_download(MODEL_REPO, MODEL_FILE, cache_dir="/models")
        log.info("inpaint.loading_model", model=MODEL_REPO)
        _session = ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])
    return _session


def _run_lama(image_512: Any, mask_512: Any) -> Any:
    """image (512,512,3) float 0..1, mask (512,512) float 0..1 -> (512,512,3).

    The Carve export returns pixels in 0..255 regardless of input scale.
    """
    import numpy as np

    session = _get_session()
    image_name, mask_name = (i.name for i in session.get_inputs())
    (out,) = session.run(
        None,
        {
            image_name: image_512.transpose(2, 0, 1)[None].astype(np.float32),
            mask_name: mask_512[None, None].astype(np.float32),
        },
    )
    return np.clip(out[0].transpose(1, 2, 0) / 255.0, 0.0, 1.0)


def crop_box(mask: Any, width: int, height: int) -> tuple[int, int, int, int]:
    """A square box around the mask with context margin, clamped to the
    frame. Returns (left, top, right, bottom); raises ValueError on an
    empty mask — "remove nothing" is a caller bug, not a render."""
    import numpy as np

    ys, xs = np.nonzero(mask > 0.5)
    if len(xs) == 0:
        raise ValueError("The mask selects nothing")
    left, right = int(xs.min()), int(xs.max()) + 1
    top, bottom = int(ys.min()), int(ys.max()) + 1

    span = max(right - left, bottom - top)
    margin = max(MIN_MARGIN, round(span * MARGIN_FACTOR))
    side = min(max(width, height), span + 2 * margin)

    cx, cy = (left + right) // 2, (top + bottom) // 2
    half = side // 2
    box_left = max(0, min(cx - half, width - side))
    box_top = max(0, min(cy - half, height - side))
    return (box_left, box_top, min(box_left + side, width), min(box_top + side, height))


def remove_region(image: Any, mask: Any, run_model: Any = None) -> Any:
    """Inpaint the masked region of a full-size image. Returns a new image.

    `run_model` is the 512-tile inpainter; defaults to LaMa. Injectable so
    the crop/blend geometry is testable without the network.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    if run_model is None:
        run_model = _run_lama

    width, height = image.size
    mask_arr = np.asarray(mask, dtype=np.float32)
    box = crop_box(mask_arr, width, height)
    left, top, right, bottom = box

    crop = image.crop(box).resize((SIDE, SIDE), Image.Resampling.LANCZOS)
    mask_img = Image.fromarray((np.clip(mask_arr, 0, 1) * 255).astype("uint8"), "L")
    mask_crop = mask_img.crop(box).resize((SIDE, SIDE), Image.Resampling.NEAREST)
    # Dilate slightly: LaMa behaves better when the hole fully covers the
    # object, and a brush rarely hits the exact silhouette edge.
    mask_crop = mask_crop.filter(ImageFilter.MaxFilter(9))

    crop_arr = np.asarray(crop, dtype=np.float32) / 255.0
    hole = (np.asarray(mask_crop, dtype=np.float32) / 255.0 > 0.5).astype(np.float32)
    filled = run_model(crop_arr, hole)

    # Paste back through the feathered mask, so ONLY invented pixels land in
    # the frame. Pasting the whole crop would silently push every unmasked
    # pixel inside the box through a 512 round-trip — softening perfectly
    # good context to sneak in a patch.
    soft = Image.fromarray((hole * 255).astype("uint8"), "L").filter(
        ImageFilter.GaussianBlur(FEATHER)
    )
    patch = Image.fromarray((filled * 255 + 0.5).astype("uint8"), "RGB").resize(
        (right - left, bottom - top), Image.Resampling.LANCZOS
    )
    soft_full = soft.resize((right - left, bottom - top), Image.Resampling.BILINEAR)

    out = image.copy()
    out.paste(patch, (left, top), mask=soft_full)
    return out
