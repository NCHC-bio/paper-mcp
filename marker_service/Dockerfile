# syntax=docker/dockerfile:1
# Clean slim base — let uv resolve marker-pdf's FULL, COHERENT torch ecosystem
# (torch + torchvision + transformers + surya) in one pass. (An earlier conda
# `pytorch/pytorch` base produced a torch ↔ torchvision ABI mismatch —
# `operator torchvision::nms does not exist` — when uv upgraded the companions.)
#
# GPU: the default PyPI torch wheel bundles CUDA userspace libs, so the host RTX
# GPU is used when nvidia-container-toolkit + the compose `deploy.devices` block
# are enabled; it falls back to CPU automatically otherwise.
FROM python:3.11-slim

# MODEL_CACHE_DIR is the one that matters for surya 1.x (marker-pdf 1.10.2):
# its models come from datalab S3 (s3://...) via S3DownloaderMixin, which caches
# into surya `settings.MODEL_CACHE_DIR` (default ~/.cache/datalab/models, NOT
# HF_HOME). Point it inside /models so the marker-models volume persists the
# Caches that must land somewhere writable. Under the hardened runtime the
# root filesystem is read-only, and any library that quietly caches into
# $HOME takes the service down at the first extraction while /health still
# reports ok: torch 2.13 dispatches through Triton, which JIT-compiles
# kernels into ~/.triton on first inference. /tmp is a sized tmpfs in
# compose, so the writes stay inside the sandbox.
# ~2 GB of weights across container recreates — otherwise every rebuild/recreate
# re-downloads them. HF_HOME/TORCH_HOME cover the few HF/torch-hosted artifacts.
ENV PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    HF_HOME=/models \
    TORCH_HOME=/models/torch \
    MODEL_CACHE_DIR=/models/datalab \
    TRITON_CACHE_DIR=/tmp/triton \
    XDG_CACHE_HOME=/tmp/cache \
    MPLCONFIGDIR=/tmp/mpl \
    HOME=/tmp

# Where a model DOWNLOAD is staged — not where a request's scratch file goes.
# surya's `download_directory` pulls an entire model into a
# `tempfile.TemporaryDirectory()` and only then moves it into MODEL_CACHE_DIR,
# so TMPDIR is where ~2 GB of weights land on a cold cache. Left at /tmp that
# is the 2 GB tmpfs: measured on a real first run at 1.2 GB used for ONE
# 1.35 GB model while /models still held 12 KB. Several download on first use,
# so a larger one exhausts the tmpfs and the first extraction dies of ENOSPC
# — with /health reporting ok throughout, which is the failure mode this
# image keeps producing. /models is the persistent volume: disk rather than
# RAM, and already sized for the weights it ends up holding anyway.
#
# MARKER_SCRATCH_DIR keeps the request's own PDF in the tmpfs, which is what
# that size limit exists to contain. Caller-supplied bytes must not follow
# TMPDIR onto the volume.
ENV TMPDIR=/models/tmp MARKER_SCRATCH_DIR=/tmp
# Build-time only: a runtime volume mount hides everything under /models,
# which is why the CMD creates it again. This one keeps any build step
# that reaches for a temp file from landing on a TMPDIR that is not there.
RUN mkdir -p /models/tmp

WORKDIR /app

# Runtime libs marker/opencv (surya) need, plus a C compiler.
#
# gcc is NOT a build-time convenience here — it is needed at RUNTIME. torch
# 2.13 dispatches some ops (e.g. `_native.ops.bmm_outer_product`) through
# Triton, which JIT-compiles a CUDA utils module on first use and shells out
# to a compiler to do it. Without one the very first GPU inference dies with
# "RuntimeError: Failed to find C compiler", so every /extract answers 500 and
# the service is useless while still reporting healthy. Measured on this image:
# layout recognition crashed on the first page; with gcc the same PDF extracted
# in 13s. Slim images ship no toolchain, hence this line.
#
# libc6-dev is NOT optional alongside it: Debian's `gcc` only *Recommends*
# it, so under --no-install-recommends the compiler installs without crt1.o
# or the libc headers and every link dies with "cannot find Scrt1.o". That
# failure is invisible when testing by hand — a bare `apt-get install gcc`
# pulls libc6-dev in via Recommends and works — and only appears in the
# built image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Fast installs with uv (project convention). The uv binary is copied from its
# official distroless image (no pip-install-uv step).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
COPY pyproject.toml .
# BuildKit cache mount: the downloaded wheels (incl. the ~2 GB torch) persist in
# the build cache across rebuilds, so a failed build does NOT re-download them.
# (Hence no UV_NO_CACHE.) --system installs into the base image's python.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r pyproject.toml

# Bake in the font Marker fetches on first use. `download_font()` runs when a
# converter is constructed and writes into site-packages — which a read-only
# root refuses, so the container came up healthy and then failed every
# extraction with "Read-only file system: .../static". It is a no-op once the
# file exists, so doing it here makes the hardened runtime work and removes a
# network fetch from the first extraction, which a no-egress deployment could
# not have satisfied either.
RUN python -c "from marker.util import download_font; download_font()"

COPY app.py llm_config.py pipeline.py ./

# Models are NOT baked into the image — they download ONCE into the /models
# named volume (see docker-compose.yml) on first /extract and persist across
# container restarts AND image rebuilds, so iterating on the image never
# re-downloads the Surya/marker weights. (MODEL_CACHE_DIR=/models/datalab holds
# the datalab S3 weights; HF_HOME/TORCH_HOME cover HF/torch-hosted artifacts.)
EXPOSE 8002
# TMPDIR lives on the models volume, which is empty on a fresh deployment,
# so it has to be created at start-up rather than baked into the image.
CMD ["sh", "-c", "mkdir -p \"$TMPDIR\" \"$MARKER_SCRATCH_DIR\" && exec uvicorn app:app --host 0.0.0.0 --port 8002"]
