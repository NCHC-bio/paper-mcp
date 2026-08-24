"""Real-workflow acceptance check: does the MCP hand back usable paper data?

Not a unit test and not a mock. It boots the real service, connects with a
real MCP client, and drives the workflow an agent would actually run:

    extract_pdf -> job -> poll -> bundle -> read markdown -> download a figure

Then it judges the *content*, because that is the product. Green here means
an agent can use the output; a green pytest means only that the code runs.

Requires the Marker service (`docker compose up -d marker`). Marker takes
roughly a minute per dense page, so a full paper is a slow check by nature.

Run:  uv run python scripts/paper_workflow_check.py <path/to/paper.pdf>
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.client.client import Client

MARKER_URL = os.environ.get("PAPER_MCP_MARKER_URL", "http://127.0.0.1:8002")
EXTRACT_TIMEOUT_S = float(os.environ.get("PAPER_MCP_CHECK_TIMEOUT_S", "1800"))

_TABLE_RE = re.compile(r"^\|.*\|\s*$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\|\s*---")
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")

_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    icon = {"PASS": "  ok  ", "FAIL": " FAIL ", "INFO": " info "}[status]
    _results.append((status, name, detail))
    print(f"[{icon}] {name}" + (f"\n           {detail}" if detail else ""), flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


def boot(port: int, artifacts: Path) -> tuple[subprocess.Popen[str], Path]:
    env = {
        **os.environ,
        "PAPER_MCP_HOST": "127.0.0.1",
        "PAPER_MCP_PORT": str(port),
        "PAPER_MCP_ALLOWED_HOSTS": f"127.0.0.1:{port}",
        "PAPER_MCP_PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "PAPER_MCP_ARTIFACT_ROOT": str(artifacts),
        "PAPER_MCP_MARKER_URL": MARKER_URL,
        "PAPER_MCP_LOG_LEVEL": "info",
    }
    # Never an undrained pipe: a full pipe buffer blocks the server inside
    # write() and freezes its event loop.
    log = Path(tempfile.gettempdir()) / f"paper-mcp-workflow-{port}.log"
    handle = log.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-c", "from paper_mcp.server import main; main()"],
        env=env, stdout=handle, stderr=subprocess.STDOUT, text=True,
    )
    return proc, log


def payload(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


def error_text(result: Any) -> str:
    for block in getattr(result, "content", None) or []:
        if getattr(block, "text", None):
            return str(block.text)
    return ""


async def run(pdf_path: str, base: str) -> None:
    url = f"{base}/mcp"

    async with Client(url, read_timeout_seconds=120.0) as client:
        # --- the workflow an agent actually runs --------------------------
        started = time.monotonic()
        data = Path(pdf_path).read_bytes()
        args = {
            "content_base64": base64.b64encode(data).decode(),
            "filename": Path(pdf_path).name,
        }
        result = await client.call_tool("extract_pdf", args)
        if result.is_error:
            record("FAIL", "extract_pdf accepted", error_text(result)[:300])
            return
        first = payload(result)
        record(
            "PASS",
            "extract_pdf accepted",
            f"status={first['status']} ({time.monotonic() - started:.1f}s)",
        )

        bundle = first.get("bundle")
        if first["status"] == "extracting":
            job_id = first["job"]["job_id"]
            record(
                "INFO",
                "extraction started",
                f"job {job_id}; polling up to {EXTRACT_TIMEOUT_S:.0f}s",
            )
            deadline = time.monotonic() + EXTRACT_TIMEOUT_S
            state = "queued"
            while time.monotonic() < deadline:
                await asyncio.sleep(10)
                status = payload(await client.call_tool("get_job", {"job_id": job_id}))
                if status["state"] != state:
                    state = status["state"]
                    record("INFO", f"job -> {state}", f"{time.monotonic() - started:.0f}s elapsed")
                if state in ("done", "error"):
                    break
            if state != "done":
                record("FAIL", "extraction completed", f"state={state}")
                return
            record("PASS", "extraction completed", f"{time.monotonic() - started:.0f}s total")
            again = await client.call_tool("extract_pdf", args)
            second = payload(again)
            bundle = second.get("bundle")
            if bundle is None:
                record("FAIL", "bundle available after job", str(second)[:200])
                return

        # --- is the content actually usable? -------------------------------
        markdown = bundle.get("markdown") or ""
        record(
            "PASS" if len(markdown) > 2000 else "FAIL",
            "markdown has substance",
            f"{len(markdown)} chars, {bundle['extraction']['pages']} pages",
        )

        headings = [ln for ln in markdown.splitlines() if ln.startswith("#")]
        record(
            "PASS" if len(headings) >= 3 else "FAIL",
            "structure survived as headings",
            f"{len(headings)} headings: {', '.join(h.strip('# ')[:20] for h in headings[:4])}",
        )

        table_lines = _TABLE_RE.findall(markdown)
        has_sep = any(_TABLE_SEP_RE.match(ln) for ln in markdown.splitlines())
        # Pipes and a separator row are not evidence a table is correct: a
        # table truncated to its header's width has both, and passed this
        # check while a whole column was missing. The service already counts
        # rendered cells against the cells Marker found; judge on that.
        dropped = [
            w for w in (bundle["extraction"].get("warnings") or []) if "were dropped" in w
        ]
        record(
            "PASS" if (table_lines and has_sep and not dropped) else "FAIL",
            "tables survived as markdown tables",
            f"{len(table_lines)} table rows, separator={'yes' if has_sep else 'NO'}, "
            f"{len(dropped)} table(s) lost cells",
        )

        record(
            "PASS" if "$$" in markdown or "$" in markdown else "FAIL",
            "equations survived as LaTeX",
            f"{markdown.count('$$')} display-math markers",
        )

        figures = bundle.get("figures") or []
        record(
            "PASS" if figures else "FAIL",
            "figure index is populated",
            f"{len(figures)} figures; captions: "
            + "; ".join((f.get('caption') or '(none)')[:40] for f in figures[:3]),
        )

        captioned = [f for f in figures if (f.get("caption") or "").strip()]
        record(
            "PASS" if captioned else "FAIL",
            "figures carry captions",
            f"{len(captioned)}/{len(figures)} captioned",
        )

        if figures:
            image_url = figures[0].get("image_url")
            try:
                resp = httpx.get(image_url, timeout=30.0)
                ok = resp.status_code == 200 and any(
                    resp.content.startswith(magic) for magic in _IMAGE_MAGIC
                )
                record(
                    "PASS" if ok else "FAIL",
                    "figure image downloads and is a real image",
                    f"HTTP {resp.status_code}, {len(resp.content)} bytes, "
                    f"magic={resp.content[:4]!r}",
                )
            except httpx.HTTPError as exc:
                record("FAIL", "figure image downloads", f"{type(exc).__name__}: {exc}")

        warnings = bundle["extraction"].get("warnings") or []
        record(
            "INFO" if warnings else "PASS",
            "extraction warnings",
            "; ".join(w[:70] for w in warnings[:3]) if warnings else "none",
        )

        # --- cache hit ------------------------------------------------------
        t0 = time.monotonic()
        cached = payload(await client.call_tool("extract_pdf", args))
        elapsed = time.monotonic() - t0
        record(
            "PASS" if cached["status"] == "ready" and elapsed < 10 else "FAIL",
            "second call is a fast cache hit",
            f"{elapsed:.2f}s, status={cached['status']}",
        )

    # --- artifact route safety ---------------------------------------------
    if figures:
        token = figures[0]["image_url"].split("/a/")[1].split("/")[0]
        evil = httpx.get(f"{base}/a/{token}/../../../etc/passwd", timeout=10.0)
        record(
            "PASS" if evil.status_code == 404 else "FAIL",
            "artifact route refuses traversal",
            f"HTTP {evil.status_code}",
        )


def _utf8_console() -> None:
    """Print extraction output without dying on it.

    Extraction output is arbitrary academic text. One warning naming a table
    column that contains a Greek epsilon (U+03F5) killed a run in which every
    content assertion had already passed, and the em dash in the title line
    mojibaked — a cp950 console can encode neither. Replace rather than raise:
    a check that dies while reporting success is worse than one that prints a
    question mark.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


async def main() -> int:
    _utf8_console()
    if len(sys.argv) < 2:
        print("usage: paper_workflow_check.py <path/to/paper.pdf>")
        return 2
    pdf_path = sys.argv[1]
    print(f"paper-mcp workflow check — {Path(pdf_path).name}")
    print("=" * 72, flush=True)

    if not httpx.get(f"{MARKER_URL}/health", timeout=5.0).is_success:
        print(f"Marker is not reachable at {MARKER_URL}. Run: docker compose up -d marker")
        return 2

    port = free_port()
    keep = os.environ.get("PAPER_MCP_CHECK_ARTIFACTS")
    with tempfile.TemporaryDirectory() as tmp:
        artifacts = Path(keep) if keep else Path(tmp) / "artifacts"
        proc, log = boot(port, artifacts)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(150):
                try:
                    if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.3)
            await run(pdf_path, base)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    print(f"artifacts: {artifacts}")
    failures = sum(1 for s, _, _ in _results if s == "FAIL")
    passes = sum(1 for s, _, _ in _results if s == "PASS")
    print(f"\n{'=' * 72}\n{passes} passed, {failures} failed\n{'=' * 72}")
    if failures:
        print(f"server log: {log}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
