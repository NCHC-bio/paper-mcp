from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

import paper_mcp.tools.extract as extract_mod
from paper_mcp.artifacts import ArtifactStore
from paper_mcp.models import InvalidArgumentError
from paper_mcp.tools.extract import decode_pdf


def _real_pdf(pages: int = 1) -> bytes:
    """A PDF that actually opens.

    The old fixture was a `%PDF-` header followed by prose. Validation now
    opens the document, so a fake one is correctly refused — and a test built
    on a fake was only ever asserting that we did not look.
    """
    import pymupdf

    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page().insert_text((72, 72), "results table and an equation")
    data: bytes = doc.tobytes()
    doc.close()
    return data


_PDF = _real_pdf()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_a_valid_pdf_decodes_to_its_bytes() -> None:
    assert decode_pdf(_b64(_PDF), max_bytes=1024) == _PDF


def test_malformed_base64_is_rejected_at_the_boundary() -> None:
    # Each rejection names a different mistake, because a caller can only fix
    # what it can tell apart. Marker would surface all of them as the same
    # opaque failure several GPU-minutes later.
    with pytest.raises(InvalidArgumentError, match="base64"):
        decode_pdf("not!valid!base64!", max_bytes=1024)


def test_bytes_that_are_not_a_pdf_are_rejected() -> None:
    with pytest.raises(InvalidArgumentError, match="not a PDF"):
        decode_pdf(_b64(b"PK\x03\x04 this is a zip"), max_bytes=1024)


def test_an_empty_payload_is_rejected() -> None:
    with pytest.raises(InvalidArgumentError, match="zero bytes"):
        decode_pdf("", max_bytes=1024)


def test_an_oversize_pdf_is_refused_before_the_decoder_sees_it() -> None:
    # Bounding what reaches the decoder is part of containment (SRS NFR-02),
    # not politeness: a file whose only purpose is to exhaust memory should
    # never reach `pillow` at all.
    oversize = _real_pdf()

    with pytest.raises(InvalidArgumentError, match="over the"):
        decode_pdf(_b64(oversize), max_bytes=len(oversize) - 1)


def test_the_size_limit_is_measured_on_decoded_bytes_not_the_encoding() -> None:
    # base64 inflates by ~33%. Measuring the encoded form would reject files
    # comfortably inside the limit the operator configured.
    data = _real_pdf()
    assert len(_b64(data)) > len(data)

    assert decode_pdf(_b64(data), max_bytes=len(data)) == data


async def test_a_failed_extraction_is_reported_not_dressed_as_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`status: extracting` on a job that already failed is a lie.

    The store now hands a failed job back, but the tool still has to tell the
    caller. Returning "extracting" with a dead job is what made the loop
    invisible: the hint said call again, the status said in progress, and the
    error lived somewhere the hint never mentioned.
    """
    from paper_mcp.jobs import JobStatus
    from paper_mcp.models import UpstreamError

    class _Store:
        def submit(self, *, content_key: str, run: object) -> JobStatus:
            return JobStatus(
                job_id="dead", state="error", content_key=content_key,
                error="Marker returned HTTP 500",
            )

    monkeypatch.setattr(extract_mod, "job_store", lambda: _Store())
    monkeypatch.setattr(extract_mod, "artifact_store", lambda: ArtifactStore(tmp_path))

    with pytest.raises(UpstreamError, match="HTTP 500"):
        await extract_mod.tool_extract_pdf(_b64(_PDF))


def test_a_pdf_that_cannot_be_opened_is_refused_at_the_boundary() -> None:
    """A `%PDF-` header is not proof the file is readable.

    A 15-byte stub passed validation, took the single GPU slot, and failed
    minutes later as `UpstreamError: Marker returned HTTP 500` — which tells
    a caller the service is broken and to retry, when the truth is the file
    is unreadable and retrying will burn the slot again. Opening it here
    costs microseconds and turns a misleading 5xx into an answerable error.
    """
    stub = b"%PDF-1.4\n%%EOF\n"

    with pytest.raises(InvalidArgumentError, match="could not be opened"):
        decode_pdf(_b64(stub), max_bytes=1024)


def test_a_truncated_pdf_is_refused_rather_than_queued() -> None:
    # Truncation is the common real case — an interrupted download, a partial
    # upload. It parses far enough to look like a PDF and fails deep inside
    # extraction, twenty minutes later.
    truncated = b"%PDF-1.7\n" + b"0" * 4000

    with pytest.raises(InvalidArgumentError, match="could not be opened"):
        decode_pdf(_b64(truncated), max_bytes=1024 * 1024)


def test_job_concurrency_comes_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker count must be an operator decision on a shared endpoint.

    `JobStore` has always taken a `concurrency` argument; nothing passed one,
    so every deployment ran a single worker no matter the hardware. On a
    shared service that is also a fairness ceiling: one caller's queue of
    papers stalls everyone else's first page.
    """
    monkeypatch.setenv("PAPER_MCP_JOB_CONCURRENCY", "3")
    monkeypatch.setattr(extract_mod, "_jobs", None)

    assert extract_mod.job_store()._semaphore._value == 3


