# FrameFound server image — one image, many entrypoints (api/worker/scanner/...).
# TODO(m4): CUDA-enabled variant for worker-ai.
FROM python:3.12-slim AS base

# Media probing/processing binaries (invoked as subprocesses, never linked).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

# Run as non-root (threat model: container compromise != root on host mounts).
RUN groupadd -r framefound && useradd -r -g framefound -u 1000 framefound

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY framefound ./framefound
RUN pip install --no-cache-dir ".[db,queue,scanner,media,ai]"

# Pre-create the data mountpoint owned by the app user so Docker named
# volumes initialize with writable ownership (found in deployment: a
# root-owned volume + non-root user = PermissionError on first derivative).
RUN mkdir -p /data /models && chown framefound:framefound /data /models

USER framefound
EXPOSE 8000
CMD ["uvicorn", "framefound.main:app", "--host", "0.0.0.0", "--port", "8000"]
