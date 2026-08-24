"""FastAPI app hosting the MCP streamable-HTTP surface.

# Mount mechanics ported in spirit from PaperHub
# `backend/src/paperhub/mcp/mounting.py` @ fd65834.
# Adapted: no request-context middleware, no database connection, no tracer —
# this service is stateless, so a tool call needs nothing but its arguments.
# Also updated for the `mcp` 2.x API: `MCPServer` replaces 1.x's `FastMCP`;
# transport options (json_response, stateless_http, transport_security) moved
# from `server.settings` into `streamable_http_app()` kwargs; `Tool` exposes
# `input_schema`, not `inputSchema`; and `session_manager` raises unless
# `streamable_http_app()` has already been called.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from paper_mcp import __version__
from paper_mcp.api.artifacts import router as artifacts_router
from paper_mcp.api.middleware import AuthQuotaMiddleware
from paper_mcp.config import Settings, settings
from paper_mcp.skills import load_skills
from paper_mcp.tools.extract import marker_client, tool_extract_pdf, tool_get_job

_LOG = logging.getLogger(__name__)

SERVER_NAME = "paper"
MCP_PATH = "/mcp"


def build_mcp_server() -> MCPServer[Any]:
    """Construct the MCP server with the extraction tools registered.

    Tool input schemas are derived from each handler's annotated signature,
    so the Pydantic models are the single source of truth (SRS NFR-04).
    Descriptions declare each tool's network scope (SRS NFR-05).
    """
    server: MCPServer[Any] = MCPServer(name=SERVER_NAME, version=__version__)

    server.add_tool(
        tool_extract_pdf,
        name="extract_pdf",
        description=(
            "Extract a PDF you already hold into agent-ready data: the full text "
            "as markdown (real tables, equations as LaTeX, headings intact) plus a "
            "figure index of ids, captions and image URLs. Send the file as base64 "
            "in content_base64. Extraction is by Marker; a dense paper takes "
            "roughly a minute per page, so this returns the bundle when cached and "
            "otherwise a job handle to poll with get_job. Identical bytes are a "
            "cache hit. Scope: NO network — this tool never fetches anything, so "
            "acquire the PDF yourself; writes only to the artifact cache."
        ),
    )
    server.add_tool(
        tool_get_job,
        name="get_job",
        description=(
            "Check a background extraction started by extract_pdf. Returns state "
            "(queued/running/done/error). When done, call extract_pdf again to "
            "get the bundle from cache. No network scope."
        ),
    )
    _register_skills(server)
    return server


def _register_skills(server: MCPServer[Any]) -> None:
    """Serve each skill bundle as an MCP prompt.

    Bound eagerly per skill: a late-binding closure over the loop variable
    would give every prompt the last skill's text.
    """
    for name, (description, body) in load_skills().items():

        def make(text: str = body):  # type: ignore[no-untyped-def]
            def render() -> str:
                return text

            return render

        server.prompt(name=name, description=description or None)(make())


# Framing around the payload: the JSON-RPC envelope, the method and tool
# names, and a filename. A few hundred bytes in practice; 64 KiB is slack
# bought cheaply, since the number only bounds a rejection.
_ENVELOPE_SLACK = 64 * 1024


def request_body_limit(max_upload_bytes: int) -> int:
    """Transport body limit that admits a PDF at `max_upload_bytes`.

    The MCP SDK defaults to 4 MiB and enforces it *before* a request reaches
    any tool, so the app's own upload cap was unreachable dead code: 25 MiB of
    PDF needs a 33 MiB body. Measured against a real corpus, that default
    rejected 35 of 44 papers — as a bare `413` outside JSON-RPC, with no
    mention of a limit, so `extract_pdf`'s carefully worded size error never
    ran once.

    Equalling the cap would not fix it. `extract_pdf` carries the file as
    base64, which inflates by 4/3, so the body must be bigger than the file it
    is meant to allow. Deriving it here keeps one number configurable
    (`PAPER_MCP_MAX_UPLOAD_BYTES`) and the transport in agreement with the
    tool, which is what went wrong.
    """
    return max_upload_bytes * 4 // 3 + _ENVELOPE_SLACK


def transport_security(cfg: Settings) -> TransportSecuritySettings:
    """Build DNS-rebinding protection from configured allowed hosts.

    The MCP transport validates the `Host` header so a hostile page cannot
    drive a locally-bound server. A public deployment must therefore list its
    own hostname in `PAPER_MCP_ALLOWED_HOSTS`; the default covers localhost
    only. A literal `*` disables the check — an explicit operator choice,
    warned about at boot, never a silent default.
    """
    if cfg.auth_mode != "open" and not (cfg.oidc_issuer and cfg.oidc_audience):
        _LOG.error(
            "AUTH_MODE=%s but PAPER_MCP_OIDC_ISSUER/AUDIENCE are unset — every "
            "request will be rejected. Configure the IdP or set AUTH_MODE=open.",
            cfg.auth_mode,
        )
    if "*" in cfg.allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = list(cfg.allowed_origins) or [
        f"{scheme}://{host}" for host in cfg.allowed_hosts for scheme in ("http", "https")
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(cfg.allowed_hosts),
        allowed_origins=origins,
    )


def create_app() -> FastAPI:
    """Build the FastAPI app with the MCP sub-app serving `POST /mcp`.

    The sub-app owns the full `/mcp` path and is mounted at `""` rather than
    built with `streamable_http_path="/"` and mounted at `"/mcp"`. That
    obvious-looking arrangement makes Starlette strip the prefix, leaving the
    sub-app to match `""` against its `"/"` route — so `POST /mcp` answers
    **307 -> /mcp/** instead of doing the work. Test clients follow redirects
    and hide it; a connector configured with `https://host/mcp` may not.
    Mounting at `""` passes the untouched path through, so `/mcp` is a direct
    200. `/health` is registered first and therefore still wins.
    """
    server = build_mcp_server()
    mcp_app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        # Without this the SDK's 4 MiB default silently governs and the
        # configured upload cap can never be reached (see request_body_limit).
        max_request_body_size=request_body_limit(settings().max_upload_bytes),
        transport_security=transport_security(settings()),
    )
    # `session_manager` is only valid AFTER streamable_http_app() has built it.
    session_manager = server.session_manager

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Starlette does NOT propagate a mounted sub-app's lifespan, so the
        # session manager's task group must be entered here or the first
        # POST /mcp fails with "Task group is not initialized".
        async with session_manager.run():
            _LOG.info("paper-mcp %s ready; mcp mounted at %s", __version__, MCP_PATH)
            yield

    app = FastAPI(title="paper-mcp", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        # Marker is required, not optional (SRS v0.2): if it is down, PDF
        # extraction fails outright rather than degrading, so an operator
        # needs to see that here rather than discover it per request.
        return {
            "status": "ok",
            "version": __version__,
            "auth_mode": settings().auth_mode,
            "marker": "up" if await marker_client().healthy() else "down",
        }

    # Authenticate and meter before anything reaches a tool.
    app.add_middleware(AuthQuotaMiddleware)
    app.include_router(artifacts_router)
    app.mount("", mcp_app)
    return app


# uvicorn accepts only these level names, lowercase.
_UVICORN_LEVELS = frozenset({"critical", "error", "warning", "info", "debug", "trace"})


def stdlib_log_level(level: str) -> int:
    """Translate the configured level for `logging.basicConfig`.

    The stdlib accepts only upper-case names and raises `ValueError: Unknown
    level: 'info'` on anything else — which kills the process at boot, before
    any log line explains why. Lower-case is the natural thing to write in a
    compose file (this project's own compose file did), so it must work.
    """
    return getattr(logging, level.strip().upper(), logging.INFO)


def uvicorn_log_level(level: str) -> str:
    """Translate the configured level into what uvicorn will accept.

    uvicorn builds its own logging config and does not inherit
    `logging.basicConfig`, so the level has to be handed to it explicitly.
    Without this, `PAPER_MCP_LOG_LEVEL` silently does nothing: measured at
    18076 bytes of output for 300 requests at WARNING versus 18222 at INFO,
    a 1% difference. That is not just noise — a server whose stdout backs up
    blocks inside write(), and per-request logging is what fills the buffer.

    An unrecognised value falls back to `info` rather than crashing the
    process on startup over a typo in an env var.
    """
    lowered = level.strip().lower()
    return lowered if lowered in _UVICORN_LEVELS else "info"


def main() -> None:
    """Console-script entry point."""
    cfg = settings()
    logging.basicConfig(level=stdlib_log_level(cfg.log_level))
    if cfg.auth_mode == "open":
        _LOG.warning(
            "AUTH_MODE=open — every caller is unauthenticated. "
            "Do not run this on a public network.",
        )
    if cfg.auth_mode != "open" and not (cfg.oidc_issuer and cfg.oidc_audience):
        _LOG.error(
            "AUTH_MODE=%s but PAPER_MCP_OIDC_ISSUER/AUDIENCE are unset — every "
            "request will be rejected. Configure the IdP or set AUTH_MODE=open.",
            cfg.auth_mode,
        )
    if "*" in cfg.allowed_hosts:
        _LOG.warning(
            "PAPER_MCP_ALLOWED_HOSTS=* — DNS-rebinding protection is DISABLED. "
            "Set this to the deployment's hostname.",
        )
    uvicorn.run(
        create_app(),
        host=cfg.host,
        port=cfg.port,
        log_level=uvicorn_log_level(cfg.log_level),
    )
