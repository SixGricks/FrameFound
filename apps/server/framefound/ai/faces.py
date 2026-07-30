"""Face detection and recognition, local and CPU-only.

Two ONNX models from InsightFace's `buffalo_l` pack:

- **SCRFD** finds faces and returns boxes with a confidence.
- **ArcFace** turns each aligned crop into a 512-d embedding where the same
  person lands close together regardless of expression, lighting or age.

ONNX Runtime for the same reason as CLIP (ADR-0017): it dispatches on CPUID at
run time, so it works on this deployment's pre-AVX Westmere Xeons where the
official PyTorch wheels cannot even load.

Nothing here identifies anybody. It produces vectors; grouping them into people
and putting names to those groups is `ai/people.py` and the operator. There is
no pre-trained celebrity set, no external lookup, and no network call at
inference time — a face never leaves this machine.
"""

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# The `buffalo_l` pack, published as ONNX. det_10g is the accurate detector;
# w600k_r50 is the recognition model whose embeddings are the whole point.
MODEL_REPO = "public-data/insightface"
DETECTOR_FILE = "models/buffalo_l/det_10g.onnx"
RECOGNISER_FILE = "models/buffalo_l/w600k_r50.onnx"

DIMENSIONS = 512
DETECTOR_SIZE = 640  # SCRFD's trained input
RECOGNISER_SIZE = 112  # ArcFace's trained input

# Below this a "face" is usually a pattern in foliage or a reflection. Chosen
# high rather than low: a missed face costs one thumbnail, a false one puts a
# tree in somebody's photo album.
MIN_DETECTION_SCORE = 0.5
# Faces smaller than this fraction of the frame carry too little detail for a
# stable embedding — a 20px face in a wide drone shot is noise, and clustering
# noise produces confident nonsense.
MIN_FACE_FRACTION = 0.02


@dataclass(frozen=True)
class DetectedFace:
    """A face found in one image, in normalised coordinates."""

    x: float
    y: float
    w: float
    h: float
    score: float
    embedding: list[float]


class FaceModelUnavailable(RuntimeError):
    """The models are not present and could not be fetched."""


def _l2_normalise(vector: Any) -> list[float]:
    import numpy as np

    array = np.asarray(vector, dtype="float32").reshape(-1)
    norm = float(np.linalg.norm(array))
    # Same convention as the CLIP vectors: unit length, so cosine similarity is
    # a dot product and pgvector's <=> operator agrees with in-Python maths.
    return (array / norm).tolist() if norm > 0 else array.tolist()


class InsightFaceOnnx:
    """SCRFD + ArcFace, loaded lazily and shared per process."""

    def __init__(self, cache_dir: str = "/models") -> None:
        # Same volume as the CLIP weights; there is no separate model dir.
        self._cache_dir = cache_dir
        self._detector: Any = None
        self._recogniser: Any = None

    def _session(self, filename: str) -> Any:
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
        except ImportError as err:
            raise FaceModelUnavailable(
                "Face recognition needs the `ai` extra (onnxruntime)."
            ) from err

        path = hf_hub_download(repo_id=MODEL_REPO, filename=filename, cache_dir=self._cache_dir)
        options = ort.SessionOptions()
        # One thread: several workers share this box and the queue provides the
        # parallelism. Letting ORT grab every core starved the API once already.
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        return ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])

    def detector(self) -> Any:
        if self._detector is None:
            log.info("faces.loading_detector", model=DETECTOR_FILE)
            self._detector = self._session(DETECTOR_FILE)
        return self._detector

    def recogniser(self) -> Any:
        if self._recogniser is None:
            log.info("faces.loading_recogniser", model=RECOGNISER_FILE)
            self._recogniser = self._session(RECOGNISER_FILE)
        return self._recogniser

    def detect(self, image_path: Path) -> list[DetectedFace]:
        """Every usable face in one image, with its embedding."""
        try:
            import numpy as np
            from PIL import Image
        except ImportError as err:  # pragma: no cover - media extra always present
            raise FaceModelUnavailable("Image support is not installed") from err

        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
            width, height = image.size
            boxes = self._detect_boxes(image)

        faces: list[DetectedFace] = []
        with Image.open(image_path) as opened:
            rgb = opened.convert("RGB")
            for x1, y1, x2, y2, score in boxes:
                box_w, box_h = x2 - x1, y2 - y1
                if box_w <= 0 or box_h <= 0:
                    continue
                # Reject tiny faces before spending a recognition pass on them.
                if (box_w / width) * (box_h / height) < MIN_FACE_FRACTION**2:
                    continue
                crop = rgb.crop((int(x1), int(y1), int(x2), int(y2))).resize(
                    (RECOGNISER_SIZE, RECOGNISER_SIZE), Image.Resampling.BILINEAR
                )
                vector = self._embed_crop(np.asarray(crop, dtype="float32"))
                faces.append(
                    DetectedFace(
                        x=round(x1 / width, 6),
                        y=round(y1 / height, 6),
                        w=round(box_w / width, 6),
                        h=round(box_h / height, 6),
                        score=round(float(score), 4),
                        embedding=vector,
                    )
                )
        return faces

    def _detect_boxes(self, image: Any) -> list[tuple[float, float, float, float, float]]:
        import numpy as np
        from PIL import Image as PilImage

        width, height = image.size
        scale = min(DETECTOR_SIZE / width, DETECTOR_SIZE / height)
        resized = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            PilImage.Resampling.BILINEAR,
        )
        # Letterbox onto a square canvas: SCRFD expects a fixed input and
        # stretching would distort every face it then tries to measure.
        canvas = np.zeros((DETECTOR_SIZE, DETECTOR_SIZE, 3), dtype="float32")
        canvas[: resized.height, : resized.width] = np.asarray(resized, dtype="float32")
        blob = ((canvas - 127.5) / 128.0).transpose(2, 0, 1)[None]

        session = self.detector()
        outputs = session.run(None, {session.get_inputs()[0].name: blob})
        return _decode_scrfd(outputs, scale)

    def _embed_crop(self, crop: Any) -> list[float]:
        blob = ((crop - 127.5) / 127.5).transpose(2, 0, 1)[None]
        session = self.recogniser()
        outputs = session.run(None, {session.get_inputs()[0].name: blob})
        return _l2_normalise(outputs[0])


