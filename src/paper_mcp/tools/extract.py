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
import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from paper_mcp.artifacts import ArtifactStore
from paper_mcp.bundle import Bundle
from paper_mcp.config import settings
from paper_mcp.context import current_principal
from paper_mcp.jobs import JobQueueFullError, JobStatus, JobStore
from paper_mcp.models import (
    InvalidArgumentError,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
)
from paper_mcp.pipelines.build_bundle import build_bundle, bundle_key, load_cached
from paper_mcp.pipelines.marker_client import MarkerClient, page_count
from paper_mcp.quota import QuotaExceededError, quota_store

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
        cfg = settings()
        _jobs = JobStore(concurrency=cfg.job_concurrency, max_queued=cfg.max_queued_jobs)
    return _jobs


def spool_dir() -> Path:
    """Where uploads wait on disk between acceptance and extraction.

    Beside the artifact cache by default rather than inside it: these are the
    caller's original documents, and nothing here is ever served over HTTP.
    Overridable because the derived path was not on a volume — with the
    shipped /app/artifacts it landed on /app/spool, the container's writable
    layer, so a backlog filled the Docker root disk instead of the volume
    that was sized for this data.
    """
    cfg = settings()
    path = cfg.spool_root or cfg.artifact_root.parent / "spool"
    path.mkdir(parents=True, exist_ok=True)
    return path


def spool_path(content_key: str) -> Path:
    """A fresh file for one upload of `content_key`.

    Unique per call, deliberately, even though the bytes are not. The path
    was `<content-sha>.pdf`, which made two callers uploading the same paper
    name the same file — and `write_bytes` truncates on open, so one call's
    write landed inside the other's read. A live 8-way run produced exactly
    that: the running job read zero bytes and the caller was told its PDF was
    "truncated or corrupt" and that a retry would not help, about a document
    that was perfectly fine.

    Content addressing is right for the artifact cache, where an entry is
    immutable and shared on purpose. The spool is the opposite: a mutable
    file with one reader and a lifetime of a single job.
    """
    return spool_dir() / f"{content_key.removeprefix('sha256:')}-{secrets.token_hex(8)}.pdf"


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


def charge_extraction() -> None:
    """Spend one unit of the caller's GPU budget, or refuse the extraction.

    Called only on a cache miss, because the budget meters GPU minutes and a
    cache hit costs none. Charging every call would turn
    `PAPER_MCP_QUOTA_EXTRACTIONS_PER_HOUR` into a second call limit and would
    punish the exact pattern `extract_pdf`'s own hint recommends — call again
    until the cache is warm.

    Nothing charged this before: the middleware consumes `"calls"` and only
    `"calls"`, so the setting was documented, configurable, and inert. With
    one worker by default, an unmetered caller is one who can hold the queue
    against everyone else indefinitely.

    No principal means no request context — a direct call, or a test. Not
    metered, rather than metered against a key that means nothing.
    """
    principal = current_principal()
    if principal is None:
        return
    try:
        quota_store().consume(principal.subject_hash, "extractions")
    except QuotaExceededError as exc:
        raise RateLimitedError(str(exc), retry_after=exc.retry_after) from exc


async def tool_extract_pdf(content_base64: str, filename: str | None = None) -> ExtractResult:
    """Extract a caller-supplied PDF into markdown plus a figure index."""
    cfg = settings()
    # Off the loop. A 100 MB base64 decode plus a PyMuPDF open is tens to
    # hundreds of milliseconds of pure CPU, and it ran inline: ten 86 MB
    # uploads accumulated 11.0 s of event-loop stall in a 28.4 s window, with
    # 8 of 38 concurrent /health probes taking over a second.
    pdf = await asyncio.to_thread(
        decode_pdf, content_base64, max_bytes=cfg.max_upload_bytes
    )

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

    # Charged for starting work, not for asking. A cache miss is not the same
    # as a GPU minute: `extract_pdf`'s own hint tells a caller to call again
    # until the cache is warm, and every one of those calls misses until the
    # job finishes — so billing the miss would bill a caller repeatedly for
    # one extraction and burn an hour's allowance in seconds. Callers that
    # coalesce onto one job are one GPU run between them, and a call handed a
    # failure to report starts nothing at all.
    #
    # `submit` decides this from the same incumbent, and does not await, so
    # reading it here cannot disagree with what happens below.
    incumbent = jobs.for_key(key)
    starting_work = incumbent is None or incumbent.state == "done"
    if starting_work:
        charge_extraction()

    # Spooled rather than closed over. A queued job that holds its upload in
    # memory turns a queue of large papers into a queue of large buffers: with
    # a 100 MB ceiling and a single worker, ten waiting uploads pinned
    # hundreds of megabytes for no reason but waiting. On disk, the cost is
    # one in-flight document regardless of queue depth.
    #
    # Written only when this call will actually start a job, and written off
    # the loop. Joining an in-flight job wrote the entire upload and unlinked
    # it a few lines later — up to 100 MB of synchronous disk write per poll,
    # for a file that never had a reader.
    spooled = spool_path(key)
    if starting_work:
        await asyncio.to_thread(spooled.write_bytes, pdf)

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
    try:
        handle = jobs.submit(content_key=key, run=run)
    except JobQueueFullError as exc:
        # Nothing will ever read this file: no job adopted it.
        spooled.unlink(missing_ok=True)
        raise RateLimitedError(str(exc), retry_after=exc.retry_after) from exc
    job = handle
    if incumbent is not None and job.job_id == incumbent.job_id:
        # `run` was never adopted: this call joined an in-flight job, or was
        # handed a failed one to report. Only the job that owns a spool file
        # deletes it, so the file written above has no owner and would sit
        # there until a restart — up to 100 MB per orphan, on exactly the
        # papers that are popular or that give trouble.
        #
        # Comparing job ids is safe because `submit` never awaits: nothing
        # else can run between reading the incumbent and getting the answer.
        spooled.unlink(missing_ok=True)
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
