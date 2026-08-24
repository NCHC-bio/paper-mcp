"""The HTTP client for Marker, and what `/health` costs when it is probed.

`healthy()` built a fresh `AsyncClient` — and so a fresh TCP connection — on
every call, and `/health` is exempt from both auth and quota. That turned a
free endpoint into an amplifier: 300 concurrent anonymous requests measured
66.5 s wall and a 43.6 s median, against 5.0 s and 1.2 s for the same server
with no upstream call.
"""
from __future__ import annotations

import asyncio

import httpx
import respx

from paper_mcp.pipelines.marker_client import MarkerClient

_BASE = "http://marker.test"
_HEALTH = f"{_BASE}/health"


async def test_concurrent_health_probes_cost_one_round_trip() -> None:
    """`/health` is unauthenticated and unmetered, so it must not amplify."""
    calls = 0

    async def counted(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"status": "ok"})

    with respx.mock:
        respx.get(_HEALTH).mock(side_effect=counted)
        client = MarkerClient(_BASE)
        results = await asyncio.gather(*(client.healthy() for _ in range(50)))

    assert all(results)
    assert calls == 1, f"{calls} upstream probes for 50 concurrent health checks"


async def test_a_health_result_is_cached_briefly() -> None:
    """Sequential probes inside the TTL reuse the answer."""
    calls = 0

    async def counted(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "ok"})

    with respx.mock:
        respx.get(_HEALTH).mock(side_effect=counted)
        client = MarkerClient(_BASE)
        for _ in range(20):
            assert await client.healthy() is True

    assert calls == 1, f"{calls} upstream probes for 20 sequential health checks"


async def test_an_outage_is_still_reported_after_the_ttl() -> None:
    """Caching must not hide a Marker that went away."""
    client = MarkerClient(_BASE)

    with respx.mock:
        respx.get(_HEALTH).mock(return_value=httpx.Response(200))
        assert await client.healthy() is True

    # Expire the cache rather than sleeping out the TTL.
    client._health_cached = None

    with respx.mock:
        respx.get(_HEALTH).mock(side_effect=httpx.ConnectError("down"))
        assert await client.healthy() is False


async def test_an_injected_client_is_never_closed() -> None:
    """An injected client belongs to whoever injected it."""
    async with httpx.AsyncClient() as injected:
        with respx.mock:
            respx.get(_HEALTH).mock(return_value=httpx.Response(200))
            client = MarkerClient(_BASE, client=injected)
            assert await client.healthy() is True
            await client.aclose()
        assert not injected.is_closed
