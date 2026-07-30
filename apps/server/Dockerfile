# FrameFound server image — one image, many entrypoints (api/worker/scanner/...).
# TODO(m4): CUDA-enabled variant for worker-ai.
FROM python:3.12-slim AS base

# Media probing/processing binaries (invoked as subprocesses, never linked).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

# Run as non-root (threat model: container compromise != root on host mounts).
# A real home directory matters: HuggingFace's Xet downloader writes a chunk
# cache under $HOME and fails opaquely without one (found in deployment).
RUN groupadd -r framefound \
    && useradd -r -g framefound -u 1000 -m -d /home/framefound framefound

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY framefound ./framefound
# pmtiles: a single Go binary used to pull a regional basemap out of the
# 125 GB planet archive over range requests. ~10 MB, no runtime deps.
ARG PMTILES_VERSION=1.28.0
RUN set -eux;     arch="$(dpkg --print-architecture)";     case "$arch" in amd64) pm=x86_64 ;; arm64) pm=arm64 ;; *) pm="" ;; esac;     if [ -n "$pm" ]; then       curl -fsSL "https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_${pm}.tar.gz"         | tar -xz -C /usr/local/bin pmtiles && chmod +x /usr/local/bin/pmtiles;     fi

RUN pip install --no-cache-dir ".[db,queue,scanner,media,ai]"

# Pre-create the data mountpoint owned by the app user so Docker named
# volumes initialize with writable ownership (found in deployment: a
# root-owned volume + non-root user = PermissionError on first derivative).
RUN mkdir -p /data /models && chown framefound:framefound /data /models

# Keep every model artifact (weights + Xet chunk cache) on the models volume
# so it survives container rebuilds instead of re-downloading each time.
ENV HOME=/home/framefound \
    HF_HOME=/models/huggingface \
    XDG_CACHE_HOME=/models/cache

USER framefound
EXPOSE 8000
CMD ["uvicorn", "framefound.main:app", "--host", "0.0.0.0", "--port", "8000"]
