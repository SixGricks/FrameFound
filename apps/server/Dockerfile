# FrameFound server image — one image, many entrypoints (api/worker/scanner/...).
# TODO(m3): add FFmpeg + ExifTool layer. TODO(m4): CUDA-enabled variant for worker-ai.
FROM python:3.12-slim AS base

# Run as non-root (threat model: container compromise != root on host mounts).
RUN groupadd -r framefound && useradd -r -g framefound -u 1000 framefound

WORKDIR /app
COPY pyproject.toml README.md ./
COPY framefound ./framefound
# Milestone 0 installs only the core; extras added per milestone:
# TODO(m1): ".[db,queue]"  TODO(m2): ".[scanner]"  TODO(m3): ".[media]"
RUN pip install --no-cache-dir .

USER framefound
EXPOSE 8000
CMD ["uvicorn", "framefound.main:app", "--host", "0.0.0.0", "--port", "8000"]
