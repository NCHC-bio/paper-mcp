# paper-mcp — the extraction service.
#
# Deliberately plain. Until v1.0 this image carried an nsjail builder stage and
# five TeX Live package sets, because the service compiled caller-supplied
# LaTeX and had to isolate it. With compilation out of scope there is nothing
# here that executes untrusted input: the service parses arguments, talks to
# Marker over the compose network, and writes derived files to its artifact
# cache. So the jail, the TeX distribution, and the `seccomp=unconfined` that
# nsjail's namespace creation required are all gone.
#
# The untrusted input is a caller's PDF, and it is handled in the Marker
# container, which is where the containment now lives (SRS NFR-02).
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    PAPER_MCP_HOST=0.0.0.0 \
    PAPER_MCP_PORT=8000 \
    PAPER_MCP_ARTIFACT_ROOT=/app/artifacts

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# Skills are served as MCP prompts, so they ship with the image.
COPY skills ./skills
# Installed FROM THE LOCKFILE, not from the dependency ranges.
#
# `uv pip install --system .` resolved `pyproject.toml` and ignored the
# `uv.lock` copied in on the line above, so every build took whatever was
# newest on PyPI. That is not a theoretical drift: it shipped `mcp` 2.1.0
# against a lockfile pinning 2.0.0, and 2.1.0 renders a failed tool call as a
# bare "Error executing tool extract_pdf" with the reason stripped. Every
# worded error this service raises — "not valid base64; send the PDF bytes
# base64-encoded", "quota exceeded; retry in 3600s", "Marker is unreachable,
# check /health" — reached a real connector as that bare string, which voids
# the error contract (SRS I-8 #7) in exactly the deployment nobody tests
# locally. 204 green tests on 2.0.0 said nothing about it.
#
# Exported rather than `uv sync`, because this image installs system-wide and
# runs the `paper-mcp` console script directly rather than through a venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --no-dev --no-emit-project --format requirements-txt \
        -o /tmp/requirements.txt \
    && uv pip install --system --no-deps -r /tmp/requirements.txt \
    && uv pip install --system --no-deps . \
    && rm /tmp/requirements.txt

# The artifact cache. Mounted as a volume in compose so bundles survive a
# container replacement — re-extracting a paper costs GPU minutes.
RUN mkdir -p /app/artifacts

EXPOSE 8000
CMD ["paper-mcp"]