def _decode_scrfd(
    outputs: list[Any], scale: float
) -> list[tuple[float, float, float, float, float]]:
    """Turn SCRFD's per-stride score/box tensors into image-space boxes.

    The model emits three strides (8, 16, 32), each with a score tensor and a
    distance-to-edge box tensor over an implicit anchor grid. Anchors are two
    per cell. Decoding it here rather than pulling in the full insightface
    package keeps the dependency to onnxruntime, which is what actually has to
    work on this hardware.
    """
    import numpy as np

    strides = (8, 16, 32)
    results: list[tuple[float, float, float, float, float]] = []
    half = len(outputs) // 2

    for index, stride in enumerate(strides):
        if index >= half:
            break
        scores = np.asarray(outputs[index]).reshape(-1)
        deltas = np.asarray(outputs[index + half]).reshape(-1, 4)
        if scores.size == 0 or deltas.shape[0] != scores.size:
            continue

        cells = DETECTOR_SIZE // stride
        per_cell = max(1, scores.size // (cells * cells))
        ys, xs = np.mgrid[0:cells, 0:cells]
        centres = np.stack([xs.ravel(), ys.ravel()], axis=1).astype("float32") * stride
        centres = np.repeat(centres, per_cell, axis=0)[: scores.size]

        keep = scores >= MIN_DETECTION_SCORE
        if not keep.any():
            continue
        centres, deltas, scores = centres[keep], deltas[keep] * stride, scores[keep]

        boxes = np.stack(
            [
                centres[:, 0] - deltas[:, 0],
                centres[:, 1] - deltas[:, 1],
                centres[:, 0] + deltas[:, 2],
                centres[:, 1] + deltas[:, 3],
            ],
            axis=1,
        )
        for box, score in zip(boxes / scale, scores, strict=False):
            results.append(
                (float(box[0]), float(box[1]), float(box[2]), float(box[3]), float(score))
            )

    return _suppress_overlaps(results)


def _suppress_overlaps(
    boxes: list[tuple[float, float, float, float, float]], iou_threshold: float = 0.4
) -> list[tuple[float, float, float, float, float]]:
    """Standard non-maximum suppression.

    Every stride fires on the same face, so without this one person becomes
    three people the moment clustering runs.
    """
    ordered = sorted(boxes, key=lambda b: -b[4])
    kept: list[tuple[float, float, float, float, float]] = []
    for candidate in ordered:
        if all(_iou(candidate, existing) < iou_threshold for existing in kept):
            kept.append(candidate)
    return kept


def _iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    overlap = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if overlap <= 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - overlap
    return overlap / union if union > 0 else 0.0


@functools.cache
def get_face_provider() -> InsightFaceOnnx:
    return InsightFaceOnnx()
