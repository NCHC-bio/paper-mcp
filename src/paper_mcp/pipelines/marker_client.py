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

import logging
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

    async def _post(self, pdf_bytes: bytes, page_range: list[int] | None) -> MarkerDoc:
        data = (
            {"page_range": ",".join(str(i) for i in page_range)}
            if page_range is not None
            else None
        )
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            resp = await client.post(
                f"{self._base_url}/extract",
                files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
                data=data,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Marker is unreachable at {self._base_url}: {type(exc).__name__}. "
                "PDF extraction requires it; there is no fallback engine.",
            ) from exc
        finally:
            if self._client is None:
                await client.aclose()

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
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        try:
            resp = await client.get(f"{self._base_url}/health")
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            # Never fatal: a missing stamp is a lesser loss than a failed
            # extraction the caller waited GPU-minutes for.
            return {}
        finally:
            if self._client is None:
                await client.aclose()
        return body if isinstance(body, dict) else {}

    async def healthy(self) -> bool:
        """Whether Marker is reachable, for `/health` and pre-flight checks."""
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        try:
            resp = await client.get(f"{self._base_url}/health")
        except httpx.HTTPError:
            return False
        finally:
            if self._client is None:
                await client.aclose()
        return resp.status_code == 200
