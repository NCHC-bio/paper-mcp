"""OIDC resource-server token verification.

This service validates tokens; it never issues them. Owning issuance,
refresh, revocation and consent is a security-critical subsystem far from
this project's competence, and an IdP the operator already trusts does it
better (SRS §II-4).

Identity exists for **quota and revocation only**. There is no per-user data
to scope — statelessness removed it — so every authenticated caller can reach
every tool. What gets logged and metered is `HMAC(salt, sub)`, not `sub`:
metering needs a stable key, not a record of which person read which paper.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from paper_mcp.config import settings

logger = logging.getLogger(__name__)

_JWKS_TTL_SECONDS = 3600.0
# A `kid` we have never seen triggers one refetch, because keys rotate and a
# stale cache would lock every caller out. That refetch is rate-limited so an
# attacker cannot turn unknown `kid`s into a battering ram against the IdP.
_MIN_REFETCH_INTERVAL = 30.0

_jwks_cache: dict[str, Any] | None = None
_jwks_fetched_at = 0.0
_last_refetch_attempt = 0.0
# One client for the process. A fresh `AsyncClient` per verification means a
# fresh TCP and TLS handshake against the IdP for every request that misses.
_jwks_client: httpx.AsyncClient | None = None
# Created lazily rather than at import: a lock binds to the loop that first
# awaits it, and the test suite runs more than one loop.
_jwks_lock: asyncio.Lock | None = None
# When the last fetch failed, how long to refuse without trying again. An
# unreachable IdP never populates the cache, so without this every request
# pays the full 10 s timeout — measured at 10.50 s, 10.40 s and 10.39 s for
# three consecutive requests, which is the whole service offline rather than
# authentication degraded.
_negative_until = 0.0
_NEGATIVE_TTL_SECONDS = 30.0


class AuthError(Exception):
    """Token rejected. The reason is logged, never returned.

    Distinguishing "expired" from "wrong audience" from "bad signature" tells
    an attacker which knob to turn next.
    """


@dataclass(frozen=True)
class Principal:
    subject: str
    subject_hash: str
    anonymous: bool = False


def _salt() -> bytes:
    configured = os.environ.get("PAPER_MCP_SUBJECT_SALT")
    if configured:
        return configured.encode("utf-8")
    # Per-process fallback: metering still works within a process, and the
    # hash never leaves it in a form anyone can correlate across restarts.
    return hashlib.sha256(b"paper-mcp-ephemeral-salt").digest()


def subject_hash(subject: str) -> str:
    return hmac.new(_salt(), subject.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def anonymous_principal(client_ip: str) -> Principal:
    """Identity for open mode, so per-IP metering still has a key."""
    return Principal(
        subject=f"ip:{client_ip}", subject_hash=subject_hash(f"ip:{client_ip}"), anonymous=True
    )


def reset_jwks_cache() -> None:
    global _jwks_cache, _jwks_fetched_at, _last_refetch_attempt
    global _jwks_client, _jwks_lock, _negative_until
    _jwks_cache = None
    _jwks_fetched_at = 0.0
    _last_refetch_attempt = 0.0
    _negative_until = 0.0
    # Dropped rather than closed: this is test-scope teardown, and closing
    # needs a running loop that may already be gone.
    _jwks_client = None
    _jwks_lock = None


def _jwks_url(issuer: str) -> str:
    return f"{issuer.rstrip('/')}/.well-known/jwks.json"


def _client() -> httpx.AsyncClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    return _jwks_client


def _lock() -> asyncio.Lock:
    global _jwks_lock
    if _jwks_lock is None:
        _jwks_lock = asyncio.Lock()
    return _jwks_lock


async def _fetch_jwks(issuer: str) -> dict[str, Any]:
    """Fetch the issuer's key set over httpx.

    Deliberately not `PyJWKClient`, which fetches with `urllib`: every other
    outbound call here is httpx, and a second HTTP stack means different
    timeout and proxy behaviour, and nothing respx or a test can observe.

    Awaited, not blocking. `httpx.get` here ran inside async middleware and
    held the event loop for the whole IdP round trip: against an 8 s JWKS
    endpoint, one request with one bogus token pushed `/health` to 16.6 s
    against a 248 ms idle baseline. No valid token was needed to trigger it,
    only a well-formed JWT header.
    """
    response = await _client().get(_jwks_url(issuer))
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


async def _refresh(issuer: str, *, force: bool = False) -> None:
    """Populate the key-set cache, once however many callers are waiting.

    `force` is the unknown-kid path, which must refetch a cache that is still
    inside its TTL — otherwise a rotated key locks every caller out until the
    hour is up.
    """
    global _jwks_cache, _jwks_fetched_at, _negative_until

    async with _lock():
        now = time.monotonic()
        # Another waiter may have refreshed while this one held at the lock.
        if not force and _jwks_cache is not None and now - _jwks_fetched_at <= _JWKS_TTL_SECONDS:
            return
        if now < _negative_until:
            raise AuthError("jwks unavailable")
        try:
            fetched = await _fetch_jwks(issuer)
        except (httpx.HTTPError, ValueError) as exc:
            _negative_until = time.monotonic() + _NEGATIVE_TTL_SECONDS
            logger.info("jwks fetch failed: %s", type(exc).__name__)
            raise AuthError("jwks unavailable") from exc
        _jwks_cache = fetched
        _jwks_fetched_at = time.monotonic()
        _negative_until = 0.0


async def _signing_key(token: str, issuer: str) -> Any:
    """Resolve the key that signed `token`, refetching once on an unknown kid."""
    global _last_refetch_attempt

    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise AuthError("malformed token header") from exc

    if _jwks_cache is None or time.monotonic() - _jwks_fetched_at > _JWKS_TTL_SECONDS:
        await _refresh(issuer)

    key = _match_kid(_jwks_cache or {}, kid)
    if key is not None:
        return key

    # Keys rotate; a stale cache must not lock every caller out. One refetch,
    # rate-limited so unknown kids cannot be turned into a battering ram
    # against the IdP. The rate limit bounds how *often* this happens — it
    # never bounded what it cost when it did, which is what made an unknown
    # kid a freeze anyone could trigger on demand. The single-flight and the
    # negative cache bound the cost.
    now = time.monotonic()
    if now - _last_refetch_attempt < _MIN_REFETCH_INTERVAL:
        raise AuthError("unknown signing key")
    _last_refetch_attempt = now
    await _refresh(issuer, force=True)
    key = _match_kid(_jwks_cache or {}, kid)
    if key is None:
        raise AuthError("unknown signing key")
    return key


def _match_kid(jwks: dict[str, Any], kid: str | None) -> Any:
    for entry in jwks.get("keys", []):
        if kid is None or entry.get("kid") == kid:
            return jwt.PyJWK(entry).key
    return None


async def verify_token(
    token: str, *, issuer: str | None = None, audience: str | None = None
) -> Principal:
    """Verify a bearer JWT and return the caller's principal."""
    cfg = settings()
    issuer = issuer or cfg.oidc_issuer
    audience = audience or cfg.oidc_audience
    if not issuer or not audience:
        raise AuthError("OIDC issuer/audience are not configured")

    try:
        signing_key = await _signing_key(token, issuer)
    except AuthError:
        raise
    except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError) as exc:
        logger.info("jwks lookup failed: %s", type(exc).__name__)
        raise AuthError("token rejected") from exc

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "RS512", "ES256", "ES384"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # Logged with the reason, returned without it.
        logger.info("token rejected: %s", type(exc).__name__)
        raise AuthError("token rejected") from exc

    subject = str(claims["sub"])
    return Principal(subject=subject, subject_hash=subject_hash(subject))
