"""`extract_pdf` and `get_job` — turning a caller's PDF into usable data.

A cache hit returns the bundle immediately. A miss starts a background job,
because Marker takes minutes on a dense paper and holding an MCP request open
that long gets the connection dropped rather than answered.

The caller supplies the bytes (SRS v1.0). Nothing here reaches the network:
acquiring a paper is the calling agent's job, and it already has better ways
to do it than this service had.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from paper_mcp.artifacts import ArtifactStore
from paper_mcp.bundle import Bundle
from paper_mcp.config import settings
from paper_mcp.jobs import JobStatus, JobStore
from paper_mcp.models import InvalidArgumentError, NotFoundError, UpstreamError
from paper_mcp.pipelines.build_bundle import build_bundle, bundle_key, load_cached
from paper_mcp.pipelines.marker_client import MarkerClient, page_count

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
        _jobs = JobStore(concurrency=settings().job_concurrency)
    return _jobs


def spool_dir() -> Path:
    """Where uploads wait on disk between acceptance and extraction.

    Beside the artifact cache rather than inside it: these are the caller's
    original documents, and nothing here is ever served over HTTP.
    """
    path = settings().artifact_root.parent / "spool"
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_spool() -> int:
    """Delete every spooled upload; returns how many went.

    Called at startup, where the reasoning is exact rather than heuristic:
    the job store is in memory, so a restart has already forgotten every job,
    and any file still here is waiting for work that will never resume. With
    a 100 MB ceiling, leaving them is how a long-lived deployment fills its
    disk with documents nobody wants.
    """
    removed = 0
    for stale in spool_dir().glob("*.pdf"):
        try:
            stale.unlink()
            removed += 1
        except OSError:  # pragma: no cover - a file in use is not fatal
            logger.warning("could not clear spooled upload %s", stale.name)
    if removed:
        logger.info("cleared %d spooled upload(s) orphaned by a restart", removed)
    return removed


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

    # The header is not proof the file is readable. A 15-byte stub and a
    # truncated download both carry it, both passed, and both took the single
    # GPU slot before failing minutes later as "Marker returned HTTP 500" —
    # which reads as "the service is broken, retry", the opposite of the
    # truth. Opening the document costs microseconds and answers the question
    # now, with the page count as a by-product of proving it.
    try:
        pages = page_count(data)
    except Exception as exc:
        raise InvalidArgumentError(
            f"that PDF could not be opened ({type(exc).__name__}); it is "
            "truncated or corrupt. Re-download it and try again — retrying "
            "these bytes will fail the same way."
        ) from exc
    if pages <= 0:
        raise InvalidArgumentError(
            "that PDF could not be opened: it reports zero pages, so there is "
            "nothing to extract."
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

    jobs = job_store()

    # Spooled rather than closed over. A queued job that holds its upload in
    # memory turns a queue of large papers into a queue of large buffers: with
    # a 100 MB ceiling and a single worker, ten waiting uploads pinned
    # hundreds of megabytes for no reason but waiting. On disk, the cost is
    # one in-flight document regardless of queue depth.
    spooled = spool_dir() / f"{key.removeprefix('sha256:')}.pdf"
    spooled.write_bytes(pdf)

    async def run() -> str:
        def _progress(done: int, total: int) -> None:
            # The job handle is bound by the time any page finishes.
            jobs.report(handle.job_id, f"extracting page {done}/{total}")

        try:
            data = await asyncio.to_thread(spooled.read_bytes)
            bundle = await build_bundle(
                data,
                filename=filename,
                store=store,
                marker=marker_client(),
                max_pages=cfg.marker_max_pages,
                ttl_hours=cfg.artifact_ttl_hours,
                on_progress=_progress,
            )
            return bundle.bundle_id
        finally:
            # Cleared whether the extraction succeeded or failed: a spool that
            # only empties on success fills up on exactly the documents that
            # gave trouble.
            spooled.unlink(missing_ok=True)

    # Keyed by content, so two callers uploading the same paper join one job
    # rather than queueing two identical GPU runs.
    handle = jobs.submit(content_key=key, run=run)
    job = handle
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
