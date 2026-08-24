"""Per-caller token buckets.

With no per-user data, one caller cannot read another's anything — but they
share a GPU, a CPU and an outbound API budget, so one caller absolutely can
starve the rest. That is what quota is for (SRS FR-09).

Two resources, because they are scarce in different ways:

* **calls/minute** — cheap and bursty; stops a hot loop.
* **extractions/hour** — GPU minutes, the genuinely expensive one.

A third bucket metered compile-seconds for caller-supplied LaTeX. Compilation
went out of scope in v1.0 and nothing has charged it since.

Buckets live in memory. A restart forgives one window, which is a better
trade than a Redis dependency for a single-host service (SRS §II-6).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

Resource = Literal["calls", "extractions"]


@dataclass
class _Bucket:
    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float

    def take(self, amount: float, now: float) -> float:
        """Consume `amount`; return 0.0 on success or the seconds to wait."""
        elapsed = now - self.updated_at
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now
        if self.tokens >= amount:
            self.tokens -= amount
            return 0.0
        deficit = amount - self.tokens
        return deficit / self.refill_per_second if self.refill_per_second > 0 else float("inf")


@dataclass(frozen=True)
class QuotaLimits:
    calls_per_minute: float = 60.0
    extractions_per_hour: float = 20.0


class QuotaExceededError(Exception):
    def __init__(self, resource: str, retry_after: float) -> None:
        super().__init__(
            f"quota exceeded for {resource}; retry in {retry_after:.0f}s"
        )
        self.resource = resource
        self.retry_after = retry_after


class QuotaStore:
    """Token buckets keyed by `(subject_hash, resource)`."""

    def __init__(self, limits: QuotaLimits | None = None) -> None:
        self.limits = limits or QuotaLimits()
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def _bucket(self, subject_hash: str, resource: Resource, now: float) -> _Bucket:
        key = (subject_hash, resource)
        bucket = self._buckets.get(key)
        if bucket is None:
            capacity, per_second = {
                "calls": (self.limits.calls_per_minute, self.limits.calls_per_minute / 60.0),
                "extractions": (
                    self.limits.extractions_per_hour,
                    self.limits.extractions_per_hour / 3600.0,
                ),
            }[resource]
            # Start full: a caller's first request should not be throttled.
            bucket = _Bucket(capacity, per_second, capacity, now)
            self._buckets[key] = bucket
        return bucket

    def consume(
        self,
        subject_hash: str,
        resource: Resource,
        amount: float = 1.0,
        *,
        now: float | None = None,
    ) -> None:
        """Charge `amount` against a bucket, or raise `QuotaExceededError`."""
        moment = now if now is not None else time.monotonic()
        wait = self._bucket(subject_hash, resource, moment).take(amount, moment)
        if wait > 0:
            logger.info("quota exceeded: %s %s (retry in %.0fs)", subject_hash[:8], resource, wait)
            raise QuotaExceededError(resource, wait)

    def remaining(self, subject_hash: str, resource: Resource) -> float:
        return self._buckets[(subject_hash, resource)].tokens if (
            (subject_hash, resource) in self._buckets
        ) else float("inf")


_store: QuotaStore | None = None


def quota_store() -> QuotaStore:
    global _store
    if _store is None:
        from paper_mcp.config import settings

        cfg = settings()
        _store = QuotaStore(
            QuotaLimits(
                calls_per_minute=cfg.quota_calls_per_minute,
                extractions_per_hour=cfg.quota_extractions_per_hour,
            )
        )
    return _store


def reset_quota_store() -> None:
    global _store
    _store = None
