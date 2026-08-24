"""HTTP client for the Marker extraction service.

# Ported from PaperHub `backend/src/paperhub/pipelines/marker_client.py` @ fd65834.
# Adapted: async (`httpx.AsyncClient`) so a tool call does not block the event
# loop; raises this project's typed errors instead of bare `raise_for_status`;
# the page-batching behaviour is carried over unchanged.

Marker is the extraction engine, not a fallback (SRS v0.2). It exists to turn
a PDF into what an LLM agent can actually use — prose, real tables, equations
as LaTeX, and an extracted figure index with captions.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pymupdf

from paper_mcp.models import UpstreamError

logger = logging.getLogger(__name__)

# A single dense two-column page (200+ OCR text lines) on a 6 GB GPU can take
# many minutes; the read timeout must clear that worst case. This is why PDF
# extraction is a background job rather than an inline tool call.
_TIMEOUT = httpx.Timeout(1800.0, connect=10.0)

# How long a health answer stands. Short enough that an orchestrator still
# sees an outage promptly, long enough that a burst of probes is one round
# trip. `/health` is exempt from both auth and quota, so this is the only
# thing between an anonymous caller and Marker.
_HEALTH_TTL_SECONDS = 5.0


@dataclass
class MarkerBlock:
    block_type: str
    html: str = ""
    latex: str | None = None
    section_hierarchy: dict[str, str] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=dict)  # name -> base64 image
    bbox: list[float] = field(default_factory=list)
    page: int | None = None
    # A figure's caption often lives in a sibling Caption block rather than the
    # figure block's own html; the service pairs them and writes the result
    # here. Captions are half of what makes the figure index useful.
    caption: str | None = None
    # Marker block id, e.g. "/page/2/Figure/0". `section_hierarchy` VALUES are
    # block-id refs to SectionHeader blocks, not names, so the mapper resolves
    # names through a {block_id -> header text} map keyed on this.
    block_id: str | None = None


@dataclass
class MarkerDoc:
    blocks: list[MarkerBlock]


def parse_blocks(payload: dict[str, Any]) -> MarkerDoc:
    return MarkerDoc(
        blocks=[
            MarkerBlock(
                block_type=str(b.get("block_type", "")),
                html=str(b.get("html", "")),
                latex=b.get("latex"),
                section_hierarchy=b.get("section_hierarchy") or {},
                images=b.get("images") or {},
                bbox=b.get("bbox") or [],
                page=b.get("page"),
                caption=b.get("caption"),
                block_id=b.get("block_id"),
            )
            for b in payload.get("blocks", [])
        ]
    )


def page_count(pdf_bytes: bytes) -> int:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        count: int = doc.page_count
    return count


class MarkerClient:
    """Async client for the Marker service.

    `max_pages` splits the PDF into page batches, each POSTed separately.
    VRAM use scales with page CONTENT DENSITY rather than page count, so
    batching is what keeps a dense paper inside a small GPU: PaperHub measured
    a 5-page batch at 21 minutes and found that exceeding one dense page tips
    into the catastrophic CUDA shared-memory fallback. Marker's `page_range`
    preserves absolute page numbers and block ids, so batch results
    concatenate without renumbering.
    """

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._owned: httpx.AsyncClient | None = None
        self._health_cached: tuple[float, bool] | None = None
        self._health_lock: asyncio.Lock | None = None

    def _shared(self) -> httpx.AsyncClient:
        """One client for this instance, not one per call.

        Every method built its own `AsyncClient`, so every call paid a fresh
        TCP connection. On `/health` — unauthenticated, unmetered, and
        reachable by anyone who can route to the port — that made a free
        endpoint an amplifier against Marker.
        """
        if self._client is not None:
            return self._client
        if self._owned is None:
            self._owned = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._owned

    def _health_gate(self) -> asyncio.Lock:
        # Lazily created: a lock binds to the loop that first awaits it.
        if self._health_lock is None:
            self._health_lock = asyncio.Lock()
        return self._health_lock

    async def aclose(self) -> None:
        """Close the client this instance owns. An injected one is the caller's."""
        if self._owned is not None:
            await self._owned.aclose()
            self._owned = None

    async def _post(self, pdf_bytes: bytes, page_range: list[int] | None) -> MarkerDoc:
        data = (
            {"page_range": ",".join(str(i) for i in page_range)}
            if page_range is not None
            else None
        )
        try:
            resp = await self._shared().post(
                f"{self._base_url}/extract",
                files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
                data=data,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Marker is unreachable at {self._base_url}: {type(exc).__name__}. "
                "PDF extraction requires it; there is no fallback engine.",
            ) from exc

        if resp.status_code >= 400:
            raise UpstreamError(
                f"Marker returned HTTP {resp.status_code}: {resp.text[:300]}",
            )
        payload: dict[str, Any] = resp.json()
        return parse_blocks(payload)

    async def extract(
        self,
        pdf_bytes: bytes,
        *,
        max_pages: int | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> MarkerDoc:
        """Extract a document, optionally reporting `(done, total)` pages.

        Progress is per page batch because that is the only point where this
        knows anything: a caller polling a job otherwise sees one opaque
        string for the whole run, which on a 49-page paper was eighteen
        minutes of silence.
        """
        if max_pages is None or max_pages <= 0:
            return await self._post(pdf_bytes, None)

        pages = page_count(pdf_bytes)
        if pages <= 0:
            return await self._post(pdf_bytes, None)

        merged: list[MarkerBlock] = []
        for start in range(0, pages, max_pages):
            indices = list(range(start, min(start + max_pages, pages)))
            logger.debug("marker batch pages %s of %d", indices, pages)
            merged.extend((await self._post(pdf_bytes, indices)).blocks)
            if on_progress is not None:
                on_progress(min(start + max_pages, pages), pages)
        return MarkerDoc(blocks=merged)

    async def profile(self) -> dict[str, Any]:
        """Marker's current extraction configuration, or `{}` if unreachable.

        Read once per extraction, not per cache lookup: it exists to stamp a
        bundle with what produced it, and a bundle is produced once.
        """
        try:
            resp = await self._shared().get(
                f"{self._base_url}/health", timeout=httpx.Timeout(5.0)
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            # Never fatal: a missing stamp is a lesser loss than a failed
            # extraction the caller waited GPU-minutes for.
            return {}
        return body if isinstance(body, dict) else {}

    async def healthy(self) -> bool:
        """Whether Marker is reachable, for `/health` and pre-flight checks.

        Cached and single-flighted. `_OPEN_PREFIXES` exempts `/health` from
        both auth and quota, so without this any anonymous caller turns a
        readiness probe into an amplifier: 300 concurrent requests measured a
        43.6 s median against 1.2 s for the same server with no upstream call.
        """
        cached = self._health_cached
        if cached is not None and time.monotonic() - cached[0] < _HEALTH_TTL_SECONDS:
            return cached[1]

        async with self._health_gate():
            # A concurrent probe may have filled the cache while this one waited.
            cached = self._health_cached
            if cached is not None and time.monotonic() - cached[0] < _HEALTH_TTL_SECONDS:
                return cached[1]
            try:
                resp = await self._shared().get(
                    f"{self._base_url}/health", timeout=httpx.Timeout(5.0)
                )
                ok = resp.status_code == 200
            except httpx.HTTPError:
                ok = False
            self._health_cached = (time.monotonic(), ok)
            return ok
