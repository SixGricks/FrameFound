# MediaHub server image — one image, many entrypoints (api/worker/scanner/...).
# TODO(m3): add FFmpeg + ExifTool layer. TODO(m4): CUDA-enabled variant for worker-ai.
FROM python:3.12-slim AS base

# Run as non-root (threat model: container compromise != root on host mounts).
RUN groupadd -r mediahub && useradd -r -g mediahub -u 1000 mediahub

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mediahub ./mediahub
# Milestone 0 installs only the core; extras added per milestone:
# TODO(m1): ".[db,queue]"  TODO(m2): ".[scanner]"  TODO(m3): ".[media]"
RUN pip install --no-cache-dir .

USER mediahub
EXPOSE 8000
CMD ["uvicorn", "mediahub.main:app", "--host", "0.0.0.0", "--port", "8000"]
