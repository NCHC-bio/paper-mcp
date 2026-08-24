"""Reclaiming what the service leaves behind.

`ArtifactStore.sweep` and `JobStore.sweep` were both written, both tested,
and both dead: an AST scan of `src/` found no caller for either outside this
suite, and the lifespan only cleared the spool. So
`PAPER_MCP_ARTIFACT_TTL_HOURS` — documented as "how long artifacts survive
before the sweeper reclaims them" — did nothing, the `expires_at` stamped
into every bundle was a promise nothing kept, and the cache grew until the
disk did not. The job registry grew the same way, in memory.

The test that matters most here is the last one: a sweeper nothing calls is
the bug, so something has to assert that the server calls it.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import paper_mcp.maintenance as maintenance_mod
import paper_mcp.tools.extract as extract_mod
from paper_mcp.artifacts import ArtifactStore
from paper_mcp.jobs import JobStore


def _aged_entry(store: ArtifactStore, key: str, *, hours_old: float) -> Path:
    entry = store.ensure(key)
    (entry / "bundle.json").write_text("{}", encoding="utf-8")
    old = time.time() - hours_old * 3600
    for path in entry.rglob("*"):
        import os

        os.utime(path, (old, old))
    return entry


async def test_a_sweep_reclaims_expired_artifacts_and_finished_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    stale = _aged_entry(store, "sha256:stale", hours_old=48)
    fresh = _aged_entry(store, "sha256:fresh", hours_old=1)

    jobs = JobStore(ttl_seconds=-1.0)  # anything finished is instantly stale

    async def quick() -> str:
        return "k"

    done = jobs.submit(content_key="sha256:done", run=quick)
    for _ in range(50):
        await asyncio.sleep(0)

    monkeypatch.setattr(extract_mod, "_store", store)
    monkeypatch.setattr(extract_mod, "_jobs", jobs)
    monkeypatch.setenv("PAPER_MCP_ARTIFACT_TTL_HOURS", "24")

    swept = await maintenance_mod.sweep_once()

    assert swept.artifacts == 1
    assert swept.jobs == 1
    assert not stale.exists(), "an artifact past its TTL survived the sweep"
    assert fresh.exists(), "an artifact inside its TTL was reclaimed"
    assert jobs.get(done.job_id) is None


def test_the_sweep_interval_tracks_the_configured_ttl() -> None:
    """A fixed interval either wastes work or overshoots a short TTL.

    An hourly sweep is right for the 24 h default and wrong for a one-hour
    TTL, where it would let artifacts live nearly twice as long as configured.
    """
    hour = maintenance_mod.sweep_interval_seconds(24.0)
    short = maintenance_mod.sweep_interval_seconds(1.0)

    assert hour == 3600.0, "a long TTL should not sweep more than hourly"
    assert short < 3600.0, "a one-hour TTL needs a sweep inside the hour"
    assert maintenance_mod.sweep_interval_seconds(0.0) >= 60.0, "never a hot loop"


async def test_the_sweeper_keeps_running_until_it_is_cancelled() -> None:
    calls = 0

    async def _count() -> maintenance_mod.Swept:
        nonlocal calls
        calls += 1
        return maintenance_mod.Swept(0, 0)

    task = asyncio.create_task(maintenance_mod.sweeper(0.01, sweep=_count))
    await asyncio.sleep(0.08)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls >= 2, f"the sweeper ran {calls} time(s); it must repeat"


async def test_a_failing_sweep_does_not_kill_the_sweeper() -> None:
    """One bad entry must not silently end all future reclamation.

    A sweeper that dies on the first `OSError` is worse than none: it looks
    configured and stops working, which is how the disk fills anyway.
    """
    calls = 0

    async def _explode() -> maintenance_mod.Swept:
        nonlocal calls
        calls += 1
        raise OSError("a file was locked")

    task = asyncio.create_task(maintenance_mod.sweeper(0.01, sweep=_explode))
    await asyncio.sleep(0.08)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls >= 2, "the sweeper stopped after a failing sweep"


def test_the_server_actually_starts_a_sweeper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect was never the sweep — it was that nothing ran it.

    Both sweep methods existed and were unit-tested while no code path in
    `src/` called either. This asserts the wiring, which is the part that was
    missing.
    """
    from fastapi.testclient import TestClient

    from paper_mcp.server import create_app

    started: list[float] = []
    real = maintenance_mod.sweeper

    async def _spy(interval: float, **kwargs: object) -> None:
        started.append(interval)
        await real(interval, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(maintenance_mod, "sweeper", _spy)
    monkeypatch.setenv("PAPER_MCP_ALLOWED_HOSTS", "testserver")

    with TestClient(create_app()) as client:
        client.get("/health")

    assert started, "the server started no sweeper; artifacts would never expire"
