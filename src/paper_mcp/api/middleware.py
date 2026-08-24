"""Authenticate and meter every tool call.

Applied to the MCP endpoint only. `/health` and artifact downloads stay open:
a readiness probe that needs a token is useless to an orchestrator, and an
artifact URL is already a capability — its unguessable token *is* the
credential.
"""
from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from paper_mcp.auth import AuthError, Principal, anonymous_principal, verify_token
from paper_mcp.config import request_body_limit, settings
from paper_mcp.context import reset_principal, set_principal
from paper_mcp.quota import QuotaExceededError, quota_store

logger = logging.getLogger(__name__)

# Paths that must work without a token.
_OPEN_PREFIXES = ("/health", "/a/")


def _client_ip(request: Request, *, trust_forwarded: bool) -> str:
    """The key open mode meters against.

    `X-Forwarded-For` used to be trusted from anyone, on the strength of a
    comment saying it "is set by the proxy in front of a public deployment" —
    with nothing checking that a proxy existed. Compose publishes port 8000
    directly, so in the shipped configuration the header comes from the
    caller. Measured against a running service at 5 calls/minute: 60 requests
    with a rotating header, 60 accepted. In open mode this meter is the only
    brake there is.

    Ignoring the header outright is not the answer either — behind a real
    proxy the peer address is the proxy for every caller, so one busy client
    would throttle the rest. So the operator declares it. Off by default,
    because the peer address is the only value a peer cannot forge.
    """
    if trust_forwarded:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class AuthQuotaMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        if any(path.startswith(prefix) for prefix in _OPEN_PREFIXES):
            return await call_next(request)

        cfg = settings()

        # Answered here so an oversized upload is refused in this service's
        # own vocabulary. The MCP SDK enforces the same ceiling a layer down
        # and returns `413 Request body too large` — bare text, outside
        # JSON-RPC, naming no limit and offering no way to raise it. That is
        # the failure the derived limit was meant to end, and it was still
        # reachable for any body more than ~64 KiB over the cap.
        #
        # Content-Length only: a chunked body has no declared size, so the
        # SDK's check remains the backstop for one. Every MCP client sends a
        # length, so in practice this is the path a caller hits.
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > request_body_limit(cfg.max_upload_bytes):
            return _payload_too_large(int(declared), cfg.max_upload_bytes)

        principal: Principal

        if cfg.auth_mode == "open":
            # Metering still applies, keyed by IP — otherwise a development
            # instance left exposed has no brakes at all.
            principal = anonymous_principal(
                _client_ip(request, trust_forwarded=cfg.trust_forwarded_for)
            )
        else:
            header = request.headers.get("authorization", "")
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not token:
                return _unauthorized("a bearer token is required")
            try:
                principal = verify_token(token)
            except AuthError:
                # Deliberately uniform: distinguishing expired from
                # wrong-audience from bad-signature tells an attacker which
                # knob to turn next. The reason is logged, not returned.
                return _unauthorized("token rejected")

        try:
            quota_store().consume(principal.subject_hash, "calls")
        except QuotaExceededError as exc:
            return _too_many(exc)

        request.state.principal = principal
        # Also bound to the context, because `request.state` cannot reach a
        # tool: the MCP SDK owns the call path and hands a handler nothing but
        # its arguments. `extract_pdf` needs the principal to charge the
        # extraction budget, which is the one resource worth metering and the
        # one that was never metered at all. Set before `call_next` so the
        # downstream task inherits it — anyio copies the context at spawn.
        bound = set_principal(principal)
        try:
            return await call_next(request)
        finally:
            reset_principal(bound)


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": detail},
        status_code=401,
        # RFC 6750: tell a compliant client how to authenticate.
        headers={"WWW-Authenticate": 'Bearer realm="paper-mcp"'},
    )


def _too_many(exc: QuotaExceededError) -> JSONResponse:
    return JSONResponse(
        {
            "error": "quota_exceeded",
            "detail": str(exc),
            "resource": exc.resource,
            "retry_after": round(exc.retry_after, 1),
        },
        status_code=429,
        headers={"Retry-After": str(max(1, int(exc.retry_after)))},
    )


def _payload_too_large(declared: int, max_upload_bytes: int) -> JSONResponse:
    return JSONResponse(
        {
            "error": "payload_too_large",
            "detail": (
                f"request body is {declared} bytes; this endpoint accepts a PDF up "
                f"to {max_upload_bytes} bytes, which is about "
                f"{request_body_limit(max_upload_bytes)} bytes of base64 request. "
                "Send a smaller PDF, split it, or raise PAPER_MCP_MAX_UPLOAD_BYTES."
            ),
            "max_upload_bytes": max_upload_bytes,
        },
        status_code=413,
    )
