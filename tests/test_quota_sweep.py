"""Bucket eviction.

`_buckets` grew one entry per (subject_hash, resource) and nothing ever
removed them; `maintenance` did not mention quota at all. In open mode the
key is the caller's IP, so a public endpoint leaked an entry per distinct
caller for the life of the process — on exactly the deployment shape the
quota exists to protect.
"""
from __future__ import annotations

import pytest

from paper_mcp.quota import QuotaExceededError, QuotaLimits, QuotaStore


def test_a_refilled_bucket_is_evicted() -> None:
    """A full bucket is indistinguishable from one that never existed."""
    store = QuotaStore(QuotaLimits(calls_per_minute=60, extractions_per_hour=20))
    store.consume("caller-a", "calls", now=0.0)

    # A minute later the bucket has refilled to capacity.
    assert store.evict_full(now=60.0) == 1
    assert store._buckets == {}


def test_a_bucket_still_in_debt_survives() -> None:
    """Evicting a partly-spent bucket would forgive the spend."""
    store = QuotaStore(QuotaLimits(calls_per_minute=60, extractions_per_hour=20))
    for _ in range(30):
        store.consume("caller-a", "calls", now=0.0)

    assert store.evict_full(now=1.0) == 0
    assert len(store._buckets) == 1


def test_eviction_changes_no_behaviour() -> None:
    """A recreated bucket starts full, which is what the evicted one held."""
    store = QuotaStore(QuotaLimits(calls_per_minute=2, extractions_per_hour=20))
    store.consume("caller-a", "calls", now=0.0)
    store.evict_full(now=600.0)

    # Two more calls fit, exactly as they would have without the eviction.
    store.consume("caller-a", "calls", now=600.0)
    store.consume("caller-a", "calls", now=600.0)
    with pytest.raises(QuotaExceededError):
        store.consume("caller-a", "calls", now=600.0)


def test_many_distinct_callers_do_not_accumulate() -> None:
    """The open-mode leak: one entry per IP, for the life of the process."""
    store = QuotaStore(QuotaLimits(calls_per_minute=60, extractions_per_hour=20))
    for i in range(500):
        store.consume(f"ip-{i}", "calls", now=0.0)

    assert len(store._buckets) == 500
    assert store.evict_full(now=60.0) == 500
    assert store._buckets == {}


async def test_the_sweeper_evicts_quota_buckets() -> None:
    """The maintenance sweep is the caller quota never had.

    The bucket is charged two minutes in the past so it has refilled by the
    time the sweep runs; `sweep_once` reads the real clock.
    """
    import time

    from paper_mcp import maintenance
    from paper_mcp.quota import quota_store

    quota_store().consume("caller-a", "calls", now=time.monotonic() - 120.0)
    swept = await maintenance.sweep_once()

    assert swept.quota == 1, "the sweep did not reclaim the refilled bucket"


async def test_the_sweeper_spares_a_caller_still_in_debt() -> None:
    """A caller who just burned their budget keeps it burned across a sweep."""
    from paper_mcp import maintenance
    from paper_mcp.quota import quota_store

    store = quota_store()
    for _ in range(30):
        store.consume("caller-b", "calls")
    swept = await maintenance.sweep_once()

    assert swept.quota == 0
    assert ("caller-b", "calls") in store._buckets
