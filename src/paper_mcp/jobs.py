"""Background jobs for work that cannot fit in a tool call.

Marker is slow by nature — PaperHub measured 21 minutes for a 5-page dense
batch — so a PDF extraction cannot be an inline tool call. It would exceed any
MCP client's request timeout, and the caller would get a dropped connection
instead of an answer (a failure this project has already paid for once).

Two properties matter beyond "run it later":

* **Coalescing by content key.** Two callers asking for the same uncached
  paper join one job. Without this, N callers start N GPU extractions of the
  same PDF and starve each other.
* **Serialized execution.** Marker work runs one at a time. VRAM scales with
  page content density, and on a 6 GB card concurrency means OOM rather than
  throughput.

Jobs are in-memory and ephemeral. A restart forgets outstanding handles, which
is survivable precisely because results are content-addressed: calling
`extract_pdf` again returns the finished bundle from cache rather than
recomputing it.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

JobState = Literal["queued", "running", "done", "error"]


class JobStatus(BaseModel):
    job_id: str
    state: JobState
    content_key: str
    progress: str = ""
    result_key: str | None = Field(
        default=None,
        description="Content key of the finished artifact; fetch it to get the result.",
    )
    error: str | None = None


class JobStore:
    """In-process job registry with coalescing and serialized execution."""

    def __init__(self, *, concurrency: int = 1, ttl_seconds: float = 3600.0) -> None:
        # One slot by default: Marker is the workload, and it does not
        # parallelize on a single small GPU.
        self._semaphore = asyncio.Semaphore(concurrency)
        self._ttl = ttl_seconds
        self._jobs: dict[str, JobStatus] = {}
        self._by_key: dict[str, str] = {}
        self._finished_at: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def get(self, job_id: str) -> JobStatus | None:
        return self._jobs.get(job_id)

    def for_key(self, content_key: str) -> JobStatus | None:
        job_id = self._by_key.get(content_key)
        return self._jobs.get(job_id) if job_id else None

    def submit(
        self,
        *,
        content_key: str,
        run: Callable[[], Awaitable[str]],
    ) -> JobStatus:
        """Start (or join) a job producing the artifact for `content_key`.

        `run` returns the content key of what it produced. An in-flight job for
        the same key is returned as-is rather than duplicated.
        """
        existing = self.for_key(content_key)
        if existing is not None and existing.state in ("queued", "running"):
            logger.debug("joining in-flight job %s for %s", existing.job_id, content_key)
            return existing
        if existing is not None and existing.state == "error":
            # Hand the failure back rather than silently starting over. The
            # caller was told to "call extract_pdf again", so re-queueing here
            # loops forever and the error is only ever visible through
            # get_job, which that advice never mentions.
            #
            # Released immediately after, so the next call may retry: a
            # transient cause — Marker restarting mid-extraction — must not
            # become permanent for those bytes.
            logger.debug("reporting failed job %s for %s", existing.job_id, content_key)
            self._by_key.pop(content_key, None)
            return existing

        ahead = sum(1 for j in self._jobs.values() if j.state in ("queued", "running"))
        job = JobStatus(
            job_id=secrets.token_urlsafe(16),
            state="queued",
            content_key=content_key,
            # Depth, not just the word "queued". Work is serialized, so a
            # caller that cannot see how much is in front of it cannot tell a
            # busy service from a dead one — measured at 2,268 s of silence on
            # a real run, with the GPU reading 0% because the accuracy pass is
            # network-bound, which looks exactly like a hang.
            progress=f"queued ({ahead} ahead)" if ahead else "queued (next)",
        )
        self._jobs[job.job_id] = job
        self._by_key[content_key] = job.job_id

        task = asyncio.create_task(self._execute(job, run))
        # Hold a reference: a task only weakly referenced can be garbage
        # collected mid-flight, which loses the work silently.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def report(self, job_id: str, progress: str) -> None:
        """Record how far a running job has got.

        Called from inside the work, because only the work knows. `extracting`
        held for eighteen minutes on a real paper says nothing; pages are the
        unit the estimate in `extract_pdf`'s own hint is quoted in.
        """
        job = self._jobs.get(job_id)
        if job is not None and job.state == "running":
            job.progress = progress

    async def _execute(self, job: JobStatus, run: Callable[[], Awaitable[str]]) -> None:
        async with self._semaphore:
            job.state = "running"
            job.progress = "extracting"
            try:
                job.result_key = await run()
                job.state = "done"
                job.progress = "complete"
            except Exception as exc:  # a failed job must report, not vanish
                job.state = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                logger.warning("job %s failed: %s", job.job_id, job.error)
            finally:
                self._finished_at[job.job_id] = time.monotonic()

    def sweep(self) -> int:
        """Drop finished jobs past their TTL; in-flight jobs are never swept."""
        cutoff = time.monotonic() - self._ttl
        stale = [
            job_id
            for job_id, finished in self._finished_at.items()
            if finished < cutoff and self._jobs.get(job_id, None) is not None
        ]
        for job_id in stale:
            job = self._jobs.pop(job_id, None)
            self._finished_at.pop(job_id, None)
            if job and self._by_key.get(job.content_key) == job_id:
                self._by_key.pop(job.content_key, None)
        return len(stale)
