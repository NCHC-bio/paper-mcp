from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver import MCPServer

import paper_mcp.server as server_mod
from paper_mcp.config import settings
from paper_mcp.server import build_mcp_server, create_app, transport_security

EXPECTED_TOOLS = {"extract_pdf", "get_job"}


@pytest.fixture
def _allow_testserver(monkeypatch: pytest.MonkeyPatch) -> None:
    """TestClient sends `Host: testserver`, which DNS-rebinding protection
    rejects by default — exactly as it would reject an unlisted public
    hostname. Allowlist it the same way an operator allowlists their domain.
    """
    monkeypatch.setenv("PAPER_MCP_ALLOWED_HOSTS", "testserver")


def test_health_reports_ok_and_version(_allow_testserver: None) -> None:
    with TestClient(create_app()) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"


async def test_the_extraction_tools_are_registered() -> None:
    server = build_mcp_server()

    names = {tool.name for tool in await server.list_tools()}

    assert names >= EXPECTED_TOOLS


async def test_every_tool_declares_its_scope_in_its_description() -> None:
    # SRS NFR-05: a caller must be able to see what a tool reaches.
    server = build_mcp_server()

    for tool in await server.list_tools():
        assert tool.description, f"{tool.name} has no description"
        assert "scope" in tool.description.lower(), (
            f"{tool.name} does not declare its network scope"
        )


async def test_tool_input_schemas_are_generated_from_the_signatures() -> None:
    server = build_mcp_server()
    by_name = {tool.name: tool for tool in await server.list_tools()}

    extract = by_name["extract_pdf"].input_schema
    assert "content_base64" in extract["properties"]
    # filename is a label with a default, so only the bytes are required.
    assert extract["required"] == ["content_base64"]

    job = by_name["get_job"].input_schema
    assert job["required"] == ["job_id"]


def test_unlisted_host_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # DNS-rebinding protection is on by default; `testserver` is not allowed.
    monkeypatch.setenv("PAPER_MCP_ALLOWED_HOSTS", "paper-mcp.example.org")

    with TestClient(create_app()) as client:
        resp = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert resp.status_code == 421


def test_wildcard_disables_rebinding_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_MCP_ALLOWED_HOSTS", "*")

    assert transport_security(settings()).enable_dns_rebinding_protection is False


def test_mcp_endpoint_answers_at_slash_mcp_without_redirecting(
    _allow_testserver: None,
) -> None:
    # The URL an operator pastes into a connector is `https://host/mcp`. If
    # that 307s to `/mcp/`, a client that does not follow redirects sees a
    # broken server. Assert the direct hit, with redirects disabled so a
    # regression cannot hide behind the test client following them.
    with TestClient(create_app()) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            follow_redirects=False,
        )

    assert resp.status_code == 200, f"expected a direct 200, got {resp.status_code}"


def test_mcp_endpoint_is_mounted_and_answers_tools_list(_allow_testserver: None) -> None:
    with TestClient(create_app()) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()["result"]["tools"]}
    assert names >= EXPECTED_TOOLS


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("INFO", "info"),
        ("WARNING", "warning"),
        ("  Debug  ", "debug"),
        ("critical", "critical"),
        ("nonsense", "info"),
        ("", "info"),
    ],
)
def test_log_level_is_translated_for_uvicorn(configured: str, expected: str) -> None:
    """uvicorn builds its own logging config and ignores basicConfig.

    Without handing it the level explicitly, PAPER_MCP_LOG_LEVEL did nothing:
    measured 18076 bytes over 300 requests at WARNING versus 18222 at INFO.
    An unknown value must fall back rather than crash the process at startup.
    """
    from paper_mcp.server import uvicorn_log_level

    assert uvicorn_log_level(configured) == expected


