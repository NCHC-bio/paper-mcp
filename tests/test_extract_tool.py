from __future__ import annotations

import base64
from pathlib import Path

import pytest

import paper_mcp.tools.extract as extract_mod
from paper_mcp.artifacts import ArtifactStore
from paper_mcp.models import InvalidArgumentError
from paper_mcp.tools.extract import decode_pdf

_PDF = b"%PDF-1.7\nattention is all you need"


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
    oversize = b"%PDF-1.7" + b"0" * 4096

    with pytest.raises(InvalidArgumentError, match="over the"):
        decode_pdf(_b64(oversize), max_bytes=1024)


def test_the_size_limit_is_measured_on_decoded_bytes_not_the_encoding() -> None:
    # base64 inflates by ~33%. Measuring the encoded form would reject files
    # comfortably inside the limit the operator configured.
    data = b"%PDF-1.7" + b"0" * 900
    assert len(_b64(data)) > 1000

    assert decode_pdf(_b64(data), max_bytes=1000) == data


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