def test_job_concurrency_defaults_to_one_because_vram_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dense two-column page can saturate 6 GB on its own, so the safe
    # default stays 1 — raising it is a claim about the card, not a
    # preference.
    monkeypatch.delenv("PAPER_MCP_JOB_CONCURRENCY", raising=False)
    monkeypatch.setattr(extract_mod, "_jobs", None)

    assert extract_mod.job_store()._semaphore._value == 1


async def test_queued_bytes_are_spooled_to_disk_not_held_in_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A queue of large papers must not be a queue of large buffers.

    The job closed over the uploaded bytes, so every queued extraction pinned
    its whole PDF until a single worker reached it. With the cap at 100 MB
    and a real library whose median paper is 10.6 MB, ten queued uploads held
    hundreds of megabytes for no reason other than waiting. Spooling to disk
    keeps the cost at one in-flight document.
    """
    captured: dict[str, object] = {}

    async def _fake_build(pdf: bytes, **kwargs: object) -> object:
        captured["seen_bytes"] = len(pdf)
        raise RuntimeError("stop after reading")

    monkeypatch.setattr(extract_mod, "build_bundle", _fake_build)
    monkeypatch.setattr(extract_mod, "artifact_store", lambda: ArtifactStore(tmp_path))
    monkeypatch.setattr(extract_mod, "_jobs", None)
    # Isolate the spool: it is a real directory beside the artifact root, so
    # a leftover from any earlier run would answer this test's glob.
    monkeypatch.setenv("PAPER_MCP_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    pdf = _real_pdf()
    result = await extract_mod.tool_extract_pdf(_b64(pdf))
    assert result.status == "extracting"

    spool = extract_mod.spool_dir()
    assert list(spool.glob("*.pdf")), "the upload should be on disk while queued"

    # The read happens on a worker thread, so this needs real time.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not list(spool.glob("*.pdf")):
            break

    # The work still receives the exact bytes, read back from the spool.
    assert captured["seen_bytes"] == len(pdf)
    # And the spool is cleaned up once the job is done with it.
    assert not list(spool.glob("*.pdf")), "the spooled upload was left behind"


def test_orphaned_spool_files_are_cleared_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every spool file present at boot is garbage, by construction.

    Jobs live in memory, so a restart forgets them — but the uploads they were
    waiting on stay on disk. A service restarted mid-extraction (a crash, a
    redeploy) therefore leaks its largest files, and with a 100 MB ceiling
    that is how a long-lived deployment fills its disk with documents nobody
    is waiting for any more.
    """
    monkeypatch.setenv("PAPER_MCP_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    spool = extract_mod.spool_dir()
    orphan = spool / "deadbeef.pdf"
    orphan.write_bytes(b"%PDF-1.7 left behind by a restart")

    removed = extract_mod.clear_spool()

    assert removed == 1
    assert not orphan.exists()
