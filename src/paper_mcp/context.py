"""The calling principal, for the duration of one request.

A tool needs nothing but its arguments — that is the design, and it is why
there is no request-context middleware here. Quota is the one exception, and
it is a real one: metering GPU minutes per caller requires knowing which
caller, and `extract_pdf` is the only thing that knows whether a call is
about to spend any.

So this carries exactly one value, set once by `AuthQuotaMiddleware` and read
once by `extract_pdf`. A `ContextVar` rather than a parameter because the MCP
SDK owns the call path between them, and threading a principal through it
would mean changing every tool signature to serve one tool.

Absent is a legitimate state: a direct call in a test, or any use of the
tool outside a request, has no principal and is not metered. Metering an
un-authenticated internal call against a bucket keyed on nothing would be
worse than not metering it.
"""
from __future__ import annotations

from contextvars import ContextVar, Token

from paper_mcp.auth import Principal

_PRINCIPAL: ContextVar[Principal | None] = ContextVar("paper_mcp_principal", default=None)


def set_principal(principal: Principal | None) -> Token[Principal | None]:
    """Bind the caller for this request; returns the token to restore with."""
    return _PRINCIPAL.set(principal)


def reset_principal(token: Token[Principal | None]) -> None:
    _PRINCIPAL.reset(token)


def current_principal() -> Principal | None:
    """The caller this request belongs to, or None outside a request."""
    return _PRINCIPAL.get()
