"""Auth and quota, exercised through the real HTTP surface.

Tokens are signed with a locally generated RSA key and the JWKS is served by
a mocked issuer, so this covers the actual verification path rather than a
stubbed `verify_token`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import paper_mcp.quota as quota_mod
from paper_mcp import auth as auth_mod
from paper_mcp.auth import anonymous_principal
from paper_mcp.bundle import Bundle, DocumentRef
from paper_mcp.context import reset_principal, set_principal
from paper_mcp.quota import QuotaExceededError, QuotaLimits, QuotaStore, reset_quota_store
from paper_mcp.server import create_app


def _b64_of(data: bytes) -> str:
    return base64.b64encode(data).decode()

_ISSUER = "https://idp.example.org"
_AUDIENCE = "paper-mcp"
_JWKS_URL = f"{_ISSUER}/.well-known/jwks.json"
_KID = "test-key-1"


@pytest.fixture(scope="module")
def keypair() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(key: Any) -> dict[str, Any]:
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public_jwk.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    return {"keys": [public_jwk]}


def _token(key: Any, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": "user-123",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": _KID})


@pytest.fixture
def secured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_MCP_AUTH_MODE", "oidc")
    monkeypatch.setenv("PAPER_MCP_OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("PAPER_MCP_OIDC_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("PAPER_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("PAPER_MCP_SUBJECT_SALT", "test-salt")
    auth_mod.reset_jwks_cache()
    reset_quota_store()


def _post(client: TestClient, token: str | None = None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )


# --- token verification ---------------------------------------------------


@respx.mock
def test_a_valid_token_is_accepted(secured: None, keypair: Any) -> None:
    respx.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(keypair)))

    with TestClient(create_app()) as client:
        response = _post(client, _token(keypair))

    assert response.status_code == 200
    assert "tools" in response.json()["result"]


@respx.mock
def test_no_token_is_rejected(secured: None, keypair: Any) -> None:
    respx.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(keypair)))

    with TestClient(create_app()) as client:
        response = _post(client)

    assert response.status_code == 401
    # RFC 6750: a compliant client needs to be told how to authenticate.
    assert "Bearer" in response.headers.get("www-authenticate", "")


@respx.mock
def test_a_token_for_another_audience_is_rejected(secured: None, keypair: Any) -> None:
    # The classic confused-deputy: a token minted for a different service.
    respx.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(keypair)))

    with TestClient(create_app()) as client:
        response = _post(client, _token(keypair, aud="some-other-service"))

    assert response.status_code == 401


@respx.mock
def test_an_expired_token_is_rejected(secured: None, keypair: Any) -> None:
    respx.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(keypair)))
    past = int(time.time()) - 60

    with TestClient(create_app()) as client:
        response = _post(client, _token(keypair, exp=past, iat=past - 300))

    assert response.status_code == 401


@respx.mock
def test_a_token_from_another_issuer_is_rejected(secured: None, keypair: Any) -> None:
    respx.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(keypair)))

    with TestClient(create_app()) as client:
        response = _post(client, _token(keypair, iss="https://evil.example"))

    assert response.status_code == 401


@respx.mock
def test_a_token_signed_by_the_wrong_key_is_rejected(secured: None, keypair: Any) -> None:
    # The attack that matters most: a well-formed token someone else signed.
    respx.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(keypair)))
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with TestClient(create_app()) as client:
        response = _post(client, _token(attacker))

    assert response.status_code == 401


@respx.mock
def test_rejections_do_not_reveal_which_check_failed(secured: None, keypair: Any) -> None:
    # Telling an attacker "expired" versus "wrong audience" tells them which
    # knob to turn next.
    respx.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(keypair)))
    past = int(time.time()) - 60

    with TestClient(create_app()) as client:
        bodies = {
            _post(client, _token(keypair, exp=past, iat=past - 300)).text,
            _post(client, _token(keypair, aud="other")).text,
            _post(client, _token(keypair, iss="https://evil.example")).text,
        }

    assert len(bodies) == 1, f"responses differ and leak the reason: {bodies}"


@respx.mock
def test_a_garbage_token_is_rejected_not_crashed(secured: None, keypair: Any) -> None:
    respx.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(keypair)))

    with TestClient(create_app()) as client:
        response = _post(client, "not.a.jwt")

    assert response.status_code == 401


# --- open paths -----------------------------------------------------------


@respx.mock
def test_health_stays_open(secured: None, keypair: Any) -> None:
    # A readiness probe that needs a token is useless to an orchestrator.
    respx.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(keypair)))
    respx.get("http://127.0.0.1:8002/health").mock(return_value=httpx.Response(200))

    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200


# --- quota ----------------------------------------------------------------


def test_a_bucket_refills_over_time() -> None:
    store = QuotaStore(QuotaLimits(calls_per_minute=60.0))

    for i in range(60):
        store.consume("subject", "calls", now=1000.0 + i * 0.001)
    with pytest.raises(QuotaExceededError):
        store.consume("subject", "calls", now=1000.1)

    # A minute later the bucket is full again.
    store.consume("subject", "calls", now=1060.0)


def test_exhaustion_reports_how_long_to_wait() -> None:
    store = QuotaStore(QuotaLimits(calls_per_minute=60.0))
    for _ in range(60):
        store.consume("s", "calls", now=500.0)

    with pytest.raises(QuotaExceededError) as exc:
        store.consume("s", "calls", now=500.0)

    assert exc.value.resource == "calls"
    assert 0 < exc.value.retry_after <= 2.0


def test_one_caller_cannot_starve_another() -> None:
    # The whole point: buckets are per subject.
    store = QuotaStore(QuotaLimits(calls_per_minute=10.0))
    for _ in range(10):
        store.consume("greedy", "calls", now=100.0)

    with pytest.raises(QuotaExceededError):
        store.consume("greedy", "calls", now=100.0)
    store.consume("polite", "calls", now=100.0)  # unaffected


def test_expensive_resources_are_metered_separately() -> None:
    # Burning the call budget must not block an extraction, and vice versa.
    store = QuotaStore(QuotaLimits(calls_per_minute=1.0, extractions_per_hour=1.0))
    store.consume("s", "calls", now=0.0)
    store.consume("s", "extractions", now=0.0)

    with pytest.raises(QuotaExceededError):
        store.consume("s", "calls", now=0.0)
    with pytest.raises(QuotaExceededError):
        store.consume("s", "extractions", now=0.0)


def test_compile_seconds_are_charged_by_duration() -> None:
    # One pathological document costs more than ten ordinary ones, so the
    # meter is time rather than call count.
    store = QuotaStore(QuotaLimits(compile_seconds_per_hour=100.0))

    store.consume("s", "compile_seconds", amount=90.0, now=0.0)

    with pytest.raises(QuotaExceededError):
        store.consume("s", "compile_seconds", amount=20.0, now=0.0)


@respx.mock
def test_over_quota_returns_429_with_retry_after(secured: None, keypair: Any) -> None:
    respx.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks(keypair)))
    import paper_mcp.quota as quota_mod

    quota_mod._store = QuotaStore(QuotaLimits(calls_per_minute=2.0))
    token = _token(keypair)

    with TestClient(create_app()) as client:
        statuses = [_post(client, token).status_code for _ in range(4)]
        final = _post(client, token)

    assert 429 in statuses or final.status_code == 429
    if final.status_code == 429:
        assert final.headers.get("retry-after")
        assert final.json()["error"] == "quota_exceeded"


def test_open_mode_still_meters_by_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    # A development instance left exposed should still have brakes.
    monkeypatch.setenv("PAPER_MCP_AUTH_MODE", "open")
    monkeypatch.setenv("PAPER_MCP_ALLOWED_HOSTS", "testserver")
    import paper_mcp.quota as quota_mod

    quota_mod._store = QuotaStore(QuotaLimits(calls_per_minute=2.0))

    with TestClient(create_app()) as client:
        statuses = [_post(client).status_code for _ in range(5)]

    assert 429 in statuses


def test_subject_hash_does_not_expose_the_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    # Metering needs a stable key, not a record of who read what.
    monkeypatch.setenv("PAPER_MCP_SUBJECT_SALT", "salt")
    digest = auth_mod.subject_hash("user@example.com")

    assert "user@example.com" not in digest
    assert digest == auth_mod.subject_hash("user@example.com")
    assert digest != auth_mod.subject_hash("other@example.com")


def _extract_call(client: object, pdf: bytes) -> dict[str, object]:
    import base64

    resp = client.post(  # type: ignore[attr-defined]
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "extract_pdf", "arguments": {
                "content_base64": base64.b64encode(pdf).decode(),
            }},
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    body: dict[str, object] = resp.json()
    return body.get("result", body)  # type: ignore[return-value]


def _pdf(tag: str) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), tag)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def test_starting_an_extraction_charges_the_gpu_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`PAPER_MCP_QUOTA_EXTRACTIONS_PER_HOUR` was never charged by anything.

    The middleware consumes `"calls"` and nothing else, so the one genuinely
    scarce resource — GPU minutes, as `quota.py`'s own docstring calls it —
    had no meter. Measured against a running service with the limit set to 2:
    five distinct PDFs, five accepted. On a shared endpoint with one worker
    that is how a single caller stalls everybody else's queue, which is the
    fairness ceiling the setting exists to be.

    This is also the test that proves the caller's identity reaches the tool
    at all: quota is per-principal, the tool sees only its arguments, and
    `BaseHTTPMiddleware` runs the downstream app in its own task.
    """
    import paper_mcp.tools.extract as extract_mod

    async def _never_finishes(pdf: bytes, **kwargs: object) -> object:
        await asyncio.Event().wait()

    monkeypatch.setattr(extract_mod, "build_bundle", _never_finishes)
    monkeypatch.setenv("PAPER_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("PAPER_MCP_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("PAPER_MCP_QUOTA_EXTRACTIONS_PER_HOUR", "1")

    with TestClient(create_app()) as client:
        first = _extract_call(client, _pdf("first paper"))
        second = _extract_call(client, _pdf("second paper"))

    assert first.get("isError") is not True, f"the first extraction was refused: {first}"
    assert second.get("isError") is True, "the second extraction was not metered"
    assert "quota" in str(second).lower()


def test_a_cached_paper_does_not_spend_the_gpu_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The budget meters GPU minutes, so a cache hit must be free.

    Charging on every call would make the limit a call limit under another
    name, and would punish exactly the access pattern the content-addressed
    cache exists to encourage — polling `extract_pdf` until it is warm, which
    is what the tool's own hint tells a caller to do.
    """
    import paper_mcp.tools.extract as extract_mod
    from paper_mcp.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(extract_mod, "_store", store)
    monkeypatch.setenv("PAPER_MCP_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    quota = QuotaStore(QuotaLimits(extractions_per_hour=1.0))
    monkeypatch.setattr(quota_mod, "_store", quota)

    pdf = _pdf("a paper already extracted")
    key = extract_mod.bundle_key(pdf)
    entry = store.ensure(key)
    (entry / "bundle.json").write_text(
        Bundle(bundle_id=key, document=DocumentRef(content_sha256="x")).model_dump_json(),
        encoding="utf-8",
    )

    principal = anonymous_principal("10.0.0.9")
    token = set_principal(principal)
    try:
        for _ in range(5):
            result = asyncio.run(extract_mod.tool_extract_pdf(_b64_of(pdf)))
            assert result.status == "ready"
    finally:
        reset_principal(token)

    # Never charged means no bucket was ever opened for it — which is the
    # proof, so spell out what it buys: the single unit is still there for a
    # real extraction, and only the second one is refused.
    assert quota.remaining(principal.subject_hash, "extractions") == float("inf")
    quota.consume(principal.subject_hash, "extractions")
    with pytest.raises(QuotaExceededError):
        quota.consume(principal.subject_hash, "extractions")


def test_a_spoofed_forwarded_for_header_does_not_reset_the_meter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In open mode the per-IP meter is the only brake, and it was one header.

    `_client_ip` trusted `X-Forwarded-For` from anyone, with no trusted-proxy
    check — and compose publishes `8000:8000` with no proxy in front. Measured
    against a running service with the limit at 5 calls/minute: 60 requests
    with a rotating header, 60 accepted. The comment said the header "is set
    by the proxy in front of a public deployment"; nothing checked that there
    was one.

    Trusting a hop that has not been declared is the bug. Default to the peer
    address, which cannot be forged by the peer.
    """
    monkeypatch.setenv("PAPER_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("PAPER_MCP_QUOTA_CALLS_PER_MINUTE", "3")

    codes = []
    with TestClient(create_app()) as client:
        for i in range(8):
            resp = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": i, "method": "tools/list", "params": {}},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "X-Forwarded-For": f"10.9.0.{i}",
                },
            )
            codes.append(resp.status_code)

    assert 429 in codes, f"rotating one header defeated the rate limit: {codes}"


def test_a_declared_proxy_deployment_still_meters_each_client_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behind a real proxy the peer address is the proxy, for everyone.

    Ignoring `X-Forwarded-For` unconditionally would meter every caller
    against one bucket, so one busy client would rate-limit the rest. An
    operator who has actually put a proxy in front says so, and then the
    header is the client.
    """
    monkeypatch.setenv("PAPER_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("PAPER_MCP_QUOTA_CALLS_PER_MINUTE", "3")
    monkeypatch.setenv("PAPER_MCP_TRUST_FORWARDED_FOR", "1")

    codes = []
    with TestClient(create_app()) as client:
        for i in range(8):
            resp = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": i, "method": "tools/list", "params": {}},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "X-Forwarded-For": f"10.9.0.{i}",
                },
            )
            codes.append(resp.status_code)

    assert codes == [200] * 8, f"a declared proxy must meter per client: {codes}"
