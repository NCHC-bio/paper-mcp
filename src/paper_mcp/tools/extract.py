"""`extract_pdf` and `get_job` — turning a caller's PDF into usable data.

A cache hit returns the bundle immediately. A miss starts a background job,
because Marker takes minutes on a dense paper and holding an MCP request open
that long gets the connection dropped rather than answered.

The caller supplies the bytes (SRS v1.0). Nothing here reaches the network:
acquiring a paper is the calling agent's job, and it already has better ways
to do it than this service had.
"""
from __future__ import annotations

import base64
import binascii
import logging
from typing import Literal

from pydantic import BaseModel, Field

from paper_mcp.artifacts import ArtifactStore
from paper_mcp.bundle import Bundle
from paper_mcp.config import settings
from paper_mcp.jobs import JobStatus, JobStore
from paper_mcp.models import InvalidArgumentError, NotFoundError, UpstreamError
from paper_mcp.pipelines.build_bundle import build_bundle, bundle_key, load_cached
from paper_mcp.pipelines.marker_client import MarkerClient

logger = logging.getLogger(__name__)

# Every PDF starts with this. Checking it turns "Marker crashed on page 1"
# into an error at the boundary naming what was actually wrong.
_PDF_MAGIC = b"%PDF-"

_store: ArtifactStore | None = None
_jobs: JobStore | None = None
_marker: MarkerClient | None = None


def artifact_store() -> ArtifactStore:
    global _store
    if _store is None:
        _store = ArtifactStore(settings().artifact_root)
    return _store


def job_store() -> JobStore:
    global _jobs
    if _jobs is None:
        _jobs = JobStore()
    return _jobs


def marker_client() -> MarkerClient:
    global _marker
    if _marker is None:
        _marker = MarkerClient(settings().marker_url)
    return _marker


class ExtractResult(BaseModel):
    """Either the document, or a handle to the extraction producing it."""

    status: Literal["ready", "extracting"]
    bundle: Bundle | None = None
    job: JobStatus | None = None
    hint: str = Field(default="", description="What the caller should do next.")


def decode_pdf(content_base64: str, *, max_bytes: int) -> bytes:
    """Decode and sanity-check an uploaded PDF.

    Three rejections, all at the boundary and all typed, because each one is
    a different mistake and a caller can only fix what it can distinguish:
    malformed base64, bytes that are not a PDF at all, and a file past the
    size ceiling. Marker would surface all three as the same opaque failure
    several GPU-minutes later.
    """
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidArgumentError(
            "content_base64 is not valid base64; send the PDF bytes base64-encoded"
        ) from exc

    if not data:
        raise InvalidArgumentError("content_base64 decoded to zero bytes")
    if len(data) > max_bytes:
        raise InvalidArgumentError(
            f"PDF is {len(data)} bytes, over the {max_bytes}-byte limit; "
            "split it or raise PAPER_MCP_MAX_UPLOAD_BYTES"
        )
    if not data.startswith(_PDF_MAGIC):
        raise InvalidArgumentError(
            "those bytes are not a PDF (no %PDF- header). This tool extracts "
            "PDFs only — decode base64 of the file itself, not of a URL or text."
        )
    return data


async def tool_extract_pdf(content_base64: str, filename: str | None = None) -> ExtractResult:
    """Extract a caller-supplied PDF into markdown plus a figure index."""
    cfg = settings()
    pdf = decode_pdf(content_base64, max_bytes=cfg.max_upload_bytes)

    store = artifact_store()
    key = bundle_key(pdf)

    cached = load_cached(key, store=store)
    if cached is not None:
        return ExtractResult(
            status="ready",
            bundle=cached,
            hint="Cached. markdown holds the document; figures[].image_url resolves to images.",
        )

    async def run() -> str:
        bundle = await build_bundle(
            pdf,
            filename=filename,
            store=store,
            marker=marker_client(),
            max_pages=cfg.marker_max_pages,
            ttl_hours=cfg.artifact_ttl_hours,
        )
        return bundle.bundle_id

    # Keyed by content, so two callers uploading the same paper join one job
    # rather than queueing two identical GPU runs.
    job = job_store().submit(content_key=key, run=run)
    if job.state == "error":
        # The store hands a previously-failed job back so the failure is seen
        # rather than silently re-queued. Reporting it as `extracting` would
        # keep it invisible: the hint below says call again, so a caller would
        # loop on a dead job forever. The store has already released the key,
        # so calling again genuinely retries.
        raise UpstreamError(
            f"extraction of these bytes failed: {job.error or 'unknown error'}. "
            "Calling extract_pdf again retries it; a repeat failure is the "
            "document, not a transient fault."
        )
    return ExtractResult(
        status="extracting",
        job=job,
        hint=(
            f"Extraction started (job {job.job_id}). Marker takes roughly a minute per "
            "dense page. Poll get_job, or call extract_pdf again — it returns the "
            "bundle once the cache is warm."
        ),
    )


async def tool_get_job(job_id: str) -> JobStatus:
    """Check a background extraction."""
    status = job_store().get(job_id)
    if status is None:
        raise NotFoundError(
            f"no job {job_id!r}. Job handles are forgotten on restart; call "
            "extract_pdf again — a finished extraction is a cache hit.",
        )
    return status