@pytest.mark.parametrize(
    ("configured", "expected_name"),
    [
        ("INFO", "INFO"),
        ("info", "INFO"),
        ("  warning ", "WARNING"),
        ("DEBUG", "DEBUG"),
        ("nonsense", "INFO"),
    ],
)
def test_stdlib_log_level_accepts_lowercase(configured: str, expected_name: str) -> None:
    """A lower-case level must not kill the process at boot.

    logging.basicConfig raises ValueError: Unknown level: 'info' on anything
    that is not upper-case, and it does so before any log line can explain
    why. This project's own docker-compose.yml wrote `warning`, so the
    container would have crashed on first boot; the real-workflow check
    caught it, the unit suite did not.
    """
    import logging as _logging

    from paper_mcp.server import stdlib_log_level

    assert stdlib_log_level(configured) == getattr(_logging, expected_name)


def test_request_body_limit_admits_a_pdf_at_the_configured_cap() -> None:
    """The transport limit must exceed the upload cap, not equal it.

    `extract_pdf` carries the PDF as base64 inside a JSON-RPC envelope, so a
    file at the cap arrives as roughly 4/3 its size plus framing. Setting the
    body limit equal to the cap would reject files the tool advertises as
    acceptable — and reject them at the transport, as a bare 413 outside
    JSON-RPC, where the tool's typed error never runs.
    """
    import base64
    import json

    cap = 25 * 1024 * 1024
    limit = server_mod.request_body_limit(cap)

    at_cap = b"%PDF-1.7" + b"0" * (cap - 8)
    envelope = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "extract_pdf", "arguments": {
            "content_base64": base64.b64encode(at_cap).decode(),
            "filename": "a-real-papers-name.pdf",
        }},
    })

    assert len(envelope.encode()) <= limit, (
        f"a {cap}-byte PDF encodes to {len(envelope.encode())} bytes of body, "
        f"over the {limit}-byte transport limit"
    )


def test_the_app_hands_the_transport_our_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SDK defaults to 4 MiB and applies it silently. Measured against a
    # real corpus that rejected 35 of 44 papers, as a 413 with no JSON-RPC
    # error and no mention of a limit anywhere.
    seen: dict[str, object] = {}
    original = MCPServer.streamable_http_app

    def _capture(self: object, **kwargs: object):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(MCPServer, "streamable_http_app", _capture)
    monkeypatch.setenv("PAPER_MCP_MAX_UPLOAD_BYTES", str(9 * 1024 * 1024))

    create_app()

    assert seen["max_request_body_size"] == server_mod.request_body_limit(9 * 1024 * 1024)


def test_an_oversized_body_is_refused_in_json_naming_the_limit(
    _allow_testserver: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body over the transport limit must still answer like this service.

    `request_body_limit` is documented as keeping "the transport in agreement
    with the tool". It only manages that within ~48 KB of the cap: the body
    grows 4/3 with the file, so anything meaningfully larger trips the SDK's
    own check and gets `413 Request body too large` — bare text, outside
    JSON-RPC, naming no limit. Measured at a 20 MB cap: cap+30 KB reached the
    tool and got its worded error; cap+4 MB and cap+43 MB both got the bare
    413. That is the exact failure `request_body_limit` was written to fix,
    still reachable for any caller who sends a genuinely large PDF.

    The limit itself has to exist. What has to change is that hitting it
    tells the caller what the limit is and what to do about it.
    """
    import json

    cap = 1024 * 1024
    monkeypatch.setenv("PAPER_MCP_MAX_UPLOAD_BYTES", str(cap))

    oversized = "A" * (server_mod.request_body_limit(cap) + 1024)
    envelope = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "extract_pdf", "arguments": {"content_base64": oversized}},
    })

    with TestClient(create_app()) as client:
        resp = client.post(
            "/mcp",
            content=envelope,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert resp.status_code == 413
    body = resp.json()
    assert body["error"] == "payload_too_large"
    assert str(cap) in body["detail"], "the caller must be told the actual cap"
    assert "PAPER_MCP_MAX_UPLOAD_BYTES" in body["detail"], "and how to raise it"
