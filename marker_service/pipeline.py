"""Request plumbing around a Marker extraction: scratch file in, payload out.

Deliberately free of `marker` imports, like `llm_config`, so both of the
defects it exists to fix are testable. The caller injects `build`, which is
where every heavyweight import actually lives.

Two things happen here, and both were bugs in `app.py`:

* **The scratch file is always deleted.** `NamedTemporaryFile(delete=False)`
  with no `unlink` leaked a full copy of the PDF per request — and paper-mcp
  POSTs the whole document once per page, so the cost is pages x filesize per
  extraction. A 40-page 67 MB paper leaves 2.7 GB behind, against the 2 GB
  `/tmp` tmpfs the hardened compose runtime gives this container. One paper
  fills the sandbox; everything after it fails until the container is
  recreated.

* **The build runs off the event loop.** `build_document` is synchronous
  CPU/GPU work, and holding the loop for it stops this service answering
  `/health` while it works. Measured through the real client:
  `MarkerClient.healthy()` went True -> False (5266 ms, its 5 s timeout) the
  moment an extraction started, so paper-mcp reported its required dependency
  as down for the whole time that dependency was busy — and
  `MarkerClient.profile()` returned `{}`, which with concurrency above 1 is
  what gets written into a cached bundle as `llm_model: null`.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from starlette.concurrency import run_in_threadpool


def scratch_dir() -> str | None:
    """Where a request's scratch PDF goes; None means "wherever `TMPDIR` says".

    Named separately from `TMPDIR` on purpose. surya stages an entire model
    download in `tempfile.TemporaryDirectory()` before moving it to
    `MODEL_CACHE_DIR`, so `TMPDIR` has to point at the persistent volume or a
    first extraction on a cold cache fills the tmpfs: measured at 1.2 GB of a
    2.0 GB `/tmp` for one 1.35 GB model, with `/models` still empty, and
    `/health` reporting ok the whole time.

    The request scratch must not follow it there. Caller-supplied bytes are
    exactly what the sized tmpfs exists to contain, so `MARKER_SCRATCH_DIR`
    keeps them in it while model staging goes to disk.
    """
    return os.environ.get("MARKER_SCRATCH_DIR") or None


@contextmanager
def temporary_pdf(data: bytes) -> Iterator[str]:
    """Write `data` to a scratch `.pdf` and delete it however the block ends.

    Marker takes a path, not bytes, which is the only reason this file
    exists. It has to stay closed and named while the converter reopens it,
    so `mkstemp` (rather than a `NamedTemporaryFile` context manager) is what
    fits — and the deletion is therefore ours to own.
    """
    fd, path = tempfile.mkstemp(suffix=".pdf", dir=scratch_dir())
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        yield path
    finally:
        # Suppressed: a build that moved or consumed the file is not a reason
        # to fail a request that otherwise succeeded.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


# One render at a time, restoring on purpose what the old `async def` gave by
# accident. Blocking the event loop serialized concurrent requests as a side
# effect; a threadpool would run up to 40 at once, and VRAM scales with page
# content density — a second concurrent dense page OOMs a 6 GB card, which is
# why paper-mcp defaults its own job concurrency to 1. The loop stays free for
# `/health` either way, which was the point.
_RENDER_SLOT = asyncio.Semaphore(1)


async def extract_document(
    data: bytes, *, build: Callable[[str], dict[str, Any]]
) -> dict[str, Any]:
    """Run `build` over `data` on a worker thread, cleaning up either way."""
    with temporary_pdf(data) as path:
        async with _RENDER_SLOT:
            return await run_in_threadpool(build, path)


__all__ = ["extract_document", "temporary_pdf"]
