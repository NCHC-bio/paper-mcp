"""How the Marker service handles a request's scratch file and its CPU work.

Loaded by path for the same reason as `test_marker_llm_config`: `app.py`
imports `marker` at module scope and the paper-mcp venv does not carry it.
Both concerns tested here are deliberately free of those imports, because
both were defects that `app.py` could not be tested for at all.
"""
from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path
from types import ModuleType

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "marker_service" / "pipeline.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("marker_pipeline", _SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PDF = b"%PDF-1.7\nnot a real document, but real bytes\n"


async def test_the_scratch_file_is_deleted_after_a_successful_build() -> None:
    """Every extraction left a full copy of the PDF in /tmp, forever.

    `NamedTemporaryFile(delete=False)` with no `unlink` — and paper-mcp POSTs
    the *whole* PDF once per page, so the leak is pages x filesize per
    extraction. A 40-page 67 MB paper (the largest in the corpus the upload
    cap was sized from) leaves 2.7 GB behind, against a `/tmp` tmpfs sized
    2 GB in the hardened compose runtime. One paper fills the sandbox and
    every later extraction fails until the container is recreated.
    """
    pipeline = _load()
    seen: dict[str, object] = {}

    def build(path: str) -> dict[str, str]:
        seen["path"] = path
        seen["bytes"] = Path(path).read_bytes()
        return {"ok": "yes"}

    result = await pipeline.extract_document(_PDF, build=build)

    assert result == {"ok": "yes"}
    assert seen["bytes"] == _PDF, "the build must see the exact uploaded bytes"
    assert not Path(str(seen["path"])).exists(), "the scratch file was left behind"


async def test_the_scratch_file_is_deleted_when_the_build_raises() -> None:
    """A failed extraction must not be the one that keeps its copy.

    The documents that make Marker fall over are exactly the ones a caller
    retries, so a cleanup that only runs on success leaks fastest on the
    worst input.
    """
    pipeline = _load()
    seen: dict[str, str] = {}

    def build(path: str) -> dict[str, str]:
        seen["path"] = path
        raise RuntimeError("marker fell over on this page")

    with pytest.raises(RuntimeError, match="fell over"):
        await pipeline.extract_document(_PDF, build=build)

    assert not Path(seen["path"]).exists(), "the scratch file survived a failed build"


async def test_the_build_runs_off_the_event_loop() -> None:
    """`/health` must keep answering while `/extract` is working.

    `build_document` is synchronous CPU/GPU work and the endpoint was an
    `async def`, so it held the event loop for the whole extraction. Measured
    against the real client: `MarkerClient.healthy()` returned True idle and
    False (5266 ms, its 5 s timeout) mid-extract — so paper-mcp's `/health`
    reports its required dependency as down for the entire time that
    dependency is doing its job.

    The same stall makes `MarkerClient.profile()` return `{}`, and with
    `PAPER_MCP_JOB_CONCURRENCY` above 1 that empty profile is what gets baked
    into a cached bundle: `llm_model: null`, `text_source: null`, permanently,
    for those bytes. That is exactly the silent degradation the profile stamp
    exists to make visible.
    """
    pipeline = _load()
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_build(path: str) -> dict[str, str]:
        time.sleep(0.3)
        return {"ok": "yes"}

    beat = asyncio.create_task(heartbeat())
    try:
        await pipeline.extract_document(_PDF, build=slow_build)
    finally:
        beat.cancel()

    assert ticks > 5, f"the event loop was blocked during the build (only {ticks} ticks)"


async def test_two_extractions_never_render_at_the_same_time() -> None:
    """Moving the build off the loop must not accidentally parallelize it.

    The endpoint was an `async def` doing synchronous work, so concurrent
    requests were serialized by the event loop itself — accidentally, but
    load-bearingly: VRAM scales with page content density and a second
    concurrent dense page OOMs a 6 GB card, which is the entire reason
    paper-mcp defaults `PAPER_MCP_JOB_CONCURRENCY` to 1.

    Handing the work to a threadpool would run up to 40 of them at once and
    take the GPU with it. Freeing the loop for `/health` is the goal; letting
    renders overlap is not.
    """
    pipeline = _load()
    live = 0
    peak = 0

    def build(path: str) -> dict[str, str]:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        time.sleep(0.05)
        live -= 1
        return {"ok": "yes"}

    await asyncio.gather(*(pipeline.extract_document(_PDF, build=build) for _ in range(4)))

    assert peak == 1, f"{peak} renders overlapped; VRAM is sized for one"


async def test_request_scratch_goes_where_it_is_told_not_where_tmpdir_points(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tmpfs and the model staging area cannot be the same directory.

    Observed on a cold volume, which is what every fresh deployment has:
    surya's `download_directory` stages an entire model in
    `tempfile.TemporaryDirectory()` before moving it to `MODEL_CACHE_DIR`, so
    the download lands wherever `TMPDIR` points. Under the hardened runtime
    that is the 2 GB `/tmp` tmpfs — measured at 1.2 GB of a 2.0 GB tmpfs for a
    single 1.35 GB model, while `/models` still held 12 KB. Several models
    download on first use, so a larger one exhausts it, the first extraction
    fails with ENOSPC, and `/health` reports ok throughout.

    Pointing `TMPDIR` at the persistent volume fixes the staging. It must not
    drag the request scratch file along with it: caller-supplied bytes are
    what the sized tmpfs exists to contain, and that containment is the whole
    reason the limit is there. So the two are named separately.
    """
    pipeline = _load()
    elsewhere = tmp_path / "tmpdir"
    scratch = tmp_path / "scratch"
    elsewhere.mkdir()
    scratch.mkdir()

    monkeypatch.setenv("TMPDIR", str(elsewhere))
    monkeypatch.setenv("TEMP", str(elsewhere))
    monkeypatch.setenv("TMP", str(elsewhere))
    monkeypatch.setenv("MARKER_SCRATCH_DIR", str(scratch))

    seen: dict[str, str] = {}

    def build(path: str) -> dict[str, str]:
        seen["path"] = path
        return {"ok": "yes"}

    await pipeline.extract_document(_PDF, build=build)

    assert Path(seen["path"]).parent == scratch, (
        "the request scratch file followed TMPDIR into the model staging area"
    )
    assert not list(elsewhere.iterdir()), "nothing of ours belongs in the staging area"
