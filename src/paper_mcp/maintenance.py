"""Periodic reclamation of what an extraction leaves behind.

Both sweeps this schedules already existed — `ArtifactStore.sweep` and
`JobStore.sweep`, each written and each unit-tested — and neither had a
caller anywhere in `src/`. The lifespan cleared the spool at boot and nothing
else ever ran. The effect was not a slow leak but a documented feature that
did not exist: `PAPER_MCP_ARTIFACT_TTL_HOURS` is described as "how long
artifacts survive before the sweeper reclaims them", every bundle carries an
`expires_at`, and nothing expired. A long-lived deployment keeps every paper
anyone ever uploaded, and the in-memory job registry keeps a record of every
extraction it has ever run.

So this module is mostly wiring, and the wiring is the point.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from paper_mcp.config import settings

logger = logging.getLogger(__name__)

# Hourly is plenty for the 24 h default, and sweeping walks every entry on
# disk — there is no reason to do it more often than the TTL needs.
_MAX_INTERVAL_SECONDS = 3600.0
# Never a hot loop, however the TTL is configured. `PAPER_MCP_ARTIFACT_TTL_HOURS=0`
# is a legitimate way to say "keep nothing", not an instruction to spin.
_MIN_INTERVAL_SECONDS = 60.0
# Sweep several times per TTL so an entry's real lifetime stays close to the
# configured one rather than up to double it.
_SWEEPS_PER_TTL = 8


class Swept(NamedTuple):
    artifacts: int
    jobs: int


def sweep_interval_seconds(ttl_hours: float) -> float:
    """How often to sweep, given the configured artifact TTL.

    Derived rather than configured: one more environment variable to explain
    buys nothing an operator would want to tune independently of the TTL it
    follows. A fixed hourly interval would let a one-hour TTL keep artifacts
    for nearly two.
    """
    return max(
        _MIN_INTERVAL_SECONDS,
        min(_MAX_INTERVAL_SECONDS, ttl_hours * 3600.0 / _SWEEPS_PER_TTL),
    )


async def sweep_once() -> Swept:
    """Reclaim expired artifacts and forgotten job handles, once."""
    # Imported here rather than at module scope: `tools.extract` owns the
    # process-level stores, and importing it eagerly would make this module
    # part of a cycle with the tool that imports the config.
    from paper_mcp.tools.extract import artifact_store, job_store

    ttl = settings().artifact_ttl_hours
    # Walking the store stats every file in every entry, which is disk work,
    # not CPU work — off the loop so a large cache cannot stall the service
    # it is being swept for.
    artifacts = await asyncio.to_thread(artifact_store().sweep, ttl)
    jobs = job_store().sweep()
    if artifacts or jobs:
        logger.info("sweep reclaimed %d artifact(s) and %d job record(s)", artifacts, jobs)
    return Swept(artifacts, jobs)


async def sweeper(
    interval_seconds: float,
    *,
    sweep: Callable[[], Awaitable[Swept]] = sweep_once,
) -> None:
    """Sweep forever, every `interval_seconds`. Cancelled at shutdown.

    A failing sweep is logged and the loop continues. Letting one locked file
    end the task would leave a service that looks configured for reclamation
    and quietly stops doing it — which fills the disk exactly as fast as no
    sweeper at all, while being harder to notice.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("sweep failed; continuing", exc_info=True)
